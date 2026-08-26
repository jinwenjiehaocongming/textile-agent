"""用户记忆测试（异步 · PostgreSQL + 单库多租户）"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src.memory import get_user


@pytest.fixture
async def user(pg_memory):
    return get_user("u_test_1")


async def test_save_and_load(user):
    await user.save_messages([HumanMessage(content="你好"), AIMessage(content="您好！")])
    msgs = await user.load_recent(10)
    assert [m.content for m in msgs] == ["你好", "您好！"]


async def test_clear_history(user):
    await user.save_messages([HumanMessage(content="x")])
    await user.clear_history()
    assert await user.load_recent(10) == []


async def test_query_type_persistence(user):
    assert await user.get_last_query_type() == "chat"
    await user.save_last_query_type("after_sales")
    assert await user.get_last_query_type() == "after_sales"


async def test_load_recent_empty(user):
    assert await user.load_recent(10) == []


async def test_user_isolation(pg_memory):
    """多租户行级隔离：不同 user_id 互不可见历史与 profile。"""
    a = get_user("iso_a")
    b = get_user("iso_b")
    await a.save_messages([HumanMessage(content="A的秘密")])
    await b.save_messages([HumanMessage(content="B的秘密")])
    assert [m.content for m in await a.load_recent(10)] == ["A的秘密"]
    assert [m.content for m in await b.load_recent(10)] == ["B的秘密"]
    await a.save_last_query_type("place_order")
    assert await b.get_last_query_type() == "chat"


def test_invalid_user_id_downgraded(monkeypatch):
    import src.memory as memory
    from src.user_identity import is_valid_user_id
    assert is_valid_user_id("ok_user_1")
    assert not is_valid_user_id("../../etc")
    assert memory.sanitize_user_id("../../etc") == "guest"
