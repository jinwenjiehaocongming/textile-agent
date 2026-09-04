"""
鉴权模块 — JWT 签发 / 验签 / 认证 / 授权（单一事实来源）
=====================================================
分层语义（面试点）：
- 认证 Authentication（你是谁）：`Authorization: Bearer <JWT>` → 验签 + 验过期
  → 解析出 {"user_id", "role"}；失败抛 401
- 授权 Authorization（你能干什么）：`require_role(role)` 依赖，角色不符 → 403

身份源可替换（关键设计）：
- token 一律由本模块签发，签发入口可以是
  ① /dev/login（DEV_MODE=1 开发用，mock 微信身份，便于本地切换客户/管理员）
  ② 企业微信 OAuth 回调（生产，二期接入，微信静默授权换证后调 create_token）
- 前端只认 {token, role}，身份源怎么来不影响其余代码——"换证"思想的落地

安全要点：
- JWT_SECRET 必设（生产）；DEV_MODE 下缺失时用开发默认并告警，生产缺失直接抛错
- 密钥只存环境变量，绝不入库、绝不下发前端
- 授权永远在服务端：前端 role 只用于 UI 显隐，接口用 Depends 兜底
"""

import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, Header, HTTPException

_DEV = os.getenv("DEV_MODE") == "1"


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


class AuthError(Exception):
    """鉴权失败载体（认证失败 401 / 凭证非法）。"""

    def __init__(self, detail: str, status: int = 401):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def create_token(user_id: str, role: str = "customer", expire_hours: int = 8) -> str:
    """签发 JWT：sub=user_id（外部身份 external_userid / 管理员标识），role=customer|admin。"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": now + timedelta(hours=expire_hours),
    }
    return jwt.encode(payload, _secret(), algorithm=ALGO)


def decode_token(token: str) -> dict:
    """验签 + 验过期 + 解析身份。失败抛 AuthError（HTTP 层转 401）。"""
    try:
        payload = jwt.decode(token, _secret(), algorithms=[ALGO])
    except jwt.ExpiredSignatureError:
        raise AuthError("登录已过期，请重新登录", 401)
    except jwt.InvalidTokenError:
        raise AuthError("无效凭证", 401)
    role = payload.get("role")
    if role not in ("customer", "admin"):
        raise AuthError("凭证角色非法", 401)
    return {"user_id": payload.get("sub", ""), "role": role}


# ── FastAPI 依赖注入（服务端校验的命根子）──────────────────

def get_current_user(authorization: str = Header(default="")) -> dict:
    """认证依赖：任何受保护端点先过这层。无 token → 401。"""
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        return decode_token(token)
    except AuthError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


def require_role(role: str):
    """授权依赖工厂：认证通过后检查角色，不符 → 403。"""

    def checker(user: dict = Depends(get_current_user)) -> dict:
        if user.get("role") != role:
            raise HTTPException(status_code=403, detail="权限不足")
        return user

    return checker


require_admin = require_role("admin")
