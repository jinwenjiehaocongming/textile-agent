"""限流 + 改密/封号闭环测试（2026-09）
=====================================
两块内容，都是"鉴权最后一块拼图"：

**A. 限流（`src/rate_limit.py`，Redis 令牌桶）**
1. 同 IP 高频登录 → 429 + Retry-After
2. 同 账号+IP 连续失败 → 锁定（429），**正确密码也被挡**（限流在密码校验之前）
3. 登录成功即清失败计数（不惩罚正常用户手滑）
4. 桶按维度隔离：不同 IP / 不同账号互不影响（直接对模块断言，避免伪造 XFF）
5. Redis 不可用 → **fail-open**（放行 + 告警）——限流是降风险，不是授权判定，
   不能把 Redis 抖动升级成"谁都登不上"

**B. 改密码 / 封号（含 token_version：让已签发的 access 也立刻失效）**
6. 改密码要旧密码正确、新密码合规、不能与旧的相同
7. **改密码后：旧 access 立刻 401**（token_version 生效，不用等 15 分钟）、
   其他设备的 refresh 全部失效、新密码能登录、旧密码不能
8. **封号后：被禁用户的 access 立刻 401、refresh 401**；解封后能重新登录
9. 权限：非管理员不能调封号接口（403）、改密码必须带自己的 token（401）
"""
import os

os.environ["DEV_MODE"] = "1"
os.environ["JWT_SECRET"] = "test-secret-only-0123456789abcdef0123456789abcdef"
# 关掉限流的"生产级宽松"值，让用例能在几次请求内触发阈值（按需在最下方单独覆盖）
os.environ.setdefault("RL_LOGIN_IP_CAPACITY", "20")
os.environ.setdefault("RL_LOGIN_FAIL_CAPACITY", "3")
os.environ.setdefault("RL_REGISTER_IP_CAPACITY", "100")

import httpx  # noqa: E402
import pytest  # noqa: E402

from src import auth_sessions, rate_limit  # noqa: E402
from src.auth import create_token  # noqa: E402
from app import app  # noqa: E402

PASSWORD = "Passw0rd!123"
NEW_PASSWORD = "NewPassw0rd!456"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _auth(access: str) -> dict:
    return {"Authorization": f"Bearer {access}"}


async def _signup(c: httpx.AsyncClient, username: str) -> dict:
    r = await c.post("/auth/register", json={
        "username": username, "password": PASSWORD, "display_name": username})
    assert r.status_code == 200, r.text
    return r.json()


# ══════════════════════════════════════════════════════════════
# A. 限流
# ══════════════════════════════════════════════════════════════

async def test_login_failures_lock_account_and_ip(pg_db, clean_auth_store, monkeypatch):
    """连续失败到阈值 → 锁定；**正确密码也进不来**（限流在校验之前，这是故意的）。"""
    monkeypatch.setattr(rate_limit, "LOGIN_FAIL", (3, 900))

    async with _client() as c:
        await _signup(c, "lockme")

        for i in range(3):
            r = await c.post("/auth/login", json={"username": "lockme", "password": "wrong-pass"})
            assert r.status_code == 401, f"第 {i+1} 次错误密码应 401"

        # 第 4 次：即使密码正确也被锁
        r = await c.post("/auth/login", json={"username": "lockme", "password": PASSWORD})
        assert r.status_code == 429
        assert "锁定" in r.json()["detail"]
        assert int(r.headers["Retry-After"]) >= 1
        assert r.json()["retry_after"] == int(r.headers["Retry-After"])


async def test_successful_login_resets_failure_counter(pg_db, clean_auth_store, monkeypatch):
    """失败 2 次后成功登录 → 计数清零（下次再错不应该立刻被锁）。"""
    monkeypatch.setattr(rate_limit, "LOGIN_FAIL", (3, 900))

    async with _client() as c:
        await _signup(c, "handslip")
        for _ in range(2):
            assert (await c.post("/auth/login", json={
                "username": "handslip", "password": "nope"})).status_code == 401
        assert (await c.post("/auth/login", json={
            "username": "handslip", "password": PASSWORD})).status_code == 200

        # 计数已清零 → 再错 2 次仍应是 401（而不是第 3 次就到 429）
        for _ in range(2):
            assert (await c.post("/auth/login", json={
                "username": "handslip", "password": "nope"})).status_code == 401


async def test_login_ip_rate_limit(pg_db, clean_auth_store, monkeypatch):
    """同 IP 高频登录 → 429（不管账号对不对，先挡住脚本）。"""
    monkeypatch.setattr(rate_limit, "LOGIN_IP", (3, 60))

    async with _client() as c:
        await _signup(c, "burst")
        codes = []
        for _ in range(4):
            r = await c.post("/auth/login", json={"username": "burst", "password": PASSWORD})
            codes.append(r.status_code)
        assert codes[:3] == [200, 200, 200]
        assert codes[3] == 429


async def test_rate_limit_buckets_are_isolated(pg_db, clean_auth_store, monkeypatch):
    """维度隔离：账号 A 被锁不影响账号 B；IP1 打满不影响 IP2（直接测模块）。"""
    monkeypatch.setattr(rate_limit, "LOGIN_FAIL", (2, 900))

    for _ in range(2):
        await rate_limit.hit("login_fail", "alice|10.0.0.1", rate_limit.LOGIN_FAIL)
    blocked = await rate_limit.hit("login_fail", "alice|10.0.0.1", rate_limit.LOGIN_FAIL)
    assert blocked.allowed is False

    other_user = await rate_limit.hit("login_fail", "bob|10.0.0.1", rate_limit.LOGIN_FAIL)
    other_ip = await rate_limit.hit("login_fail", "alice|10.0.0.2", rate_limit.LOGIN_FAIL)
    assert other_user.allowed is True, "换账号不该受牵连"
    assert other_ip.allowed is True, "换 IP 不该受牵连"


async def test_rate_limit_fails_open_when_redis_down(pg_db, clean_auth_store, monkeypatch):
    """Redis 不可用 → 放行（fail-open）并告警，绝不把限流变成"谁都登不上"。"""
    from src import redis_client

    original = redis_client.REDIS_URL
    monkeypatch.setattr(redis_client, "REDIS_URL", "redis://127.0.0.1:6399/0")
    redis_client.drop_client()
    try:
        decision = await rate_limit.hit("login_fail", "whoever", (1, 60))
        assert decision.allowed is True
    finally:
        monkeypatch.setattr(redis_client, "REDIS_URL", original)
        redis_client.drop_client()


async def test_token_bucket_refills_over_time(pg_db, clean_auth_store):
    """令牌桶按时间连续补充（不是固定窗口）：容量 2、1 秒补满 → 等 1.1 秒就又有额度。"""
    import asyncio
    policy = (2, 1)
    assert (await rate_limit.hit("t", "u", policy)).allowed is True
    assert (await rate_limit.hit("t", "u", policy)).allowed is True
    assert (await rate_limit.hit("t", "u", policy)).allowed is False
    await asyncio.sleep(1.1)
    assert (await rate_limit.hit("t", "u", policy)).allowed is True


# ══════════════════════════════════════════════════════════════
# B. 改密码 —— 关键：旧 access 立刻失效（token_version）
# ══════════════════════════════════════════════════════════════

async def test_change_password_requires_old_password(pg_db, clean_auth_store):
    async with _client() as c:
        me = await _signup(c, "pw1")
        r = await c.post("/auth/change-password", headers=_auth(me["access_token"]),
                         json={"old_password": "wrong", "new_password": NEW_PASSWORD})
        assert r.status_code == 401
        # 未登录 → 401
        assert (await c.post("/auth/change-password",
                             json={"old_password": PASSWORD,
                                   "new_password": NEW_PASSWORD})).status_code == 401
        # 新密码不合规 → 400；与旧密码相同 → 400
        r = await c.post("/auth/change-password", headers=_auth(me["access_token"]),
                         json={"old_password": PASSWORD, "new_password": "123"})
        assert r.status_code == 400
        r = await c.post("/auth/change-password", headers=_auth(me["access_token"]),
                         json={"old_password": PASSWORD, "new_password": PASSWORD})
        assert r.status_code == 400


async def test_change_password_revokes_old_access_and_other_sessions(pg_db, clean_auth_store):
    """改密码的核心验收：旧 access 立刻 401（不等 TTL）+ 其他设备 refresh 全废。"""
    async with _client() as c:
        # 设备 A（当前）
        a = await _signup(c, "pw2")
        # 设备 B（另一个标签页 = 另一次登录）
        b = (await c.post("/auth/login", json={
            "username": "pw2", "password": PASSWORD})).json()
        assert a["sid"] != b["sid"]
        assert (await c.get("/me", headers=_auth(b["access_token"]))).status_code == 200

        r = await c.post("/auth/change-password", headers=_auth(a["access_token"]),
                         json={"old_password": PASSWORD, "new_password": NEW_PASSWORD})
        assert r.status_code == 200, r.text
        fresh = r.json()
        assert fresh["refresh_token"] and fresh["access_token"]

        # ① 旧 access（A 和 B 的）立刻失效 —— 不是等 15 分钟
        assert (await c.get("/me", headers=_auth(a["access_token"]))).status_code == 401
        assert (await c.get("/me", headers=_auth(b["access_token"]))).status_code == 401
        # ② 其他设备的 refresh 全废
        assert (await c.post("/auth/refresh",
                             json={"refresh_token": b["refresh_token"]})).status_code == 401
        # ③ 当前设备拿到的**新**凭证可用
        assert (await c.get("/me", headers=_auth(fresh["access_token"]))).status_code == 200
        assert (await c.post("/auth/refresh",
                             json={"refresh_token": fresh["refresh_token"]})).status_code == 200


async def test_old_password_stops_working_after_change(pg_db, clean_auth_store):
    async with _client() as c:
        me = await _signup(c, "pw3")
        await c.post("/auth/change-password", headers=_auth(me["access_token"]),
                     json={"old_password": PASSWORD, "new_password": NEW_PASSWORD})
        assert (await c.post("/auth/login", json={
            "username": "pw3", "password": PASSWORD})).status_code == 401
        r = await c.post("/auth/login", json={
            "username": "pw3", "password": NEW_PASSWORD})
        assert r.status_code == 200
        # 新登录拿到的 access 版本是最新的 → 可用
        assert (await c.get("/me", headers=_auth(r.json()["access_token"]))).status_code == 200


async def test_logout_all_kills_every_session(pg_db, clean_auth_store):
    async with _client() as c:
        a = await _signup(c, "allout")
        b = (await c.post("/auth/login", json={
            "username": "allout", "password": PASSWORD})).json()

        r = await c.post("/auth/logout-all", headers=_auth(a["access_token"]))
        assert r.status_code == 200 and r.json()["revoked"] >= 2
        assert (await c.get("/me", headers=_auth(a["access_token"]))).status_code == 401
        assert (await c.post("/auth/refresh",
                             json={"refresh_token": b["refresh_token"]})).status_code == 401


# ══════════════════════════════════════════════════════════════
# B2. 封号（管理员）
# ══════════════════════════════════════════════════════════════

async def test_disable_user_kills_access_immediately_then_reenable(pg_db, clean_auth_store):
    """封号 → 已签发 access 立刻 401（token_version）+ refresh 401；解封后能重新登录。"""
    async with _client() as c:
        victim = await _signup(c, "victim")
        admin = await _signup(c, "boss")                       # 同账号签 admin token
        admin_tok = create_token(admin["user_id"], role="admin")

        # 非管理员不能调 → 403
        assert (await c.post(f"/admin/users/{victim['user_id']}/status",
                             headers=_auth(victim["access_token"]),
                             json={"status": "disabled"})).status_code == 403

        # 管理员封号
        r = await c.post(f"/admin/users/{victim['user_id']}/status",
                         headers=_auth(admin_tok), json={"status": "disabled"})
        assert r.status_code == 200 and r.json()["status"] == "disabled"

        # 立刻生效：access 401、refresh 401、再登录 401
        assert (await c.get("/me", headers=_auth(victim["access_token"]))).status_code == 401
        assert (await c.post("/auth/refresh",
                             json={"refresh_token": victim["refresh_token"]})).status_code == 401
        assert (await c.post("/auth/login", json={
            "username": "victim", "password": PASSWORD})).status_code == 401

        # 解封 → 能重新登录
        assert (await c.post(f"/admin/users/{victim['user_id']}/status",
                             headers=_auth(admin_tok),
                             json={"status": "active"})).status_code == 200
        assert (await c.post("/auth/login", json={
            "username": "victim", "password": PASSWORD})).status_code == 200


async def test_disable_user_404_and_bad_status(pg_db, clean_auth_store):
    async with _client() as c:
        admin = await _signup(c, "boss2")
        admin_tok = create_token(admin["user_id"], role="admin")
        assert (await c.post("/admin/users/nosuchuser/status", headers=_auth(admin_tok),
                             json={"status": "disabled"})).status_code == 404
        assert (await c.post(f"/admin/users/{admin['user_id']}/status",
                             headers=_auth(admin_tok),
                             json={"status": "hacked"})).status_code == 400


# ══════════════════════════════════════════════════════════════
# C. 版本校验本身（含开关与 fail-open）
# ══════════════════════════════════════════════════════════════

async def test_revocation_check_can_be_disabled(pg_db, clean_auth_store, monkeypatch):
    """AUTH_REVOCATION_CHECK=0 → 回到纯验签零查询：已撤销的 access 在 TTL 内仍可用。

    这是刻意保留的开关：把"撤销窗口"与"热路径零 IO"的取舍交给部署方决定。
    """
    async with _client() as c:
        me = await _signup(c, "switch")
        await c.post("/auth/change-password", headers=_auth(me["access_token"]),
                     json={"old_password": PASSWORD, "new_password": NEW_PASSWORD})

        monkeypatch.setattr(auth_sessions, "REVOCATION_CHECK", False)
        assert (await c.get("/me", headers=_auth(me["access_token"]))).status_code == 200


async def test_version_check_fails_open_when_store_unavailable(pg_db, clean_auth_store,
                                                               monkeypatch):
    """Redis/DB 都读不到版本 → 放行（fail-open），靠 access TTL 兜底，不当成全站 401。"""
    async with _client() as c:
        me = await _signup(c, "failopen")
        from src import redis_client
        from src.users import get_token_version

        async def _boom(_uid):
            raise RuntimeError("db down")

        original_url = redis_client.REDIS_URL
        monkeypatch.setattr(redis_client, "REDIS_URL", "redis://127.0.0.1:6399/0")
        monkeypatch.setattr("src.users.get_token_version", _boom)
        redis_client.drop_client()
        auth_sessions.reset_version_cache()
        try:
            assert (await c.get("/me", headers=_auth(me["access_token"]))).status_code == 200
        finally:
            monkeypatch.setattr(redis_client, "REDIS_URL", original_url)
            redis_client.drop_client()
            auth_sessions.reset_version_cache()


async def test_warm_version_cache_still_enforces_after_store_outage(pg_db, clean_auth_store,
                                                                   monkeypatch):
    """缓存预热过的版本，在 Redis/DB 同时抖动时**仍然能正确拒绝**旧 token。

    这条区分了两种行为：fail-open 只适用于"版本未知"；"已知版本"必须继续生效
    —— 否则攻击者只要把 Redis 打挂，就能让封号失效。
    """
    from src import redis_client
    from src.users import bump_token_version, create_user

    pub = await create_user("warmcache", PASSWORD, "")
    await bump_token_version(pub["user_id"])            # 版本 0 → 1（模拟改密/封号）
    assert await auth_sessions.current_token_version(pub["user_id"]) == 1    # 预热

    async def _boom(_uid):
        raise RuntimeError("db down")

    original_url = redis_client.REDIS_URL
    monkeypatch.setattr(redis_client, "REDIS_URL", "redis://127.0.0.1:6399/0")
    monkeypatch.setattr("src.users.get_token_version", _boom)
    redis_client.drop_client()
    try:
        assert await auth_sessions.current_token_version(pub["user_id"]) == 1   # 走本地缓存
        async with _client() as c:
            old = create_token(pub["user_id"], role="customer", ver=0)
            assert (await c.get("/me", headers=_auth(old))).status_code == 401
    finally:
        monkeypatch.setattr(redis_client, "REDIS_URL", original_url)
        redis_client.drop_client()
        auth_sessions.reset_version_cache()


async def test_unknown_version_is_cached_briefly(pg_db, clean_auth_store, monkeypatch):
    """读不到版本时的"未知"标记要缓存几秒：否则故障期间每个请求都重试 Redis+DB。

    这是"故障放大"防护 —— 鉴权失败已经 fail-open 了，别再让它变成 DB 打点机。
    """
    calls = {"n": 0}

    async def _boom(_uid):
        calls["n"] += 1
        raise RuntimeError("db down")

    monkeypatch.setattr("src.users.get_token_version", _boom)
    from src import redis_client
    original = redis_client.REDIS_URL
    monkeypatch.setattr(redis_client, "REDIS_URL", "redis://127.0.0.1:6399/0")
    redis_client.drop_client()
    auth_sessions.reset_version_cache()
    try:
        assert await auth_sessions.current_token_version("whoever") == -1
        assert await auth_sessions.current_token_version("whoever") == -1
        assert calls["n"] == 1, "第二次应命中'未知'缓存，不该再打 DB"
    finally:
        monkeypatch.setattr(redis_client, "REDIS_URL", original)
        redis_client.drop_client()
        auth_sessions.reset_version_cache()


# ══════════════════════════════════════════════════════════════
# D. 业务接口按 user_id 限流（/chat、/chat/stream）
# ══════════════════════════════════════════════════════════════

async def test_chat_rate_limited_by_user_id(pg_db, clean_auth_store, monkeypatch):
    """`/chat` 按 user_id 限流，且**在跑图之前**就拒绝。

    为什么这是"按用户"而不是"按 IP"：/chat 已经从 JWT 拿到 user_id，
    用身份做桶才准（换 IP 绕不过、NAT 下也不误伤同事）。

    测试技巧：先把桶打空，再打接口 —— 这样断言 429 时**根本没有走到 LLM 调用**，
    如果限流写在跑图之后，这个用例会直接炸（说明位置错了）。
    """
    monkeypatch.setattr(rate_limit, "CHAT_USER", (2, 60))

    async with _client() as c:
        me = await _signup(c, "chatter")
        uid = me["user_id"]

        # 打空该用户的桶
        for _ in range(2):
            assert (await rate_limit.hit("chat_user", uid, rate_limit.CHAT_USER)).allowed is True

        r = await c.post("/chat", headers=_auth(me["access_token"]), json={"message": "你好"})
        assert r.status_code == 429
        assert "频繁" in r.json()["detail"]
        assert int(r.headers["Retry-After"]) >= 1


async def test_chat_stream_rate_limit_is_http_429_not_sse_event(pg_db, clean_auth_store,
                                                                monkeypatch):
    """SSE 端点被限流时必须是**真正的 HTTP 429**，而不是流里的一个 error 事件。

    这是 SSE 的经典坑：一旦开始返回流，状态码就锁死 200，前端再也拿不到 429。
    """
    monkeypatch.setattr(rate_limit, "CHAT_USER", (1, 60))

    async with _client() as c:
        me = await _signup(c, "streamer")
        await rate_limit.hit("chat_user", me["user_id"], rate_limit.CHAT_USER)  # 打空

        r = await c.post("/chat/stream", headers=_auth(me["access_token"]),
                         json={"message": "你好", "session_id": ""})
        assert r.status_code == 429, "限流必须在建立流之前生效"
        assert "text/event-stream" not in r.headers.get("content-type", "")
        assert int(r.headers["Retry-After"]) >= 1


async def test_chat_quota_is_per_user_not_per_ip(pg_db, clean_auth_store, monkeypatch):
    """两个用户（同 IP，测试里就是同一个对端）各自独立计账：A 刷爆不影响 B。"""
    monkeypatch.setattr(rate_limit, "CHAT_USER", (2, 60))

    async with _client() as c:
        a = await _signup(c, "alice_chat")
        b = await _signup(c, "bob_chat")
        for _ in range(2):
            await rate_limit.hit("chat_user", a["user_id"], rate_limit.CHAT_USER)

        assert (await c.post("/chat", headers=_auth(a["access_token"]),
                             json={"message": "hi"})).status_code == 429
        # B 的桶没被动过 → 不该是 429（这里没配 LLM key，会走别的失败路径，
        # 所以只断言"不是限流拒绝"）
        rb = await c.post("/chat", headers=_auth(b["access_token"]), json={"message": "hi"})
        assert rb.status_code != 429


# ══════════════════════════════════════════════════════════════
# E. 老库未迁移的容错（真实踩过的坑）
# ══════════════════════════════════════════════════════════════

async def test_login_works_when_token_version_column_missing(pg_db, clean_auth_store,
                                                             monkeypatch):
    """数据库没迁移（缺 users.token_version）时，登录不能 500。

    真实事故：直接 `uvicorn app:app` 起服务、没跑建表脚本，而老库是加列之前建的
    → 登录路径直接查 token_version → UndefinedColumnError → 前端"请求失败 500"。
    语义上"缺列"等于"存量行全是 0"，等价于迁移后的状态，所以按 0 处理 + 告警即可。
    """
    from sqlalchemy.exc import ProgrammingError

    from src import users as users_mod

    async def _missing_column(*args, **kwargs):
        raise ProgrammingError("SELECT token_version FROM users", {}, Exception(
            'column "token_version" does not exist'))

    monkeypatch.setattr(users_mod, "_schema_warned", False)
    monkeypatch.setattr(users_mod, "query_one", _missing_column)

    assert await users_mod.get_token_version("any_uid") == 0

    # 其它 SQL 错误不该被吞掉
    async def _other_error(*args, **kwargs):
        raise ProgrammingError("SELECT 1", {}, Exception("syntax error"))

    monkeypatch.setattr(users_mod, "query_one", _other_error)
    with pytest.raises(ProgrammingError):
        await users_mod.get_token_version("any_uid")
