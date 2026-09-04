"""
鉴权测试 — JWT 签发/验签 + 认证/授权依赖 + /dev/login + /me + 审批端点三态
=========================================================================
核心验收（面试演示点）：
- 审批端点三态：无 token → 401；客户 token → 403；管理员 token → 200
- 身份可切换：/dev/login 按 role 签发不同 token（mock 微信身份）

注意：
- DEV_MODE / JWT_SECRET 必须在 import app 之前设置（路由注册与模块级常量在 import 时确定）
- TestClient 不进 with 块 → 不触发 lifespan → 不连接 MCP 子进程
"""
import os
import time

os.environ["DEV_MODE"] = "1"
os.environ["JWT_SECRET"] = "test-secret-only-0123456789abcdef0123456789abcdef"

import jwt as pyjwt  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.auth import AuthError, create_token, decode_token  # noqa: E402
from app import app  # noqa: E402

client = TestClient(app)


# ── 纯函数：签发 / 验签 ─────────────────────────────────────

def test_create_and_decode_token():
    token = create_token("alice", role="admin")
    payload = decode_token(token)
    assert payload["user_id"] == "alice"
    assert payload["role"] == "admin"


def test_token_tampered():
    """篡改 payload（把 role 改成 admin）→ 签名对不上 → 401。"""
    token = create_token("bob", role="customer")
    header, payload_b64, sig = token.split(".")
    # 篡改 payload 内容（base64 补位后替换）
    import base64
    raw = base64.urlsafe_b64decode(payload_b64 + "==")
    raw = raw.replace(b'"customer"', b'"admin"')
    forged_b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    forged = f"{header}.{forged_b64}.{sig}"
    with pytest.raises(AuthError) as ei:
        decode_token(forged)
    assert ei.value.status == 401


def test_token_expired():
    """过期 token → 401（客户端应重新登录）。"""
    now = int(time.time())
    expired = pyjwt.encode(
        {"sub": "carol", "role": "customer", "iat": now - 7200, "exp": now - 3600},
        os.environ["JWT_SECRET"], algorithm="HS256",
    )
    with pytest.raises(AuthError) as ei:
        decode_token(expired)
    assert ei.value.status == 401


# ── /dev/login：mock 身份签发 ────────────────────────────────

def test_dev_login_admin():
    r = client.post("/dev/login", json={"role": "admin"})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "admin"
    assert body["user_id"] == "dev_admin"
    assert decode_token(body["token"])["role"] == "admin"


def test_dev_login_customer_default():
    r = client.post("/dev/login", json={"role": "customer", "user_id": "u_test_001"})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "customer"
    assert body["user_id"] == "u_test_001"


def test_dev_login_invalid_role_defaults_to_customer():
    r = client.post("/dev/login", json={"role": "hacker"})
    assert r.status_code == 200
    assert r.json()["role"] == "customer"


def test_dev_login_invalid_user_id_400():
    r = client.post("/dev/login", json={"role": "admin", "user_id": "../../etc"})
    assert r.status_code == 400


# ── /me：身份探测 ────────────────────────────────────────────

def test_me_guest_without_token():
    r = client.get("/me")
    assert r.status_code == 200
    assert r.json()["role"] == "guest"


def test_me_with_admin_token():
    tok = create_token("dev_admin", role="admin")
    r = client.get("/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.json() == {"user_id": "dev_admin", "role": "admin"}


# ── 审批端点三态（鉴权核心验收）──────────────────────────────

def test_approval_pending_401_without_token():
    assert client.get("/approval/pending").status_code == 401


def test_approval_pending_403_with_customer_token():
    tok = create_token("customer_a", role="customer")
    r = client.get("/approval/pending", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


def test_approval_pending_200_with_admin_token():
    tok = create_token("dev_admin", role="admin")
    r = client.get("/approval/pending", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "pending" in r.json()


def test_approve_reject_401_without_token():
    assert client.post("/approval/approve", json={"thread_id": "x"}).status_code == 401
    assert client.post("/approval/reject", json={"thread_id": "x"}).status_code == 401
