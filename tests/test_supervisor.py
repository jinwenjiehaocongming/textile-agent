"""Supervisor 对话延续判断测试（纯规则，不触发 LLM）"""
from langchain_core.messages import AIMessage, HumanMessage

from src.agent import _detect_continuation


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
