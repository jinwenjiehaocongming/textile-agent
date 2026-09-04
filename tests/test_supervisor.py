"""Supervisor 对话延续判断测试（纯规则，不触发 LLM）"""
import asyncio

from langchain_core.messages import AIMessage, HumanMessage

from src.agent import _detect_continuation, _is_order_completed, order_agent_node, supervisor_node


def test_continuation_when_ai_asks_and_user_short():
    messages = [
        AIMessage(content="请问您的收货地址是？"),
        HumanMessage(content="杭州"),
    ]
    assert _detect_continuation(messages) is True


def test_no_continuation_after_order_done():
    messages = [
        AIMessage(content="订单已生成 ORD-123456"),
        HumanMessage(content="你好"),
    ]
    assert _detect_continuation(messages) is False


def test_no_continuation_on_greeting():
    messages = [
        AIMessage(content="请问还有什么可以帮您？"),
        HumanMessage(content="谢谢"),
    ]
    assert _detect_continuation(messages) is False


# ── Layer 0.5：下单流程中的确认词无条件延续（2026-09 修复）──

async def test_place_order_confirm_continues_to_order():
    """上一轮下单 Agent 只查价（无确认单关键词），客户说"确认" → 仍延续下单。"""
    state = {
        "messages": [AIMessage(content="已查到价格：¥12.7/米"), HumanMessage(content="确认")],
        "query_type": "place_order",
    }
    result = await supervisor_node(state)
    assert result["query_type"] == "place_order"


async def test_place_order_ok_word_continues_to_order():
    state = {
        "messages": [AIMessage(content="请补充收货地址"), HumanMessage(content="可以")],
        "query_type": "place_order",
    }
    result = await supervisor_node(state)
    assert result["query_type"] == "place_order"


async def test_place_order_long_sentence_not_shortcut(monkeypatch):
    """确认词但消息过长（不是短确认）→ 不走 Layer 0.5，交给后续判断（mock LLM 判 sales）。"""
    class FakeLLM:
        async def ainvoke(self, messages):
            return AIMessage(content="sales")
    monkeypatch.setattr("src.agent.get_cheap_llm", lambda: FakeLLM())
    state = {
        "messages": [AIMessage(content="已查到价格"), HumanMessage(content="确认一下这个面料还有库存吗")],
        "query_type": "place_order",
    }
    result = await supervisor_node(state)
    assert result["query_type"] == "chat"  # 未被 0.5 短路，LLM 判定为售前


# ── order_completed 标记：以工具结果为准，不扫 LLM 文本（2026-09 修复）──

async def test_order_node_completed_flag_sets_chat(monkeypatch):
    """create_order 真实成功（标记挂载）→ query_type 切回售前。"""
    async def fake_order_agent(messages, customer_id):
        msg = AIMessage(content="✅ 订单已生成！\n订单号：ORD-20260824-1658089312341234")
        msg.additional_kwargs = {"order_completed": True}
        return msg
    monkeypatch.setattr("src.agent.order_agent", fake_order_agent)
    state = {"messages": [HumanMessage(content="确认")], "query_type": "place_order", "user_id": "t"}
    result = await order_agent_node(state)
    assert result["query_type"] == "chat"


async def test_order_node_real_order_no_in_text_keeps_place_order(monkeypatch):
    """LLM 回复复述历史中的真实订单号（但本轮未真实下单、无标记）
    → query_type 不得被重置（修复前会被 _is_order_completed 误判为完成）。"""
    async def fake_order_agent(messages, customer_id):
        return AIMessage(content="为您查到 T400 黑色，另外您之前的订单 ORD-20260824-1658089312341234 已付款。")
    monkeypatch.setattr("src.agent.order_agent", fake_order_agent)
    state = {"messages": [HumanMessage(content="T400 黑色 3000米 帮我下单")], "query_type": "place_order", "user_id": "t"}
    result = await order_agent_node(state)
    assert "query_type" not in result or result["query_type"] == "place_order"


async def test_order_node_no_flag_keeps_place_order(monkeypatch):
    """仅展示产品/确认单（无 create_order）→ 保持下单流程。"""
    async def fake_order_agent(messages, customer_id):
        return AIMessage(content="📋 订单确认单\n请确认以上信息是否正确？回复「确认」即可下单。")
    monkeypatch.setattr("src.agent.order_agent", fake_order_agent)
    state = {"messages": [HumanMessage(content="可以")], "query_type": "place_order", "user_id": "t"}
    result = await order_agent_node(state)
    assert "query_type" not in result or result["query_type"] == "place_order"


# ── _is_order_completed：文本兜底判断（不再用于 query_type 判定）──

def test_order_completed_real_format():
    """真实订单号（ORD-YYYYMMDD-时分秒6+微秒6+随机4，共16位数字）→ 判定完成。"""
    assert _is_order_completed("✅ 订单已生成！\n订单号：ORD-20260824-1658089312341234") is True
    assert _is_order_completed("订单号：ORD-20260824-1658089312341234，状态待付款") is True


def test_order_completed_fake_format_not_triggered():
    """LLM 编造/复述的假订单号（后段过短或含字母）→ 不判定完成，保持下单流程。"""
    assert _is_order_completed("您之前的订单 ORD-20260822-0381 已发货") is False
    assert _is_order_completed("订单 ORD-20260822-T400 待确认") is False
    assert _is_order_completed("请确认订单信息，订单号将在审批通过后生成") is False
    assert _is_order_completed("📋 订单确认单\n请确认以上信息是否正确？") is False
