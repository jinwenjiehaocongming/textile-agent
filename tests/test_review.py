"""审核规则层 + 闲聊判断测试（只测确定性逻辑，不触发 LLM）"""
from src.agent import review_response, should_skip_retrieval


def test_review_blocks_cost_price():
    r = review_response("我们的成本价是每米5元")
    assert r["safe"] is False
    assert "成本价" in r["reason"]


def test_review_allows_polite_refusal():
    # 客服正确拒绝透露成本价 → 规则层应放行
    r = review_response("抱歉，成本价不方便提供")
    assert r["safe"] is True


def test_should_skip_greeting():
    assert should_skip_retrieval("你好") is True


def test_should_not_skip_product_query():
    assert should_skip_retrieval("T400多少钱") is False
