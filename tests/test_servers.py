"""MCP Server 工具函数测试（纯 SQLite，不依赖 LLM/网络）"""
import sqlite3

from src.mcp_servers.order_server import create_order, query_order
from src.mcp_servers.product_server import search_product
from src.mcp_servers.refund_server import create_refund


def _make_order(customer_id="123456"):
    return create_order(
        customer_id=customer_id,
        product_id="P001",
        product_name="T400 复合弹力布",
        color="黑色",
        quantity=100,
        unit_price=13.2,
        phone="13800000000",
        address="杭州",
        delivery_date="明天",
    )


def _latest_order_no(db):
    return sqlite3.connect(str(db)).execute(
        "SELECT order_no FROM orders ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]


def test_create_order_writes_db(tmp_orders_db):
    result = _make_order()
    assert "订单已生成" in result
    assert "ORD-" in result

    row = sqlite3.connect(str(tmp_orders_db)).execute(
        "SELECT customer_id, product_id, quantity, status FROM orders"
    ).fetchone()
    assert row == ("123456", "P001", 100, "待付款")


def test_query_order_not_found(tmp_orders_db):
    assert "未找到订单" in query_order("ORD-NOTEXIST")


def test_query_order_roundtrip(tmp_orders_db):
    _make_order()
    order_no = _latest_order_no(tmp_orders_db)
    result = query_order(order_no)
    assert order_no in result
    assert "T400 复合弹力布" in result


def test_create_refund(tmp_orders_db):
    _make_order()
    order_no = _latest_order_no(tmp_orders_db)
    result = create_refund(order_no, "色差超标")
    assert "退款工单已生成" in result

    row = sqlite3.connect(str(tmp_orders_db)).execute(
        "SELECT order_no, reason, status FROM refunds"
    ).fetchone()
    assert row == (order_no, "色差超标", "待审核")


def test_search_product_found(tmp_products_db):
    result = search_product("T400")
    assert "P001" in result
    assert "T400" in result


def test_search_product_no_match(tmp_products_db):
    assert "未找到匹配产品" in search_product("不存在的面料xyz")
