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
