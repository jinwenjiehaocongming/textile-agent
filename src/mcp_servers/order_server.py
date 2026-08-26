"""订单管理 MCP Server（FastMCP · async）

企业级演进（2026-08）：FastMCP 协议 + PostgreSQL 异步存储。
create_order 业务语义保持不变（订单号唯一性：时间戳+微秒+随机，避免并发撞 UNIQUE）。
"""
import random
import sys
from datetime import datetime

from mcp.server.fastmcp import FastMCP

from src.db import execute, query_one
from src.logging_config import get_logger
_logger = get_logger(__name__)

mcp = FastMCP("order-server")


@mcp.tool()
async def query_order_status(order_no: str) -> str:
    """查询订单状态。客户提供了订单号（ORD- 开头）时使用。"""
    row = await query_one("SELECT * FROM orders WHERE order_no = :order_no", {"order_no": order_no})
    if not row:
        return f"未找到订单 {order_no}"
    return (
        f"订单号：{row['order_no']}\n"
        f"产品：{row['product_name']} | {row['color']}\n"
        f"数量：{row['quantity']}米 | ¥{row['unit_price']}/米 | 总价：¥{row['total']}\n"
        f"状态：{row['status']}\n"
        f"下单时间：{row['created_at'][:16]}"
    )


@mcp.tool()
async def create_order(
    customer_id: str,
    product_id: str,
    product_name: str,
    color: str,
    quantity: int,
    unit_price: float,
    phone: str = "",
    address: str = "",
    delivery_date: str = "",
) -> str:
    """为客户创建面料采购订单。仅在客户明确确认下单后调用。"""
    now = datetime.now()
    # 订单号 = 日期 + 时分秒 + 微秒(6位) + 随机(4位)：并发同一秒多单不撞 UNIQUE
    order_no = (f"ORD-{now.strftime('%Y%m%d')}-"
                f"{now.strftime('%H%M%S')}{now.microsecond:06d}{random.randint(1000, 9999)}")
    total = round(quantity * unit_price, 2)

    try:
        await execute(
            """INSERT INTO orders (order_no, customer_id, product_id, product_name, color,
                                   quantity, unit_price, total, status, created_at,
                                   phone, address, delivery_date)
               VALUES (:order_no, :customer_id, :product_id, :product_name, :color,
                       :quantity, :unit_price, :total, '待付款', :created_at,
                       :phone, :address, :delivery_date)""",
            {
                "order_no": order_no, "customer_id": customer_id,
                "product_id": product_id, "product_name": product_name, "color": color,
                "quantity": quantity, "unit_price": unit_price, "total": total,
                "created_at": now.isoformat(),
                "phone": phone, "address": address, "delivery_date": delivery_date,
            },
        )
    except Exception as e:  # noqa: BLE001
        _logger.exception("create_order 失败: %s", str(e)[:200])
        return "订单生成失败，请稍后重试。您的需求已记录，销售同事会尽快联系您。"

    extra = ""
    if phone:
        extra += f"\n电话：{phone}"
    if address:
        extra += f"\n地址：{address}"
    if delivery_date:
        extra += f"\n交期：{delivery_date}"

    return (
        f"✅ 订单已生成！\n"
        f"订单号：{order_no}\n"
        f"产品：{product_name} | {color or '未指定'}\n"
        f"数量：{quantity} 米\n"
        f"单价：¥{unit_price}/米\n"
        f"总价：¥{total}\n"
        f"状态：待付款{extra}\n"
        f"请于 3 日内完成付款，支持对公转账。如有疑问请联系销售经理。"
    )


if __name__ == "__main__":
    sys.exit(mcp.run(transport="stdio"))