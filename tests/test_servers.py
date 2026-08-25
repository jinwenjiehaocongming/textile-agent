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


def test_concurrent_create_order(tmp_orders_db):
    """P0-3 验收：并发写入不报 database is locked（WAL + busy_timeout 生效）。

    8 线程 × 10 单并发写同一 orders.db，要求：
    1. 全部成功（无"失败"文案）；
    2. 订单号不撞 UNIQUE（随机后缀）；
    3. 行数与写次数一致。
    """
    from concurrent.futures import ThreadPoolExecutor

    def worker(i: int) -> str:
        return create_order(
            customer_id=f"c{i % 5}",
            product_id="P001",
            product_name="T400 复合弹力布",
            color="黑色",
            quantity=100,
            unit_price=13.2,
        )

    N_WORKERS, N_EACH = 8, 10
    with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
        results = list(ex.map(worker, range(N_WORKERS * N_EACH)))

    assert all("订单已生成" in r for r in results), \
        f"{sum('失败' in r for r in results)}/{len(results)} 单失败"
    cnt = sqlite3.connect(str(tmp_orders_db)).execute(
        "SELECT COUNT(*) FROM orders"
    ).fetchone()[0]
    assert cnt == N_WORKERS * N_EACH
    # 订单号唯一性
    rows = sqlite3.connect(str(tmp_orders_db)).execute(
        "SELECT order_no, COUNT(*) c FROM orders GROUP BY order_no HAVING c > 1"
    ).fetchall()
    assert rows == []
