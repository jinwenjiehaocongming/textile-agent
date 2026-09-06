"""账号体系测试 — users 表 / bcrypt 哈希 / 注册 / 登录 / /me（2026-09）

- 注册：开放、强制 customer 角色、username 小写归一化、重复 409
- 登录：成功签发 token、密码错/用户不存在统一 401（不泄露账号存在性）
- 安全：password_hash 绝不下发；注册接口不接受 role 越权
"""
import os

os.environ["DEV_MODE"] = "1"
os.environ["JWT_SECRET"] = "test-secret-only-0123456789abcdef0123456789abcdef"

import httpx  # noqa: E402
import pytest  # noqa: E402

from app import app  # noqa: E402
from src.db import reset_schema  # noqa: E402
from src.users import auth_user, hash_password, verify_password  # noqa: E402


async def _api() -> httpx.AsyncClient:
    """同 loop 的 ASGI 客户端（与 pytest-asyncio 测试共用事件循环）。"""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
async def _clean_schema():
    """清表重建（含 users），测试间隔离。"""
    await reset_schema()
    yield


async def test_hash_roundtrip():
    h = hash_password("s3cret-pass")
    assert h != "s3cret-pass"
    assert verify_password("s3cret-pass", h)
    assert not verify_password("wrong-pass", h)


async def test_auth_user_unknown_returns_none():
    assert await auth_user("nobody", "whatever1") is None


async def test_register_and_login_flow():
    async with await _api() as c:
        r = await c.post("/auth/register", json={
            "username": "Alice", "password": "pass123456", "display_name": "爱丽丝"})
        assert r.status_code == 200
        body = r.json()
        assert body["username"] == "alice"  # 小写归一化
        assert body["role"] == "customer"
        assert body["display_name"] == "爱丽丝"
        assert "token" in body

        # 注册即登录：token 可直接过 /me
        me = await c.get("/me", headers={"Authorization": f"Bearer {body['token']}"})
        assert me.status_code == 200
        assert me.json()["username"] == "alice"

        # 重复注册 → 409
        dup = await c.post("/auth/register", json={
            "username": "alice", "password": "pass123456"})
        assert dup.status_code == 409

        # 登录成功
        login = await c.post("/auth/login", json={
            "username": "alice", "password": "pass123456"})
        assert login.status_code == 200
        assert login.json()["username"] == "alice"

        # 密码错误 → 401
        bad = await c.post("/auth/login", json={
            "username": "alice", "password": "wrong-pass"})
        assert bad.status_code == 401

        # 未知用户名 → 同样 401（不泄露用户名是否存在）
        nope = await c.post("/auth/login", json={
            "username": "ghost_user", "password": "whatever1"})
        assert nope.status_code == 401


async def test_register_role_ignored():
    """注册接口不接受 role 字段：越权注册 admin 被忽略，永远是 customer。"""
    async with await _api() as c:
        r = await c.post("/auth/register", json={
            "username": "hacker", "password": "pass123456", "role": "admin"})
        assert r.status_code == 200
        assert r.json()["role"] == "customer"


async def test_register_validation():
    async with await _api() as c:
        assert (await c.post("/auth/register", json={
            "username": "u1", "password": "123"})).status_code == 400      # 弱密码
        assert (await c.post("/auth/register", json={
            "username": "a", "password": "12345678"})).status_code == 400  # 用户名过短
        assert (await c.post("/auth/register", json={
            "username": "ok_user", "password": "12345678"})).status_code == 200


async def test_never_return_password_fields():
    async with await _api() as c:
        r = await c.post("/auth/register", json={
            "username": "safe", "password": "pass123456"})
        raw = r.json()
        assert "password" not in raw and "password_hash" not in raw
        # DB 里只存哈希
        from src.db import query_one
        row = await query_one(
            "SELECT password_hash FROM users WHERE username = :u", {"u": "safe"})
        assert row and row["password_hash"].startswith("$2b$")
        assert row["password_hash"] != "pass123456"
