"""售后 MCP Server（FastMCP · async）

提供 query_order + create_refund 两个工具（售后 Agent 使用）。
"""
import sys
from mcp.server.fastmcp import FastMCP

from src.db import query_one
from src.order_access import NOT_YOURS
from src.order_flow import OrderActionError, create_refund_ticket

mcp = FastMCP("refund-server")


@mcp.tool()
async def query_order(order_no: str, caller_id: str = "") -> str:
    """查询订单详情（售后用，比 order_server 的版本多了地址电话）。

    ⚠️ `caller_id` 由**服务端注入**（`src/order_access.py`），不是给模型填的：
    这个工具会返回电话/地址，改之前**任何调用者只要知道订单号就能读到别人的 PII**。
    现在按 `order_no + customer_id` 一起查，查不到与不属于自己返回同一句话。
    """
    if not caller_id:
        return "无法确认您的身份，请重新登录后再试。"
    row = await query_one(
        "SELECT * FROM orders WHERE order_no = :order_no AND customer_id = :cid",
        {"order_no": order_no, "cid": caller_id})
    if not row:
        return NOT_YOURS
    return (
        f"订单号：{row['order_no']}\n"
        f"产品：{row['product_name']} | {row['color']} | {row['quantity']}米\n"
        f"单价：¥{row['unit_price']}/米 | 总价：¥{row['total']}\n"
        f"状态：{row['status']}\n"
        f"电话：{row['phone'] or '未留'} | 地址：{row['address'] or '未留'}\n"
        f"下单时间：{row['created_at'][:16]}"
    )


@mcp.tool()
async def create_refund(order_no: str, reason: str, caller_id: str = "") -> str:
    """为客户创建退款/退货工单。仅在确认符合退货条件后调用。

    ⚠️ `caller_id` 服务端注入。改之前不校验归属 —— 攻击者能让 Agent 在**别人的订单**上
    建退款工单，而工单一旦建立，订单就会被推进「退款中」（跨租户写 + 冻结别人的单）。


    2026-09：建单**同时联动订单状态**（`src/order_flow.py::create_refund_ticket`）——
    订单进入「退款中」并记下退款前的状态（驳回时原样退回）。
    改之前工单和订单互不相干：客户看到"已申请退款"，订单还挂在"已发货"，
    后台也没人能把它推进（实测 19 张已通过工单的订单状态一个字都没动）。
    """
    if not caller_id:
        return "无法确认您的身份，请重新登录后再试。"
    owned = await query_one(
        "SELECT order_no FROM orders WHERE order_no = :no AND customer_id = :cid",
        {"no": order_no, "cid": caller_id})
    if not owned:
        return NOT_YOURS
    try:
        out = await create_refund_ticket(order_no, reason, actor="refund_agent")
    except OrderActionError as e:
        return f"退款申请提交失败：{e.detail}"
    except Exception:
        return "退款申请提交失败，请稍后重试。如需紧急处理请联系销售经理。"

    if not out["ok"]:
        # 订单已是终态（已退款/已取消）：工单照样登记留痕，但要如实告知客户
        return (
            f"⚠️ 该订单当前是「{out['order_status']}」，无需再申请退款。\n"
            f"订单号：{order_no}\n"
            f"若您认为处理有误，请直接联系销售经理。"
        )
    return (
        f"✅ 退款工单已生成！\n"
        f"订单号：{order_no}\n"
        f"退款原因：{reason}\n"
        f"状态：待审核（订单已标记为「退款中」）\n"
        f"我们的售后人员将在 1 个工作日内审核并联系您。"
    )


if __name__ == "__main__":
    sys.exit(mcp.run(transport="stdio"))