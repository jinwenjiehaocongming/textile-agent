"""用户记忆测试（SQLite 持久化 + 热缓存，不涉及 ChromaDB 偏好）"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage

import src.memory as memory
from src.memory import UserMemory


@pytest.fixture(autouse=True)
def _isolate_memory(monkeypatch):
    """强制用 dict 热缓存（避免依赖本机 Redis），并清空全局缓存"""
    monkeypatch.setattr(memory, "_use_redis", False)
    memory._dict_cache.clear()


def test_save_and_load(tmp_user_dir):
    mem = UserMemory("u1")
    mem.save_messages([HumanMessage(content="你好"), AIMessage(content="您好")])
    rows = mem.load_recent(10)
    assert [m.content for m in rows] == ["你好", "您好"]


def test_clear_history(tmp_user_dir):
    mem = UserMemory("u2")
    mem.save_messages([HumanMessage(content="你好")])
    mem.clear_history()
    assert mem.load_recent(10) == []


def test_query_type_persistence(tmp_user_dir):
    mem = UserMemory("u3")
    mem.save_last_query_type("place_order")
    assert mem.get_last_query_type() == "place_order"


def test_load_recent_empty(tmp_user_dir):
    mem = UserMemory("u4")
    assert mem.load_recent(10) == []


def test_user_isolation(tmp_user_dir):
    """P0-1 验收：两个 user_id 的历史/状态互不串（Web 层按 X-User-Id 取 get_user）"""
    from src.memory import get_user

    alice = get_user("alice")
    bob = get_user("bob")
    alice.save_messages([HumanMessage(content="alice 的询价"), AIMessage(content="alice 的报价")])
    alice.save_last_query_type("place_order")

    # bob 看不到 alice 的任何历史/状态
    assert bob.load_recent(10) == []
    assert bob.get_last_query_type() == "chat"
    # alice 自己的数据完好
    assert [m.content for m in alice.load_recent(10)] == ["alice 的询价", "alice 的报价"]
    assert alice.get_last_query_type() == "place_order"


def test_invalid_user_id_downgraded(tmp_user_dir, monkeypatch):
    """防御兜底：恶意 ID 不会拼进文件路径，而是降级为 guest"""
    import src.memory as memory
    monkeypatch.setattr(memory, "_use_redis", False)
    from src.memory import get_user

    evil = get_user("../../etc/passwd")
    assert evil.user_id == "guest"
    evil2 = get_user("u_ok-1")
    assert evil2.user_id == "u_ok-1"
