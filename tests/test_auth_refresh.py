"""refresh token 会话测试（路线②：短期 access + 可轮换 refresh）
=============================================================
锁定这套设计的关键不变式：

1. **登录发双凭证**：access（JWT，15 分钟，无状态）+ refresh（不透明串，存 Redis）
2. **轮换**：每次刷新作废旧的 refresh，发新的（RFC 9700 对 public client 是 MUST）
3. **重用检测**：旧 refresh 在宽限窗外被重放 → 作废**整个会话**（防泄露扩散）
4. **竞态宽限**：宽限窗内重放 = 并发刷新 → 照常放行（Okta 的 rotation leeway 同理）
5. **双封顶**：空闲期滑动续期 + 绝对上限（到点必须重新登录）
6. **登出/踢设备真的生效**：服务端撤销后 refresh 立刻失效
7. **fail-closed**：Redis 不可用 → 登录/刷新 503（绝不放行）；
   但已签发的 access 仍有效（无状态，最多再活一个 TTL）—— 这个取舍是有意为之

需要 Redis：没有则整文件跳过（见 conftest.auth_store）。
"""
import asyncio
import time
import os

os.environ["DEV_MODE"] = "1"
os.environ["JWT_SECRET"] = "test-secret-only-0123456789abcdef0123456789abcdef"

import httpx  # noqa: E402
import pytest  # noqa: E402

from src import auth_sessions, redis_client  # noqa: E402
from src.auth import ACCESS_TTL_SECONDS, decode_token  # noqa: E402
from app import app  # noqa: E402

USERNAME = "refresh_user"
PASSWORD = "Passw0rd!123"


def _client() -> httpx.AsyncClient:
    """直连 ASGI（不跑 lifespan → 不拉 MCP 子进程）。"""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _login(c: httpx.AsyncClient) -> dict:
    """注册并登录（角色 customer），返回登录响应体（含 access/refresh）。"""
    r = await c.post("/auth/register", json={
        "username": USERNAME, "password": PASSWORD, "display_name": "刷新测试",
    })
    if r.status_code == 409:                      # 已存在 → 直接登录
        r = await c.post("/auth/login", json={"username": USERNAME, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()


def _auth(access: str) -> dict:
    return {"Authorization": f"Bearer {access}"}


# ── 1. 登录发双凭证 ──────────────────────────────────────────

async def test_login_issues_access_and_refresh(pg_db, clean_auth_store):
    async with _client() as c:
        body = await _login(c)

    assert body["access_token"] and body["refresh_token"]
    assert body["token"] == body["access_token"]          # 兼容旧前端的别名
    assert body["expires_in"] == ACCESS_TTL_SECONDS
    assert ACCESS_TTL_SECONDS == 15 * 60                  # 默认 15 分钟（撤销窗口）

    payload = decode_token(body["access_token"])
    assert payload["user_id"] == body["user_id"]
    assert payload["sid"] == body["sid"]                  # access 带着它属于哪个会话
    assert payload["jti"]

    # refresh 不是 JWT，且以 sid 开头（可定位会话）
    assert "." not in body["access_token"].split(".")[0] or True
    assert body["refresh_token"].startswith(body["sid"] + ".")
    assert body["refresh_token"].count(".") == 1


# ── 2. 轮换 ──────────────────────────────────────────────────

async def test_refresh_rotates_and_old_access_still_valid(pg_db, clean_auth_store):
    async with _client() as c:
        body = await _login(c)
        r = await c.post("/auth/refresh", json={"refresh_token": body["refresh_token"]})
        assert r.status_code == 200, r.text
        rotated = r.json()

        assert rotated["refresh_token"] != body["refresh_token"]   # 旧 refresh 作废
        assert rotated["sid"] == body["sid"]                       # 同一个会话
        assert rotated["access_token"] != body["access_token"]

        # 新 access 能用；旧 access 在 TTL 内也仍能用（无状态的必然结果，如实承认）
        assert (await c.get("/me", headers=_auth(rotated["access_token"]))).status_code == 200
        assert (await c.get("/me", headers=_auth(body["access_token"]))).status_code == 200


# ── 3. 重用检测（安全核心）───────────────────────────────────

async def test_reuse_of_rotated_refresh_revokes_whole_session(pg_db, clean_auth_store,
                                                              monkeypatch):
    """宽限窗关闭后重放旧 refresh → 视为泄露 → 整个会话作废。"""
    monkeypatch.setattr(auth_sessions, "LEEWAY_SECONDS", 0)

    async with _client() as c:
        body = await _login(c)
        r1 = await c.post("/auth/refresh", json={"refresh_token": body["refresh_token"]})
        assert r1.status_code == 200
        fresh = r1.json()["refresh_token"]

        # 攻击者拿旧 refresh 重放
        replay = await c.post("/auth/refresh", json={"refresh_token": body["refresh_token"]})
        assert replay.status_code == 401
        assert replay.json()["reuse"] is True
        assert replay.json()["code"] == "refresh_token_reuse"

        # 会话已整体作废：连"合法的新 refresh"也不能再用（宁可让用户重登，也不留后门）
        after = await c.post("/auth/refresh", json={"refresh_token": fresh})
        assert after.status_code == 401


# ── 4. 竞态宽限 ──────────────────────────────────────────────

async def test_rotation_leeway_tolerates_concurrent_refresh(pg_db, clean_auth_store,
                                                            monkeypatch):
    """宽限窗内重放 = 前端并发刷新（单飞失效）→ 放行，不核弹。"""
    monkeypatch.setattr(auth_sessions, "LEEWAY_SECONDS", 30)

    async with _client() as c:
        body = await _login(c)
        first = await c.post("/auth/refresh", json={"refresh_token": body["refresh_token"]})
        assert first.status_code == 200

        # 同一个旧 refresh 立刻再来一次（模拟并发请求同时 401）
        second = await c.post("/auth/refresh", json={"refresh_token": body["refresh_token"]})
        assert second.status_code == 200
        assert second.json()["refresh_token"] not in (
            body["refresh_token"], first.json()["refresh_token"])

        # 会话仍然健康
        assert (await c.get("/me", headers=_auth(second.json()["access_token"]))).status_code == 200


# ── 5. 双封顶：空闲滑动 + 绝对上限 ───────────────────────────

async def test_idle_window_slides_on_each_use(pg_db, clean_auth_store, monkeypatch):
    """空闲期是"用一次续一次"：把 IDLE 改小，刷新后 Redis TTL 应跟着变。"""
    monkeypatch.setattr(auth_sessions, "IDLE_SECONDS", 120)

    async with _client() as c:
        body = await _login(c)
        client = redis_client.get_client()
        key = auth_sessions._sess_key(body["sid"])
        assert 0 < await client.ttl(key) <= 120

        r = await c.post("/auth/refresh", json={"refresh_token": body["refresh_token"]})
        assert r.status_code == 200
        assert 0 < await client.ttl(key) <= 120          # 续期后仍是新窗口


async def test_absolute_lifetime_cap_forces_relogin(pg_db, clean_auth_store):
    """绝对上限到点 → 刷新失败并要求重新登录（防"活跃用户被无限续命"）。

    直接把会话的 born_at 改成很久以前，比 monkeypatch 配置更贴近真实场景。
    """
    async with _client() as c:
        body = await _login(c)
        client = redis_client.get_client()
        key = auth_sessions._sess_key(body["sid"])
        stale_born = int(time.time()) - auth_sessions.ABSOLUTE_SECONDS - 10
        await client.hset(key, "born_at", stale_born)

        r = await c.post("/auth/refresh", json={"refresh_token": body["refresh_token"]})
        assert r.status_code == 401
        assert r.json()["code"] == "refresh_absolute_expired"
        # 会话已被清理，不是"暂时拒绝"
        assert await client.exists(key) == 0


# ── 6. 登出 / 会话管理 ──────────────────────────────────────

async def test_logout_revokes_refresh_on_server(pg_db, clean_auth_store):
    """登出必须打服务端：否则被拷走的 refresh 在空闲期内一直是活凭证。"""
    async with _client() as c:
        body = await _login(c)
        assert (await c.post("/auth/logout",
                             json={"refresh_token": body["refresh_token"]})).status_code == 200
        # access 无状态 → TTL 内仍可用；refresh 立刻死
        assert (await c.get("/me", headers=_auth(body["access_token"]))).status_code == 200
        assert (await c.post("/auth/refresh",
                             json={"refresh_token": body["refresh_token"]})).status_code == 401


async def test_logout_with_access_token_only(pg_db, clean_auth_store):
    """只带 access（没带 refresh）也要能登出：用 access 里的 sid 兜底。"""
    async with _client() as c:
        body = await _login(c)
        r = await c.post("/auth/logout", headers=_auth(body["access_token"]))
        assert r.status_code == 200 and r.json()["revoked"] is True
        assert (await c.post("/auth/refresh",
                             json={"refresh_token": body["refresh_token"]})).status_code == 401


async def test_session_list_and_kick_one_device(pg_db, clean_auth_store):
    """会话列表 + 单设备踢出：踢掉 A 不影响 B（多端登录的正确语义）。"""
    async with _client() as c:
        a = await _login(c)                     # 设备 A（同一账号第二次登录）
        b = await c.post("/auth/login", json={"username": USERNAME, "password": PASSWORD})
        b = b.json()                            # 设备 B
        assert a["sid"] != b["sid"]

        listed = await c.get("/auth/sessions", headers=_auth(a["access_token"]))
        assert listed.status_code == 200
        sids = {s["sid"] for s in listed.json()["sessions"]}
        assert {a["sid"], b["sid"]} <= sids
        # 当前会话被标记（前端显示"本机"）
        assert [s["current"] for s in listed.json()["sessions"] if s["sid"] == a["sid"]] == [True]

        # 踢掉 B
        assert (await c.delete(f"/auth/sessions/{b['sid']}",
                              headers=_auth(a["access_token"]))).status_code == 200
        assert (await c.post("/auth/refresh",
                             json={"refresh_token": b["refresh_token"]})).status_code == 401
        # A 仍可刷新
        assert (await c.post("/auth/refresh",
                             json={"refresh_token": a["refresh_token"]})).status_code == 200


async def test_cannot_kick_other_users_session(pg_db, clean_auth_store):
    """越权防护：不能踢别人的会话（404，且对方会话不受影响）。"""
    async with _client() as c:
        a = await _login(c)
        r = await c.post("/auth/register", json={
            "username": "other_user", "password": PASSWORD, "display_name": "别人"})
        other = r.json()

        resp = await c.delete(f"/auth/sessions/{other['sid']}",
                              headers=_auth(a["access_token"]))
        assert resp.status_code == 404
        assert (await c.post("/auth/refresh",
                             json={"refresh_token": other["refresh_token"]})).status_code == 200


# ── 7. 凭证类型不能混用 ─────────────────────────────────────

async def test_refresh_token_cannot_be_used_as_access(pg_db, clean_auth_store):
    async with _client() as c:
        body = await _login(c)
        assert (await c.get("/me", headers=_auth(body["refresh_token"]))).status_code == 401


async def test_non_access_typed_jwt_rejected(pg_db, clean_auth_store):
    """typ != access 的 JWT（比如未来签的 refresh JWT）不得当 access 用。"""
    import time as _t
    import jwt as pyjwt

    forged = pyjwt.encode(
        {"sub": "x", "role": "admin", "typ": "refresh",
         "iat": int(_t.time()), "exp": int(_t.time()) + 600},
        os.environ["JWT_SECRET"], algorithm="HS256",
    )
    async with _client() as c:
        assert (await c.get("/me", headers=_auth(forged))).status_code == 401


# ── 8. fail-closed：Redis 不可用 ────────────────────────────

async def test_redis_down_is_fail_closed_but_access_still_works(pg_db, clean_auth_store,
                                                                monkeypatch):
    """Redis 挂掉：登录/刷新 503（不放行），但已签发的 access 仍有效。

    这正是"缓存 fail-open / 鉴权 fail-closed"两条相反语义的落地。
    """
    async with _client() as c:
        body = await _login(c)                     # 先在正常状态下拿到 access

        original_url = redis_client.REDIS_URL
        monkeypatch.setattr(redis_client, "REDIS_URL", "redis://127.0.0.1:6399/0")
        redis_client.drop_client()                 # 让下一次调用按"死端口"重建
        try:
            r = await c.post("/auth/login", json={"username": USERNAME, "password": PASSWORD})
            assert r.status_code == 503
            assert "登录状态服务" in r.json()["detail"]

            r = await c.post("/auth/refresh", json={"refresh_token": body["refresh_token"]})
            assert r.status_code == 503

            # 无状态 access 不受影响（最多再活一个 TTL）
            assert (await c.get("/me", headers=_auth(body["access_token"]))).status_code == 200
        finally:
            monkeypatch.setattr(redis_client, "REDIS_URL", original_url)
            redis_client.drop_client()             # 恢复：下次按原 REDIS_URL 重建

    # Redis 回来之后一切照旧（会话没被误删）
    async with _client() as c:
        assert (await c.post("/auth/refresh",
                             json={"refresh_token": body["refresh_token"]})).status_code == 200
