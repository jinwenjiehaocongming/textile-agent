"""订单人工审批（HITL）测试

- 审批注册表纯逻辑
- 合成 interrupt 图：验证本项目使用的 0.6.x 语义
  （invoke 返回 __interrupt__ → get_state 可见 → Command(resume=...) 恢复）
- build_graph 冒烟：编译带 checkpointer，thread 配置形状正确
（不触发真实 LLM）
"""
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from src.approval import (
    find_pending_draft, get_pending, list_pending,
    pending_reply_text, register_pending, remove_pending,
)


# ── 注册表 ────────────────────────────────────────────────

def test_registry_roundtrip():
    register_pending("u1", "u1", {"product_name": "T400", "quantity": 200})
    try:
        assert get_pending("u1")["draft"]["quantity"] == 200
        items = list_pending()
        assert items and items[0]["thread_id"] == "u1"
        assert get_pending("nope") is None
    finally:
        remove_pending("u1")
    assert get_pending("u1") is None


def test_find_pending_draft():
    class Fake:
        def __init__(self, v):
            self.value = v

    assert find_pending_draft(None) is None
    assert find_pending_draft([Fake({"type": "other"})]) is None
    draft = find_pending_draft([
        Fake({"type": "other"}),
        {"type": "order_approval", "draft": {"qty": 5}},
    ])
    assert draft == {"qty": 5}


def test_pending_reply_text():
    text = pending_reply_text({
        "product_name": "T400", "product_id": "P0075", "color": "黑色",
        "quantity": 200, "unit_price": "12.7", "total": "2540.0",
        "phone": "138", "address": "杭州", "delivery_date": "下周",
    })
    assert "已提交人工审批" in text
    assert "T400" in text and "2540.0" in text


# ── 合成 interrupt 图（语义与本项目下单路径一致）────────

def _make_hitl_graph():
    class S(dict):
        msgs: list

    def node_appr(s):
        decision = interrupt({"type": "order_approval", "draft": {"qty": 100}})
        return {"msgs": s["msgs"] + [f"decided={decision}"]}

    g = StateGraph(S)
    g.add_node("appr", node_appr)
    g.set_entry_point("appr")
    g.add_edge("appr", END)
    return g.compile(checkpointer=MemorySaver())


async def test_hitl_invoke_interrupt_then_resume():
    graph = _make_hitl_graph()
    cfg = {"configurable": {"thread_id": "t-hitl"}}

    # 首次：invoke 返回 partial state，带 __interrupt__，不抛异常
    first = await graph.ainvoke({"msgs": []}, cfg)
    draft = find_pending_draft(first.get("__interrupt__"))
    assert draft == {"qty": 100}

    # 挂起态可见
    snap = graph.get_state(cfg)
    assert snap.next  # 有节点在等 resume
    assert find_pending_draft(snap.interrupts) == {"qty": 100}

    # 恢复：Command(resume=...) 走完
    resume = await graph.ainvoke(Command(resume={"approved": True}), cfg)
    assert resume["msgs"] == ["decided={'approved': True}"]
    assert not graph.get_state(cfg).next  # 已结束


async def test_hitl_reject_path():
    graph = _make_hitl_graph()
    cfg = {"configurable": {"thread_id": "t-hitl-rej"}}
    await graph.ainvoke({"msgs": []}, cfg)
    resume = await graph.ainvoke(Command(resume={"approved": False, "reason": "库存不足"}), cfg)
    assert resume["msgs"] == ["decided={'approved': False, 'reason': '库存不足'}"]


# ── build_graph 冒烟（不触发 LLM）────────────────────────

def test_build_graph_compiles_with_checkpointer():
    from src.agent import build_graph, thread_config
    graph = build_graph()
    snap = graph.get_state(thread_config("smoke_user"))
    assert graph is not None
    assert not snap.next  # 新线程无中断