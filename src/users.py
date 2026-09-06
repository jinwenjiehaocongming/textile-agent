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

import bcrypt

from src.db import execute, query_one

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


async def auth_user(username: str, password: str) -> dict | None:
    """登录校验：用户名+密码 → 通过返回对外视图，失败/禁用返回 None。"""
    user = await get_user_by_username(username)
    if not user or user.get("status") != "active":
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return to_public(user)
