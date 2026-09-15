"""
鉴权模块 — JWT 签发 / 验签 / 认证 / 授权（单一事实来源）
=====================================================
分层语义（面试点）：
- 认证 Authentication（你是谁）：`Authorization: Bearer <JWT>` → 验签 + 验过期
  → 解析出 {"user_id", "role", "jti", "sid"}；失败抛 401
- 授权 Authorization（你能干什么）：`require_role(role)` 依赖，角色不符 → 403

凭证体系（2026-09 路线②：短期 access + 可轮换 refresh）
=====================================================
- **access token（本模块）**：JWT，默认 **15 分钟**（``ACCESS_TOKEN_TTL_MINUTES``），
  **无状态** —— 验签不查任何存储，所以不能单独撤销，撤销窗口就是它的 TTL。
- **refresh token（``src/auth_sessions.py``）**：不透明随机串，存 Redis，
  轮换 + 重用检测 + 服务端登出；撤销能力全在这里。
设计上"短 access + 可撤销 refresh"：把撤销粒度问题从 access 挪到 refresh，
access 保持零查询（热路径不引入 IO）。

claims 说明：
- ``sub``  用户 id；``role`` customer|admin；``iat``/``exp`` 时间；``jti`` 唯一 id
- ``typ``  固定 ``access``（refresh 串不是 JWT，且类型校验能挡住"拿 refresh 当 access 用"）
- ``sid``  该 access 属于哪个登录会话（用于 /auth/sessions 标记"当前会话"）
- ``iss``/``aud``  签发方与受众（防跨系统串用 token）

**滚动升级友好**：``typ``/``iss``/``aud`` 采用"存在才校验"策略 —— 老版本签发的
token 没有这些 claim 仍然可用（最多再活一个 TTL），升级时不会把所有人踢下线；
等一个 TTL 过去后可以改成强制校验。

身份源可替换（关键设计）：
- token 一律由本模块签发，签发入口可以是
  ① /auth/login（账号密码）② /dev/login（DEV_MODE=1 开发用）
  ③ 企业微信 OAuth 回调（生产，二期接入，换证后调 create_token）
- 前端只认 {access_token, refresh_token, role}，身份源怎么来不影响其余代码

安全要点：
- JWT_SECRET 必设（生产）；DEV_MODE 下缺失时用开发默认并告警，生产缺失直接抛错
- 密钥只存环境变量，绝不入库、绝不下发前端
- 授权永远在服务端：前端 role 只用于 UI 显隐，接口用 Depends 兜底
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, Header, HTTPException

_DEV = os.getenv("DEV_MODE") == "1"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _secret() -> str:
    s = os.getenv("JWT_SECRET", "")
    if s:
        return s
    if _DEV:
        # 仅开发：固定密钥保证本地无 .env 也能跑；生产必须设置 JWT_SECRET
        print("[auth] ⚠️ 未设置 JWT_SECRET，DEV_MODE 下使用开发默认密钥（仅限本地调试）")
        return "dev-only-secret-do-not-use-in-prod"
    raise RuntimeError(
        "JWT_SECRET 未设置：生产环境必须配置强随机密钥（openssl rand -hex 32）"
    )


ALGO = "HS256"
JWT_ISSUER = os.getenv("JWT_ISSUER", "study1")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "study1-web")
# access 寿命：越短撤销窗口越小，代价是刷新更频繁（refresh 轮换让刷新几乎无感）
ACCESS_TTL_SECONDS = max(60, _env_int("ACCESS_TOKEN_TTL_MINUTES", 15) * 60)
# 时钟偏移容忍（验签时）；分布式部署机器时钟不可能完全一致
CLOCK_SKEW_SECONDS = max(0, _env_int("JWT_LEEWAY_SECONDS", 30))


class AuthError(Exception):
    """鉴权失败载体（认证失败 401 / 凭证非法）。"""

    def __init__(self, detail: str, status: int = 401):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def create_token(user_id: str, role: str = "customer", sid: str = "",
                 ttl_seconds: int = None, ver: int = 0) -> str:
    """签发 access token。

    :param sid: 关联的登录会话 id（来自 auth_sessions.create_session），便于标记"当前会话"
    :param ttl_seconds: 覆盖默认寿命（测试/特殊场景用）
    :param ver: 凭证版本（users.token_version）—— 改密码/封号时 +1，
                老 token 的 ver 对不上即失效。详见 auth_sessions.current_token_version
    """
    now = datetime.now(timezone.utc)
    ttl = int(ttl_seconds if ttl_seconds is not None else ACCESS_TTL_SECONDS)
    payload = {
        "sub": user_id,
        "role": role,
        "typ": "access",
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": now + timedelta(seconds=ttl),
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "ver": int(ver),
    }
    if sid:
        payload["sid"] = sid
    return jwt.encode(payload, _secret(), algorithm=ALGO)


def decode_token(token: str) -> dict:
    """验签 + 验过期 + 解析身份。失败抛 AuthError（HTTP 层转 401）。

    **纯函数**：不查 Redis/DB —— 这是 access token 保持无状态的代价与收益：
    热路径零 IO，但改密码/封号要等它过期（≤1 个 TTL），或靠 refresh 侧撤销。
    """
    try:
        payload = jwt.decode(
            token, _secret(), algorithms=[ALGO], leeway=CLOCK_SKEW_SECONDS,
            # iss/aud 由下面手工校验（"存在才校验"，兼容滚动升级期间的旧 token）
            options={"verify_aud": False, "verify_iss": False},
        )
    except jwt.ExpiredSignatureError:
        raise AuthError("登录已过期，请重新登录", 401)
    except jwt.InvalidTokenError:
        raise AuthError("无效凭证", 401)

    typ = payload.get("typ")
    if typ is not None and typ != "access":
        raise AuthError("凭证类型非法", 401)          # refresh 串/其他类型 token 不得当 access 用
    iss = payload.get("iss")
    if iss is not None and iss != JWT_ISSUER:
        raise AuthError("凭证签发方非法", 401)
    aud = payload.get("aud")
    if aud is not None and JWT_AUDIENCE not in (aud if isinstance(aud, list) else [aud]):
        raise AuthError("凭证受众非法", 401)
    role = payload.get("role")
    if role not in ("customer", "admin"):
        raise AuthError("凭证角色非法", 401)
    try:
        ver = int(payload.get("ver", 0))
    except (TypeError, ValueError):
        ver = 0
    return {
        "user_id": payload.get("sub", ""),
        "role": role,
        "jti": payload.get("jti", ""),
        "sid": payload.get("sid", ""),
        "ver": ver,
    }


# ── FastAPI 依赖注入（服务端校验的命根子）──────────────────

async def get_current_user(authorization: str = Header(default="")) -> dict:
    """认证依赖：任何受保护端点先过这层。无 token → 401。

    2026-09 起多一步**凭证版本校验**（改密码/封号能让已签发的 access 立刻失效）：
    版本号走 60 秒读穿缓存，绝大多数请求只命中进程内缓存（零 IO）；
    缓存/DB 双故障时 fail-open —— access 本身已被 TTL 限制，不能把缓存故障升级成
    全站 401。用 ``AUTH_REVOCATION_CHECK=0`` 可关掉这一步，回到纯验签零查询。
    """
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        user = decode_token(token)
    except AuthError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)

    from src import auth_sessions          # 局部导入：避免 auth ↔ auth_sessions 循环依赖
    if auth_sessions.REVOCATION_CHECK:
        current = await auth_sessions.current_token_version(user["user_id"])
        if current >= 0 and user["ver"] < current:   # -1 = 未知（fail-open）
            raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录")
    return user


def require_role(role: str):
    """授权依赖工厂：认证通过后检查角色，不符 → 403。"""

    def checker(user: dict = Depends(get_current_user)) -> dict:
        if user.get("role") != role:
            raise HTTPException(status_code=403, detail="权限不足")
        return user

    return checker


require_admin = require_role("admin")
