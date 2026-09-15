"""L1 热缓存（Redis）一致性与容错测试
======================================
锁定 2026-09 修复的四个缓存 bug（清单第 8 条）：

1. ``load_recent`` 命中即返回、不补齐 —— 缓存比 PG 短 → 模型"偶发失忆"
2. 删会话只删库不清缓存 —— 删掉的消息在 TTL 内"复活"
3. ``clear_history`` 清库是全量、清缓存只清 default —— 其他会话残留
4. 缓存写失败连累落库 —— 缓存故障升级成"数据丢失"

外加：Redis 不可用（读抛异常）时必须回源 PG —— 降级可以慢，不能答错。

**设计原则（修复后的不变式）**：PG 是唯一真相来源；L1 只在"装得下本次请求"
时短路；任何 Redis 异常一律 fail-open（当 miss 处理），绝不向上抛。
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage

import src.memory as memory
from src.db import query_all
from src import sessions as sessions_mod
from src.memory import get_user


async def _reset_l1() -> None:
    """清空 L1（Redis + 进程内 dict），模拟"缓存冷启动"。

    新实现提供 ``memory.reset_cache()``；旧实现没有，退回手工清理
    （这样本文件在修复前也能跑出真实的红）。
    """
    reset = getattr(memory, "reset_cache", None)
    if reset is not None:
        await reset()
        return
    dict_cache = getattr(memory, "_dict_cache", None)
    if dict_cache is not None:
        dict_cache.clear()
    r = getattr(memory, "_r", None)
    if r is not None:
        keys = r.keys("*")
        if keys:
            r.delete(*keys)


@pytest.fixture(autouse=True)
async def _clean_l1(pg_memory):
    """每个用例前后都清空 L1 —— PG 被 reset_schema 清空时，缓存必须一起清，
    否则上个用例/上轮运行的缓存会变成"第二个真相来源"（这正是 bug 1/2/3 的成因）。"""
    await _reset_l1()
    yield
    await _reset_l1()


# ── bug 1：缓存比 PG 短时不能短路 ────────────────────────────────

async def test_load_recent_returns_full_window_when_cache_too_short():
    """先小 n 后大 n：修复前第二次只拿到缓存里的 4 条（PG 明明有 12 条）。"""
    user = get_user("cache_short")
    await user.save_messages([HumanMessage(content=f"m{i}") for i in range(12)])

    await _reset_l1()  # 模拟 Redis 重启 / TTL 刚过期
    assert len(await user.load_recent(4)) == 4      # 这次回源 PG 并回填缓存
    assert len(await user.load_recent(8)) == 8      # 缓存装不下 8 条 → 必须再回源


async def test_cache_hit_returns_latest_n_in_chronological_order():
    """缓存命中时也要取"最近 n 条"且保持正序（复习：lrange -n -1）。"""
    user = get_user("cache_order")
    await user.save_messages([HumanMessage(content=f"m{i}") for i in range(12)])
    await _reset_l1()
    await user.load_recent(12)  # 预热整窗

    assert [m.content for m in await user.load_recent(3)] == ["m9", "m10", "m11"]


# ── bug 2：删会话必须失效缓存 ────────────────────────────────────

async def test_deleted_session_does_not_resurrect_from_cache():
    """会话删除后，即使缓存还热，也必须读不到（修复前 TTL 内会"复活"）。"""
    uid = "cache_del"
    sess = await sessions_mod.create_session(uid, "临时会话")
    sid = sess["session_id"]
    user = get_user(uid)
    await user.save_messages([HumanMessage(content="要被删掉的话")], sid)

    # 预热缓存（这一步让修复前的实现在删除后仍能命中）
    assert [m.content for m in await user.load_recent(5, sid)] == ["要被删掉的话"]

    assert await sessions_mod.delete_session(uid, sid) is True
    assert await user.load_recent(5, sid) == []


# ── bug 3：clear_history 的库/缓存范围必须一致 ────────────────────

async def test_clear_history_invalidates_every_session_of_user():
    """clear_history() 不传 session → PG 删全部会话，缓存也必须全部失效。"""
    user = get_user("cache_clear_all")
    await user.save_messages([HumanMessage(content="会话A")], "sess_a")
    await user.save_messages([HumanMessage(content="会话B")], "sess_b")
    assert await user.load_recent(5, "sess_a")  # 两个会话都预热
    assert await user.load_recent(5, "sess_b")

    await user.clear_history()

    assert await user.load_recent(5, "sess_a") == []
    assert await user.load_recent(5, "sess_b") == []


async def test_clear_history_scoped_to_one_session():
    """传了 session → 只清这个会话（库与缓存都不能误伤别的会话）。"""
    user = get_user("cache_clear_one")
    await user.save_messages([HumanMessage(content="A 的话")], "sess_only_a")
    await user.save_messages([HumanMessage(content="B 的话")], "sess_only_b")
    assert await user.load_recent(5, "sess_only_a")
    assert await user.load_recent(5, "sess_only_b")

    await user.clear_history("sess_only_a")

    assert await user.load_recent(5, "sess_only_a") == []
    assert [m.content for m in await user.load_recent(5, "sess_only_b")] == ["B 的话"]


# ── bug 4：缓存异常不能影响正确性与落库 ──────────────────────────

async def test_cache_write_failure_still_persists_to_pg(monkeypatch):
    """Redis 写失败时 PG 仍必须落库（修复前：先写缓存、异常直接抛出 → 连库都没写）。

    故障同时打在旧实现（``_cache_append``，同步）与新实现（``_cache_append_many``，异步）
    的 seam 上，这样本用例在修复前是真红、修复后是真好。
    """
    def _boom(*args, **kwargs):
        raise RuntimeError("redis down (write)")

    async def _aboom(*args, **kwargs):
        raise RuntimeError("redis down (write)")

    monkeypatch.setattr(memory, "_cache_append", _boom, raising=False)
    monkeypatch.setattr(memory, "_cache_append_many", _aboom, raising=False)

    uid = "cache_write_fail"
    await get_user(uid).save_messages([HumanMessage(content="这条不能丢")])

    rows = await query_all(
        "SELECT content FROM conversations WHERE user_id = :uid", {"uid": uid})
    assert [r["content"] for r in rows] == ["这条不能丢"]


async def test_cache_read_failure_falls_back_to_pg(monkeypatch):
    """Redis 读失败 → 当作 miss 回源 PG（降级可以慢，不能答错/报错）。"""
    uid = "cache_read_fail"
    user = get_user(uid)
    await user.save_messages([HumanMessage(content="历史还在")])
    await _reset_l1()

    def _boom(*args, **kwargs):
        raise RuntimeError("redis down (read)")

    async def _aboom(*args, **kwargs):
        raise RuntimeError("redis down (read)")

    monkeypatch.setattr(memory, "_cache_load_recent", _boom, raising=False)
    monkeypatch.setattr(memory, "_cache_get", _aboom, raising=False)

    assert [m.content for m in await user.load_recent(5)] == ["历史还在"]


async def test_cache_clear_failure_does_not_break_delete(monkeypatch):
    """清缓存失败不能把"删历史"这个业务动作打断（库已删就是成功）。"""
    uid = "cache_clear_fail"
    user = get_user(uid)
    await user.save_messages([HumanMessage(content="待删")])

    def _boom(*args, **kwargs):
        raise RuntimeError("redis down (del)")

    async def _aboom(*args, **kwargs):
        raise RuntimeError("redis down (del)")

    monkeypatch.setattr(memory, "_cache_clear", _boom, raising=False)
    monkeypatch.setattr(memory, "_cache_clear_user", _aboom, raising=False)

    await user.clear_history()  # 不应抛异常

    rows = await query_all(
        "SELECT content FROM conversations WHERE user_id = :uid", {"uid": uid})
    assert rows == []


# ── 回归：既有语义不能被缓存改造破坏 ─────────────────────────────

async def test_multi_turn_history_roundtrip():
    user = get_user("cache_roundtrip")
    await user.save_messages([HumanMessage(content="你好"), AIMessage(content="您好！")])
    await _reset_l1()  # 强制走 PG 回源 + 回填
    assert [m.content for m in await user.load_recent(10)] == ["你好", "您好！"]
    # 再读一次（此时走缓存命中）结果必须一致
    assert [m.content for m in await user.load_recent(10)] == ["你好", "您好！"]
