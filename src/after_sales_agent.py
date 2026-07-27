"""
售后 Agent — 退货退款、质量问题投诉
=====================================
独立 Agent，查订单 + 对照退货规则 + 生成退款工单。
不碰产品库、不报价。
"""

import sqlite3, sys
from pathlib import Path
from datetime import datetime
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from dotenv import load_dotenv
import os

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

ORDERS_DB = Path(__file__).parent.parent / "data" / "orders.db"

after_sales_llm = ChatOpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
    model="deepseek-v4-flash",
    temperature=0,
)

# ============================================================
# 工具1：查订单
# ============================================================
def _query_order(order_no: str) -> str:
    conn = sqlite3.connect(str(ORDERS_DB))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
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


QUERY_ORDER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "query_order",
        "description": "查询订单详情，用于售后处理前确认订单信息",
        "parameters": {
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "订单号 ORD-xxx"},
            },
            "required": ["order_no"],
        },
    },
}


# ============================================================
# 工具2：创建退款工单
# ============================================================
def _create_refund(order_no: str, reason: str) -> str:
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


CREATE_REFUND_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_refund",
        "description": "为客户创建退款/退货工单。仅在确认符合退货条件后调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "订单号"},
                "reason": {"type": "string", "description": "退款原因，如：色差超标、纬斜、面料破损等"},
            },
            "required": ["order_no", "reason"],
        },
    },
}


# ============================================================
# Supervisor 判断：是否该走售后
# ============================================================
def should_after_sales(messages: list) -> bool:
    """检查对话最后一条客户消息是否有售后意图"""
    last = messages[-1].content if messages else ""
    keywords = ["退货", "退款", "质量问题", "色差", "破洞", "纬斜", "缩水",
                "投诉", "换货", "退钱", "赔", "不满意", "发货了吗", "催发货",
                "还没收到", "物流", "到哪了"]
    return any(kw in last for kw in keywords)


# ============================================================
# 售后 Agent
# ============================================================
AFTER_SALES_PROMPT = """你是纺织厂的售后服务专员。处理客户的退货、退款、质量投诉。

## 工作流程
**不要说 "我先帮您查一下""让我确认一下情况" 之类的废话。直接把结果给客户。**
1. 先问客户订单号。如果客户没提供，问他要
2. 查到订单后，和客户确认问题：哪里不满意？收到后发现了什么问题？
3. 对照退货规则判断：
   - 符合退货条件（纬斜>3%、色差超标、破洞油污超标）→ 创建退款工单
   - 不符合条件（已裁剪、定制色、超30天）→ 礼貌解释为什么不能退
   - 物流/发货问题 → 查状态告知进度
4. 确属我方责任的，创建退款工单；不属我方责任的，解释原因

## 退货规则参考
- 纬斜率超过3%可退货
- 色差超合同约定等级（缸差超半级）可退货
- 破洞、油污、勾丝等明显疵点超四分制标准可退货
- 已裁剪加工不可退货
- 定制染色不可退货
- 已确认产前样、大货一致不可退货
- 超30天未提出异议视为验收合格
- 大货到货建议7天内验收
- 物流破损由物流公司赔付，我方协助处理

## 投诉处理规范
- 先共情：客户情绪激动时，先道歉安抚 "很抱歉给您带来不便"
- 再落实：问清楚具体问题（什么时候收到的、哪批货、什么表现）
- 然后对照退货规则判断是否符合退货条件
- 符合条件 → 创建退款工单；不符合 → 解释原因，不推诿
- 不要说空洞的 "我会帮您处理" —— 给出具体下一步行动
- 让客户感觉问题在被认真对待，不是在走流程

## 原则
- 态度诚恳，不推卸责任
- 确属我方问题的，主动创建工单
- 不确定的情况，建议客户寄回样片检测
- 不直接承诺赔多少钱——工单审核后由人工确认

对话历史：
{history}

请处理客户的售后需求。"""


def after_sales_node(messages: list) -> AIMessage:
    """售后 Agent 的对话节点"""
    history = "\n".join(
        f"{'客户' if m.type == 'human' else '客服'}: {m.content[:100]}"
        for m in messages[-10:]
    )

    llm_with_tools = after_sales_llm.bind_tools([QUERY_ORDER_SCHEMA, CREATE_REFUND_SCHEMA])
    conversation = [
        SystemMessage(content=AFTER_SALES_PROMPT.format(history=history)),
        HumanMessage(content="请处理客户的售后需求。"),
    ]

    for _ in range(5):
        response = llm_with_tools.invoke(conversation)
        conversation.append(response)

        if hasattr(response, "tool_calls") and response.tool_calls:
            tool_msgs = []
            for tc in response.tool_calls:
                name, args = tc["name"], tc["args"]
                if name == "query_order":
                    result = _query_order(**args)
                    print(f"   🔧 [售后] query_order({args['order_no']})")
                elif name == "create_refund":
                    result = _create_refund(**args)
                    print(f"   🔧 [售后] create_refund({args['order_no']})")
                else:
                    result = f"未知工具: {name}"
                tool_msgs.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
            conversation.extend(tool_msgs)

            if any(tc["name"] == "create_refund" for tc in response.tool_calls):
                return tool_msgs[-1]
        else:
            return response

    return AIMessage(content="售后处理超时，请稍后重试或转人工客服。")
