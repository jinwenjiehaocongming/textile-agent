"""
账号模块 — users 表访问 + 密码哈希 + 注册/登录（单一事实来源）
==========================================================
分层（延续 auth.py 的叙事）：
- src/auth.py      签发/验签 JWT、认证/授权依赖（身份载体）
- src/users.py     账号持久化：谁有资格登录、密码对不对（身份来源）

安全要点：
- 密码只存 bcrypt 哈希，绝不存明文、绝不下发前端
- username 小写归一化 + 唯一约束（防大小写撞号）
- 注册只允许 customer 角色（admin 只能由种子脚本创建，防越权注册）
- 查询类接口按 id/username 索引查（users.username 有 UNIQUE 索引）
"""

import re
import uuid
from datetime import datetime

from collections import OrderedDict

import bcrypt
from sqlalchemy.exc import ProgrammingError

from src.db import execute, execute_returning, query_one
from src.logging_config import get_logger

logger = get_logger(__name__)

# username：3-32 位，字母数字下划线连字符（对齐 user_identity 的字符集）
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
# 密码：最短 6 位（演示项目量级；生产可接强度策略/限流）
MIN_PASSWORD_LEN = 6
MAX_PASSWORD_LEN = 72  # bcrypt 输入上限，超出拒绝而非静默截断

ROLE_CUSTOMER = "customer"
ROLE_ADMIN = "admin"
ROLES = (ROLE_CUSTOMER, ROLE_ADMIN)


class UsernameTaken(Exception):
    """用户名已存在（HTTP 层映射 409）。"""


# bcrypt 成本因子（12 ≈ 0.2-0.3s，登录可接受；生产可调 12-14）
BCRYPT_ROUNDS = 12


# ── 密码哈希 ────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """生成 bcrypt 哈希（带随机盐，自含成本因子）。"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """校验密码。哈希非法返回 False（不抛错，避免信息泄露）。"""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ── 校验 ────────────────────────────────────────────────────

def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def validate_username(username: str) -> str:
    u = normalize_username(username)
    if not USERNAME_RE.match(u):
        raise ValueError("用户名需为 3-32 位字母、数字、下划线或连字符")
    return u


def validate_password(password: str) -> None:
    if password is None or len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"密码至少 {MIN_PASSWORD_LEN} 位")
    if len(password) > MAX_PASSWORD_LEN:
        raise ValueError(f"密码不能超过 {MAX_PASSWORD_LEN} 位")


def validate_display_name(name: str) -> str:
    n = (name or "").strip()
    if len(n) > 32:
        raise ValueError("昵称不能超过 32 个字符")
    return n


# ── 查询 ────────────────────────────────────────────────────

async def get_user_by_id(user_id: str) -> dict | None:
    """按 id 查用户（users.id = JWT sub）。"""
    return await query_one(
        "SELECT id, username, password_hash, display_name, role, status, created_at "
        "FROM users WHERE id = :uid", {"uid": user_id}
    )


async def get_user_by_username(username: str) -> dict | None:
    """按登录名查用户（username 已小写归一化 + UNIQUE）。"""
    return await query_one(
        "SELECT id, username, password_hash, display_name, role, status, created_at "
        "FROM users WHERE username = :u", {"u": normalize_username(username)}
    )


def to_public(user: dict) -> dict:
    """对外安全视图：绝不带 password_hash。"""
    return {
        "user_id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"] or user["username"],
        "role": user["role"],
    }


# ── 写 ──────────────────────────────────────────────────────

async def create_user(username: str, password: str,
                      display_name: str = "", role: str = ROLE_CUSTOMER) -> dict:
    """注册新用户（仅 customer 可经此创建；admin 走 scripts/create_admin.py）。

    返回对外视图 {user_id, username, display_name, role}。
    用户名已存在 → 抛 ValueError（HTTP 层映射 409）。
    """
    u = validate_username(username)
    validate_password(password)
    name = validate_display_name(display_name or u)
    if role not in ROLES:
        raise ValueError("非法角色")

    if await get_user_by_username(u):
        raise UsernameTaken("用户名已存在")

    uid = uuid.uuid4().hex  # 32 位 hex，无横线（对齐 user_identity 字符集）
    await execute(
        "INSERT INTO users (id, username, password_hash, display_name, role, status, created_at) "
        "VALUES (:id, :username, :pw, :name, :role, 'active', :ts)",
        {"id": uid, "username": u, "pw": hash_password(password),
         "name": name, "role": role, "ts": datetime.now().isoformat()},
    )
    return {"user_id": uid, "username": u, "display_name": name, "role": role}


# ── 影子账号：立住"凡是被接受的身份，users 里都有行"（外键前提）──────
# 为什么需要：业务表都用 user_id 关联 users，而代码里确实存在"没有账号行"的身份：
#   - sanitize_user_id 的 guest 兜底、/dev/login 的 mock 用户（dev_customer/dev_admin）、
#     HITL 演示账号（hitl_demo_user）、评测脚本的 eval_user、历史自由文本标识（客户/手机号/123456）
# 实测：这类 user_id 在库里出现过 63 个。要给业务表加外键，必须先让它们都有账号行，
# 否则那些写入会被数据库直接拒绝（不是"数据脏"，是"流程漏了开户这一步"）。
_KNOWN_USER_CACHE: "OrderedDict[str, bool]" = OrderedDict()
_KNOWN_USER_CACHE_MAX = 2000


async def ensure_user_row(user_id: str, role: str = ROLE_CUSTOMER,
                          display_name: str = "") -> None:
    """保证该 user_id 在 users 里有行（幂等；进程内缓存避免每次写都查库）。

    影子行的语义：它代表"系统见过的身份"，不是"可登录的账号"（password_hash 为空串，
    永远无法通过密码校验登录）。真正的登录账号仍由 create_user 创建。
    """
    if not user_id or _KNOWN_USER_CACHE.get(user_id):
        return
    await execute(
        "INSERT INTO users (id, username, password_hash, display_name, role, status, created_at) "
        "VALUES (:id, :username, '', :name, :role, 'active', :ts) "
        "ON CONFLICT (id) DO NOTHING",
        {"id": user_id, "username": user_id[:32], "name": display_name or user_id,
         "role": role, "ts": datetime.now().isoformat()},
    )
    _KNOWN_USER_CACHE[user_id] = True
    while len(_KNOWN_USER_CACHE) > _KNOWN_USER_CACHE_MAX:
        _KNOWN_USER_CACHE.popitem(last=False)


def reset_user_cache() -> None:
    """清空影子账号缓存。

    ⚠️ 必须有这个口子：缓存假设"行一旦存在就不会消失"，但 `reset_schema()`（测试、
    初始化脚本）会把 users 表整个重建 —— 此时缓存还在，于是 `ensure_user_row` 以为
    用户存在而跳过插入，随后的写入就撞外键。**缓存与"世界被重置"必须一起失效。**
    """
    _KNOWN_USER_CACHE.clear()


async def auth_user(username: str, password: str) -> dict | None:
    """登录校验：用户名+密码 → 通过返回对外视图，失败/禁用返回 None。"""
    user = await get_user_by_username(username)
    if not user or user.get("status") != "active":
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return to_public(user)


# ── 凭证版本 / 账号状态（2026-09：改密封号闭环）────────────────
# token_version 是"秒级吊销"的支点：改密码/封号时 +1，之前签发的 access token
# 里的 ver 就对不上了 → 立刻失效（access 本身无状态，撤不了，只能这样间接判定）。

_schema_warned = False


async def get_token_version(user_id: str) -> int:
    """读凭证版本（DB 为唯一真相来源；调用方通常还会在外面加一层缓存）。

    **缺列容错**：老库没跑过迁移时 ``users.token_version`` 不存在。这时按 **0** 处理
    并打告警，而不是 500 —— 语义上也正确：迁移会给所有存量行填 0，
    所以"缺列"和"全是 0"是同一个状态，登录不该因此挂掉。
    （真实踩过：直接 uvicorn 起服务、没跑建表脚本 → 登录 500，排查了半天。）
    """
    global _schema_warned
    try:
        row = await query_one(
            "SELECT token_version FROM users WHERE id = :uid", {"uid": user_id})
        return int((row or {}).get("token_version") or 0)
    except ProgrammingError as e:
        if "token_version" in str(e):
            if not _schema_warned:
                _schema_warned = True
                logger.error(
                    "[鉴权] users.token_version 列缺失：数据库未迁移。请执行 "
                    "python scripts/create_admin.py（内部 ensure_schema 幂等建表），"
                    "否则改密码/封号的秒级吊销不生效。已临时按版本 0 处理。")
            return 0
        raise


async def bump_token_version(user_id: str) -> int:
    """版本 +1（改密码 / 封号 / 强制全端下线）→ 返回新版本。

    用 ``execute_returning``（走 begin() 提交）而不是 ``query_one``：
    后者不提交事务，UPDATE 会被静默回滚 —— 版本号没变，等于封号没生效。
    """
    row = await execute_returning(
        "UPDATE users SET token_version = token_version + 1 WHERE id = :uid "
        "RETURNING token_version",
        {"uid": user_id},
    )
    return int((row or {}).get("token_version") or 0)


async def set_password(user_id: str, new_password: str) -> None:
    """改哈希（调用方负责先验证旧密码 + 随后 bump_token_version）。"""
    validate_password(new_password)
    await execute(
        "UPDATE users SET password_hash = :pw WHERE id = :uid",
        {"pw": hash_password(new_password), "uid": user_id},
    )


async def set_status(user_id: str, status: str) -> bool:
    """启用/禁用账号（禁用后登录被拒 + 调用方应同时吊销会话与版本）。"""
    if status not in ("active", "disabled"):
        raise ValueError("非法状态")
    row = await execute_returning(
        "UPDATE users SET status = :st WHERE id = :uid RETURNING id",
        {"st": status, "uid": user_id},
    )
    return bool(row)


async def verify_user_password(user_id: str, password: str) -> bool:
    """校验某用户当前密码（改密码时验旧密码用）。"""
    user = await get_user_by_id(user_id)
    if not user:
        return False
    return verify_password(password, user["password_hash"])
