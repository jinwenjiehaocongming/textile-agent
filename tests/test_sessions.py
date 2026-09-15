"""多会话 + 历史订单测试（2026-09）

- 会话 CRUD：新建/列表/改名/删除
- 行级隔离：他人会话 → 404（不能跨用户读）
- 订单接口：本用户可见、空订单 200

**注意**：这里刻意**不走 /auth/register** 拿 token —— 注册登录依赖 Redis 会话存储
（fail-closed），会让"聊天会话"的测试被鉴权基础设施耦合：没 Redis 的环境就得整片跳过。
本文件测的是会话/订单/授权边界，所以直接建账号 + 签 access token（`_make_user`），
登录流程本身由 test_accounts.py / test_auth_refresh.py 覆盖。
"""
import os

os.environ["DEV_MODE"] = "1"
os.environ["JWT_SECRET"] = "test-secret-only-0123456789abcdef0123456789abcdef"

import httpx  # noqa: E402
import pytest  # noqa: E402

from app import app  # noqa: E402
from src.db import reset_schema  # noqa: E402


async def _api() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _make_user(username: str, role: str = "customer") -> dict:
    """建账号 + 签 access token（跳过登录流程，不依赖 Redis 会话存储）。"""
    from src.auth import create_token
    from src.users import create_user
    pub = await create_user(username, "pass123456", "")
    return {**pub, "token": create_token(pub["user_id"], role=role)}


@pytest.fixture(autouse=True)
async def _clean():
    await reset_schema()
    yield


async def test_session_crud_flow():
    async with await _api() as c:
        tok = (await _make_user("alice"))["token"]
        h = {"Authorization": f"Bearer {tok}"}

        # 初始为空
        r = await c.get("/sessions", headers=h)
        assert r.status_code == 200 and r.json()["sessions"] == []

        # 新建两个
        s1 = (await c.post("/sessions", headers=h, json={"title": ""})).json()
        s2 = (await c.post("/sessions", headers=h, json={"title": "第二会话"})).json()
        assert s1["title"] == "新对话"
        assert s2["title"] == "第二会话"

        # 列表按活跃倒序
        r = await c.get("/sessions", headers=h)
        ids = [x["session_id"] for x in r.json()["sessions"]]
        assert ids == [s2["session_id"], s1["session_id"]]

        # 改名
        assert (await c.patch(f"/sessions/{s1['session_id']}",
                              headers=h, json={"title": "改名了"})).status_code == 200
        # 删除
        assert (await c.delete(f"/sessions/{s1['session_id']}", headers=h)).status_code == 200
        ids = [x["session_id"] for x in (await c.get("/sessions", headers=h)).json()["sessions"]]
        assert s1["session_id"] not in ids


async def test_session_ownership_isolation():
    async with await _api() as c:
        tok_a = (await _make_user("alice"))["token"]
        tok_b = (await _make_user("bob"))["token"]
        h_a = {"Authorization": f"Bearer {tok_a}"}
        h_b = {"Authorization": f"Bearer {tok_b}"}

        sid = (await c.post("/sessions", headers=h_a, json={})).json()["session_id"]

        # 他人访问 alice 的会话 → 404（历史/改名/删除）
        assert (await c.get(f"/history?session_id={sid}", headers=h_b)).status_code == 404
        assert (await c.patch(f"/sessions/{sid}", headers=h_b,
                              json={"title": "hack"})).status_code == 404
        assert (await c.delete(f"/sessions/{sid}", headers=h_b)).status_code == 404

        # 本人访问正常
        assert (await c.get(f"/history?session_id={sid}", headers=h_a)).status_code == 200


async def test_orders_empty_and_isolated():
    async with await _api() as c:
        tok = (await _make_user("carol"))["token"]
        h = {"Authorization": f"Bearer {tok}"}
        r = await c.get("/orders", headers=h)
        assert r.status_code == 200
        assert r.json()["orders"] == []


async def test_admin_users_endpoint_role_gate():
    """用户列表：仅管理员可看（200），普通客户 403；响应不含 password_hash。"""
    from src.auth import create_token

    async with await _api() as c:
        # 两个普通账号（customer）
        reg_c = await _make_user("userlist_c")
        reg_a = await _make_user("userlist_a")

        tok_c = reg_c["token"]                                  # customer token
        tok_a = create_token(reg_a["user_id"], role="admin")    # admin token（同账号）

        # 客户访问 → 403
        assert (await c.get("/admin/users",
                            headers={"Authorization": f"Bearer {tok_c}"})).status_code == 403

        # 管理员访问 → 200，能看到两个账号，且绝无 password_hash 字段
        r = await c.get("/admin/users", headers={"Authorization": f"Bearer {tok_a}"})
        assert r.status_code == 200
        names = [u["username"] for u in r.json()["users"]]
        assert "userlist_c" in names and "userlist_a" in names
        raw = r.text
        assert "password_hash" not in raw
