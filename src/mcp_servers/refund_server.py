"""售后 MCP Server（FastMCP · async）

提供 query_order + create_refund 两个工具（售后 Agent 使用）。
"""
import sys
from datetime import datetime

from mcp.server.fastmcp import FastMCP

from src.db import execute, query_one

mcp = FastMCP("refund-server")


@mcp.tool()
async def query_order(order_no: str) -> str:
    """查询订单详情（售后用，比 order_server 的版本多了地址电话）。"""
    row = await query_one("SELECT * FROM orders WHERE order_no = :order_no", {"order_no": order_no})
    if not row:
        return f"未找到订单 {order_no}"
    return (
        f"订单号：{row['order_no']}\n"
        f"产品：{row['product_name']} | {row['color']} | {row['quantity']}米\n"
        f"单价：¥{row['unit_price']}/米 | 总价：¥{row['total']}\n"
        f"状态：{row['status']}\n"
        f"电话：{row['phone'] or '未留'} | 地址：{row['address'] or '未留'}\n"
        f"下单时间：{row['created_at'][:16]}"
    )


@mcp.tool()
async def create_refund(order_no: str, reason: str) -> str:
    """为客户创建退款/退货工单。仅在确认符合退货条件后调用。"""
    now = datetime.now()
    try:
        await execute(
            "INSERT INTO refunds (order_no, reason, status, created_at) VALUES (:order_no, :reason, '待审核', :created_at)",
            {"order_no": order_no, "reason": reason, "created_at": now.isoformat()},
        )
    except Exception:
        return "退款申请提交失败，请稍后重试。如需紧急处理请联系销售经理。"
    return (
        f"✅ 退款工单已生成！\n"
        f"订单号：{order_no}\n"
        f"退款原因：{reason}\n"
        f"状态：待审核\n"
        f"我们的售后人员将在 1 个工作日内审核并联系您。"
    )


if __name__ == "__main__":
    sys.exit(mcp.run(transport="stdio"))