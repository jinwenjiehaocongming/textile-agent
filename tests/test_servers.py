"""MCP Server 业务函数测试（异步 · PostgreSQL）

直接测 FastMCP 注册的 async 业务函数（绕过协议层），
覆盖：下单写库 / 查单回环 / 退款工单 / 产品搜索 / 并发下单。
"""
import asyncio

from src.mcp_servers.order_server import create_order, query_order_status
from src.mcp_servers.product_server import search_product
from src.mcp_servers.refund_server import create_refund


def _make_order(customer_id="123456") -> str:
    """返回 (await 用的) coroutine-friendly 调用——直接调 async 函数。"""


async def test_create_order_writes_db(pg_db):
    result = await create_order(
        customer_id="123456", product_id="P001", product_name="T400 复合弹力布",
        color="黑色", quantity=100, unit_price=13.2,
        phone="13800000000", address="杭州", delivery_date="明天",
    )
    assert "✅ 订单已生成" in result
    assert "ORD-" in result


async def test_query_order_not_found(pg_db):
    result = await query_order_status("ORD-不存在")
    assert "未找到订单" in result


async def test_query_order_roundtrip(pg_db):
    await create_order(
        customer_id="1", product_id="P001", product_name="T400",
        color="黑", quantity=10, unit_price=13.2,
    )
    # 查出最近一单回环验证
    from src.db import query_one
    row = await query_one("SELECT order_no FROM orders ORDER BY id DESC LIMIT 1")
    assert row, "订单应已写入 PG"
    result = await query_order_status(row["order_no"])
    assert "T400" in result


async def test_create_refund(pg_db):
    result = await create_refund(order_no="ORD-20260101-00000000000000001234", reason="纬斜超标")
    assert "✅ 退款工单已生成" in result
    from src.db import query_one
    row = await query_one("SELECT * FROM refunds ORDER BY id DESC LIMIT 1")
    assert row and row["status"] == "待审核"


async def test_search_product_found(pg_db):
    result = await search_product("T400")
    assert "T400 复合弹力布" in result


async def test_search_product_no_match(pg_db):
    result = await search_product("不存在的面料昵称xyz")
    assert "未找到匹配产品" in result


async def test_concurrent_create_order(pg_db):
    """并发 10 单：订单号不撞 UNIQUE 约束（时间戳+微秒+随机）。"""

    async def _one(i):
        return await create_order(
            customer_id=f"c{i}", product_id="P001", product_name="T400",
            color="黑", quantity=i + 1, unit_price=1.0,
        )

    results = await asyncio.gather(*[_one(i) for i in range(10)])
    assert all("✅ 订单已生成" in r for r in results)
    from src.db import query_all
    rows = await query_all("SELECT order_no FROM orders GROUP BY order_no HAVING COUNT(*) > 1")
    assert rows == [], "订单号必须唯一"
