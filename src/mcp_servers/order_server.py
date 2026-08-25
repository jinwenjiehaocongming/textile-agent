"""
订单管理 MCP Server
===================
提供 query_order + create_order 两个工具。
下单 Agent 和售前 Agent 都通过 MCP 协议调用。
"""
import json
import random
import sys
from pathlib import Path
from datetime import datetime

try:
    from sqlite_utils import execute, query_one  # 纯脚本运行（sys.path[0] = src/mcp_servers/）
except ImportError:
    from src.mcp_servers.sqlite_utils import execute, query_one  # 包方式导入（测试/项目内）

ORDERS_DB = Path(__file__).parent.parent.parent / "data" / "orders.db"


def query_order(order_no: str) -> str:
    """查询订单状态"""
    row = query_one(
        ORDERS_DB,
        "SELECT * FROM orders WHERE order_no = ?", (order_no,),
    )
    if not row:
        return f"未找到订单 {order_no}"
    return (
        f"订单号：{row['order_no']}\n"
        f"产品：{row['product_name']} | {row['color']}\n"
        f"数量：{row['quantity']}米 | ¥{row['unit_price']}/米 | 总价：¥{row['total']}\n"
        f"状态：{row['status']}\n"
        f"下单时间：{row['created_at'][:16]}"
    )


def create_order(customer_id: str, product_id: str, product_name: str,
                 color: str, quantity: int, unit_price: float,
                 phone: str = "", address: str = "", delivery_date: str = "") -> str:
    """创建订单，写入 SQLite。连接由 sqlite_utils 保证 finally 关闭（不泄漏）。"""
    now = datetime.now()
    # 订单号 = 日期 + 时分秒 + 微秒(6位) + 随机(4位)：并发下同一秒多单不撞 UNIQUE 约束
    order_no = (f"ORD-{now.strftime('%Y%m%d')}-"
                f"{now.strftime('%H%M%S')}{now.microsecond:06d}{random.randint(1000, 9999)}")
    total = round(quantity * unit_price, 2)

    try:
        execute(ORDERS_DB, """
            INSERT INTO orders (order_no, customer_id, product_id, product_name, color,
                                quantity, unit_price, total, status, created_at,
                                phone, address, delivery_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '待付款', ?, ?, ?, ?)
        """, (order_no, customer_id, product_id, product_name, color,
              quantity, unit_price, total, now.isoformat(),
              phone, address, delivery_date))
    except Exception:
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


# ============================================================
# JSON Schema 参数校验
# ============================================================

TOOL_SCHEMAS = {
    "query_order_status": {
        "type": "object",
        "properties": {
            "order_no": {"type": "string"},
        },
        "required": ["order_no"],
    },
    "create_order": {
        "type": "object",
        "properties": {
            "customer_id": {"type": "string"},
            "product_id": {"type": "string"},
            "product_name": {"type": "string"},
            "color": {"type": "string"},
            "quantity": {"type": "integer"},
            "unit_price": {"type": "number"},
            "phone": {"type": "string"},
            "address": {"type": "string"},
            "delivery_date": {"type": "string"},
        },
        "required": ["customer_id", "product_id", "product_name",
                     "quantity", "unit_price", "phone", "address", "delivery_date"],
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
                "serverInfo": {"name": "order-server", "version": "1.0.0"},
            })

        elif method == "tools/list":
            _respond(req_id, {
                "tools": [
                    {
                        "name": "query_order_status",
                        "description": "查询订单状态。客户提供了订单号(ORD-开头的)就能查。",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "order_no": {
                                    "type": "string",
                                    "description": "订单号，格式 ORD-20260720-xxxxxx",
                                },
                            },
                            "required": ["order_no"],
                        },
                    },
                    {
                        "name": "create_order",
                        "description": "为客户创建面料采购订单。仅在客户明确确认下单后调用。",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "customer_id": {"type": "string", "description": "客户标识"},
                                "product_id": {"type": "string", "description": "产品货号，如 P003"},
                                "product_name": {"type": "string", "description": "面料名称"},
                                "color": {"type": "string", "description": "颜色"},
                                "quantity": {"type": "integer", "description": "订购数量（米）"},
                                "unit_price": {"type": "number", "description": "单价（元/米）"},
                                "phone": {"type": "string", "description": "联系电话"},
                                "address": {"type": "string", "description": "收货地址"},
                                "delivery_date": {"type": "string", "description": "期望交期"},
                            },
                            "required": ["customer_id", "product_id", "product_name",
                                         "quantity", "unit_price", "phone", "address", "delivery_date"],
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
            elif tool_name == "query_order_status":
                result_text = query_order(**tool_args)
            elif tool_name == "create_order":
                result_text = create_order(**tool_args)
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
