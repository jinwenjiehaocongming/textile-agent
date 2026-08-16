"""
售后 MCP Server
===============
提供 query_order + create_refund 两个工具。
售后 Agent 通过 MCP 协议调用。
"""
import json
import sqlite3
import sys
from pathlib import Path
from datetime import datetime

ORDERS_DB = Path(__file__).parent.parent.parent / "data" / "orders.db"


def query_order(order_no: str) -> str:
    """查询订单详情（售后用，比 order_server 的版本多了地址电话）"""
    conn = sqlite3.connect(str(ORDERS_DB))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM orders WHERE order_no = ?", (order_no,)
    ).fetchone()
    conn.close()
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


def create_refund(order_no: str, reason: str) -> str:
    """创建退款工单"""
    now = datetime.now()
    try:
        conn = sqlite3.connect(str(ORDERS_DB))
        conn.execute(
            "INSERT INTO refunds (order_no, reason, status, created_at) VALUES (?, ?, '待审核', ?)",
            (order_no, reason, now.isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception:
        return "退款申请提交失败，请稍后重试。如需紧急处理请联系销售经理。"
    return (
        f"✅ 退款工单已生成！\n"
        f"订单号：{order_no}\n"
        f"退款原因：{reason}\n"
        f"状态：待审核\n"
        f"我们的售后人员将在 1 个工作日内审核并联系您。"
    )


# ============================================================
# JSON Schema 参数校验
# ============================================================

TOOL_SCHEMAS = {
    "query_order": {
        "type": "object",
        "properties": {
            "order_no": {"type": "string"},
        },
        "required": ["order_no"],
    },
    "create_refund": {
        "type": "object",
        "properties": {
            "order_no": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["order_no", "reason"],
    },
}


def _validate_args(tool_name, args):
    """校验工具参数。返回错误信息或 None（通过）。"""
    schema = TOOL_SCHEMAS.get(tool_name)
    if schema is None:
        return None
    props = schema.get("properties", {})
    required = schema.get("required", [])
    for field in required:
        if field not in args or args[field] is None:
            return f"参数校验失败: 缺少必填字段 '{field}'"
    type_map = {"string": str, "integer": int, "number": (int, float), "boolean": bool}
    for field, value in args.items():
        if field not in props:
            return f"参数校验失败: 未声明的字段 '{field}'"
        expected = props[field].get("type")
        if expected and expected in type_map:
            if not isinstance(value, type_map[expected]):
                return (
                    f"参数校验失败: 字段 '{field}' 期望 {expected} 类型，"
                    f"实际收到 {type(value).__name__} 类型"
                )
    return None


# ============================================================
# MCP Server 主循环
# ============================================================

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = request.get("method", "")
        req_id = request.get("id")
        params = request.get("params", {})

        if method == "initialize":
            _respond(req_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "refund-server", "version": "1.0.0"},
            })

        elif method == "tools/list":
            _respond(req_id, {
                "tools": [
                    {
                        "name": "query_order",
                        "description": "查询订单详情，用于售后处理前确认订单信息",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "order_no": {
                                    "type": "string",
                                    "description": "订单号 ORD-xxx",
                                },
                            },
                            "required": ["order_no"],
                        },
                    },
                    {
                        "name": "create_refund",
                        "description": "为客户创建退款/退货工单。仅在确认符合退货条件后调用。",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "order_no": {"type": "string", "description": "订单号"},
                                "reason": {
                                    "type": "string",
                                    "description": "退款原因，如：色差超标、纬斜、面料破损等",
                                },
                            },
                            "required": ["order_no", "reason"],
                        },
                    },
                ],
            })

        elif method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            error = _validate_args(tool_name, tool_args)
            if error:
                result_text = error
            elif tool_name == "query_order":
                result_text = query_order(**tool_args)
            elif tool_name == "create_refund":
                result_text = create_refund(**tool_args)
            else:
                result_text = f"未知工具: {tool_name}"
            _respond(req_id, {
                "content": [{"type": "text", "text": result_text}],
            })


def _respond(req_id, result):
    response = {"jsonrpc": "2.0", "result": result, "id": req_id}
    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
