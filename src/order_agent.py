"""
下单 Agent — 独立 Agent，只管订单
====================================
自带产品查询工具 + 订单写入工具。不依赖售前 Agent。
"""

import sqlite3, json, sys
from pathlib import Path
from datetime import datetime
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from dotenv import load_dotenv
import os

sys.path.insert(0, str(Path(__file__).parent.parent))

load_dotenv()

ORDERS_DB = Path(__file__).parent.parent / "data" / "orders.db"
PRODUCTS_DB = Path(__file__).parent.parent / "data" / "products.db"

order_llm = ChatOpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
    model="deepseek-v4-flash",
    temperature=0,
)


# ============================================================
# 工具1：查产品（下单 Agent 也要能查）
# ============================================================
def _search_product(query: str) -> str:
    """查产品库，按关键词匹配数排序，精准的排前面"""
    keywords = [kw.strip().lower() for kw in query.replace("，", " ").replace(",", " ").split() if kw.strip()]
    if not keywords:
        return "未找到产品"

    conn = sqlite3.connect(str(PRODUCTS_DB))
    conn.row_factory = sqlite3.Row
    where_parts = []
    params = []
    for kw in keywords:
        p = f"%{kw}%"
        where_parts.append("(name LIKE ? OR color LIKE ? OR category LIKE ?)")
        params.extend([p, p, p])

    rows = conn.execute(
        f"SELECT * FROM products WHERE {' OR '.join(where_parts)}", params
    ).fetchall()
    conn.close()

    if not rows:
        return "未找到匹配产品"

    # 计分排序：匹配关键词越多越靠前
    scored = []
    for r in rows:
        text = f"{r['name']} {r['color']} {r['category']} {r['weight']} {r['id']}".lower()
        score = sum(1 for kw in keywords if kw in text)
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)

    lines = []
    for _, r in scored[:10]:
        lines.append(
            f"货号:{r['id']} | {r['name']} | {r['color']} | 门幅:{r['width']}cm "
            f"| {r['weight']} | 库存:{r['stock']}米 | MOQ:{r['moq']}米 "
            f"| ¥{r['price']}/米 | 交期:{r['delivery_days']}天"
        )
    return "\n".join(lines)


SEARCH_PRODUCT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_product",
        "description": "查询产品库存和报价。只有查到真实产品才能下单，不能编造。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "产品名、颜色或规格关键词"},
            },
            "required": ["query"],
        },
    },
}

# ============================================================
# 下单 Agent 专用 JSON Schema
# ============================================================
CREATE_ORDER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_order",
        "description": "为客户创建面料采购订单。仅在客户明确确认下单后调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "customer_id": {
                    "type": "string",
                    "description": "客户标识，从对话上下文中提取（如微信ID、用户ID）",
                },
                "product_id": {
                    "type": "string",
                    "description": "产品货号，如 P003。必须从工具搜索结果或对话中提到过的产品中选择",
                },
                "product_name": {"type": "string", "description": "面料名称"},
                "color": {"type": "string", "description": "颜色"},
                "quantity": {
                    "type": "integer",
                    "description": "订购数量（米）",
                },
                "unit_price": {
                    "type": "number",
                    "description": "单价（元/米），必须是对话中确认过的价格",
                },
                "phone": {
                    "type": "string",
                    "description": "客户联系电话，必须向客户确认",
                },
                "address": {
                    "type": "string",
                    "description": "收货地址，必须向客户确认",
                },
                "delivery_date": {
                    "type": "string",
                    "description": "期望交期，必须向客户确认",
                },
            },
            "required": ["customer_id", "product_id", "product_name", "quantity", "unit_price", "phone", "address", "delivery_date"],
        },
    },
}


def _insert_order(
    customer_id: str, product_id: str, product_name: str,
    color: str, quantity: int, unit_price: float,
    phone: str = "", address: str = "", delivery_date: str = ""
) -> str:
    """插入订单到数据库"""
    now = datetime.now()
    order_no = f"ORD-{now.strftime('%Y%m%d')}-{now.strftime('%H%M%S')}"
    total = round(quantity * unit_price, 2)

    try:
        conn = sqlite3.connect(str(ORDERS_DB))
        conn.execute("""
            INSERT INTO orders (order_no, customer_id, product_id, product_name, color,
                                quantity, unit_price, total, status, created_at,
                                phone, address, delivery_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '待付款', ?, ?, ?, ?)
        """, (order_no, customer_id, product_id, product_name, color,
              quantity, unit_price, total, now.isoformat(),
              phone, address, delivery_date))
        conn.commit()
        conn.close()
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


def query_order(order_no: str) -> str:
    """查询订单状态"""
    conn = sqlite3.connect(str(ORDERS_DB))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
    conn.close()
    if not row:
        return f"未找到订单 {order_no}"
    return (
        f"订单号：{row['order_no']}\n"
        f"产品：{row['product_name']} | {row['color']}\n"
        f"数量：{row['quantity']}米 | ¥{row['unit_price']}/米 | 总价：¥{row['total']}\n"
        f"状态：{row['status']}\n"
        f"下单时间：{row['created_at'][:16]}"
    )


# ============================================================
# Supervisor 判断：是否该走下单 Agent？
# ============================================================
SUPERVISOR_PROMPT = """你是订单路由判断。检查对话历史，判断客户是否想要下单。

下单信号（任一满足即判定 yes）：
- 客户明确说 "下单""订货""就要这个""来xxx米""买了""给我安排"
- 客服已经报了价，客户说 "行""好""可以""OK"（结合上下文确认是同意下单而非一般回应）
- 客户主动给了收货信息或要求开发票

不下单信号（判定 no）：
- 只是询价、对比、问规格
- 只是回应 "好的" 但不涉及确认交易
- 闲聊、谢谢

对话历史：
{history}

只输出 yes 或 no："""


def should_create_order(messages: list) -> bool:
    """Supervisor：检查对话是否应该走下单流程"""
    history = "\n".join(
        f"{'客户' if m.type == 'human' else '客服'}: {m.content[:80]}"
        for m in messages[-8:]
    )
    try:
        resp = order_llm.invoke([
            HumanMessage(content=SUPERVISOR_PROMPT.format(history=history))
        ])
        return resp.content.strip().lower() == "yes"
    except Exception:
        return False


# ============================================================
# 下单 Agent 的对话节点
# ============================================================
ORDER_AGENT_PROMPT = """你是下单助手。根据对话历史处理订单。

## 下单流程（必须遵守，不能跳步）
**不要把思考过程说出来。不要自言自语。直接给确认单或下单结果。**

### 第一步：收集信息
- 检查对话里是否有完整信息：货号 + 产品名 + 颜色 + 数量 + 单价 + 电话 + 地址 + 交期
- 信息不齐 → 问客户补齐，不能编造
- 如果产品价格没有在对话中确认过 → 先调 search_product 查，不能猜

### 第二步：展示确认单（必须！）
- 信息齐全后，直接输出确认单给客户
- **确认单里绝对不能出现订单号**——订单号只有下单成功后才生成。
  确认单格式：
  ```
  📋 订单确认单
  产品：xxx | 货号：Pxxx | 颜色：xxx
  规格：xxx | 数量：xxx米 | 单价：¥xxx/米
  总价：¥xxx | 电话：xxx | 地址：xxx | 交期：xxx
  请确认以上信息是否正确？回复"确认"即可下单。
  ```
- 展示确认单时**不要调 create_order**，等客户回复

### 第三步：客户确认后下单
- 客户说 "确认" → 调用 create_order 工具，严禁编造订单号
- 订单号只能从 create_order 工具返回

对话历史：
{history}

处理命令："""


def order_agent_node(messages: list) -> AIMessage:
    """
    下单 Agent：先查产品确认，再下单。
    绑了 search_product + create_order 两个工具，ReAct 循环。
    """
    history = "\n".join(
        f"{'客户' if m.type == 'human' else '客服'}: {m.content[:100]}"
        for m in messages[-10:]
    )

    llm_with_tools = order_llm.bind_tools([SEARCH_PRODUCT_SCHEMA, CREATE_ORDER_SCHEMA])

    # 构建对话消息
    conversation = [
        SystemMessage(content=ORDER_AGENT_PROMPT.format(history=history)),
        HumanMessage(content="处理：刚才发过确认单 + 客户说确认 → 直接create_order。信息不齐 → 先查先问。"),
    ]

    # 最多 5 轮 ReAct（查产品 → 下单）
    for _ in range(5):
        response = llm_with_tools.invoke(conversation)
        conversation.append(response)

        if hasattr(response, "tool_calls") and response.tool_calls:
            print(f"   🔧 [下单Agent工具] {[(tc['name'], tc['args']) for tc in response.tool_calls]}")
            tool_msgs = []
            for tc in response.tool_calls:
                name, args = tc["name"], tc["args"]
                if name == "search_product":
                    result = _search_product(**args)
                    print(f"   ✅ 查产品结果: {result[:100]}...")
                elif name == "create_order":
                    result = _insert_order(**args)
                    print(f"   ✅ 下单结果: {result[:100]}...")
                else:
                    result = f"未知工具: {name}"
                tool_msgs.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
            conversation.extend(tool_msgs)

            # 如果刚才调了 create_order，直接返回订单结果
            if any(tc["name"] == "create_order" for tc in response.tool_calls):
                # 找到最后一个 ToolMessage（订单结果）
                return tool_msgs[-1]
        else:
            # 没有工具调用 → 最终回复
            # 检查：如果 LLM 编造了订单号（没调 create_order 就说 "订单已生成"），
            # 强制它再试一次，必须调工具
            content = response.content or ""
            if any(kw in content for kw in ["订单已生成", "订单号：ORD", "订单号为 ORD"]):
                print("   ⚠️ [下单Agent] 检测到编造订单号，强制重试...")
                conversation.append(HumanMessage(content="你刚才编了一个订单号。这是不允许的。你必须调用 create_order 工具来生成真实订单。请现在调用 create_order。"))
                continue  # 回到 for 循环开头，再问 LLM
            return response

    return AIMessage(content="下单流程超时，请稍后重试。")


# ============================================================
# 工具函数暴露给外部 graph
# ============================================================
