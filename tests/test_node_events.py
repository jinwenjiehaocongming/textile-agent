"""图节点事件描述测试（纯函数）"""
from langchain_core.messages import AIMessage, ToolMessage

from src.node_events import NODE_LABELS, describe_node


def test_labels():
    assert NODE_LABELS["query_reformulator"] == "改写查询"
    assert NODE_LABELS["tool_executor"] == "工具执行"
    assert NODE_LABELS["review"] == "安全审核"


def test_supervisor_routes():
    assert describe_node("supervisor", {"query_type": "place_order"}) == "→ 下单"
    assert describe_node("supervisor", {"query_type": "chat"}) == "→ 售前"


def test_retriever_counts():
    assert describe_node("context_retriever",
                         {"knowledge_chunks": ["a", "b", "c"]}) == "命中 3 条知识"
    assert describe_node("context_retriever", {"knowledge_chunks": []}) == "闲聊，跳过检索"


def test_agent_tool_calls_visible():
    ai = AIMessage(content="", tool_calls=[{
        "name": "search_product", "args": {"query": "T400"}, "id": "call-1",
    }])
    detail = describe_node("agent", {"messages": [ai]})
    assert detail == "调用 search_product"


def test_tool_executor_names_by_call_id():
    ai = AIMessage(content="", tool_calls=[
        {"name": "search_product", "args": {}, "id": "call-1"},
        {"name": "create_order", "args": {}, "id": "call-2"},
    ])
    tm1 = ToolMessage(content="ok", tool_call_id="call-1")
    tm2 = ToolMessage(content="ok", tool_call_id="call-2")
    detail = describe_node("tool_executor", {"messages": [ai, tm1, tm2]})
    assert detail == "执行 search_product、create_order"


def test_unknown_node_returns_empty():
    assert describe_node("some_node", {}) == ""