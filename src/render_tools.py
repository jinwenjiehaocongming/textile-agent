"""
结构化输出工具（Render Tools）
==============================
让 LLM 在"要给客户展示数据"时，强制通过工具调用输出结构化 JSON，
前端据此渲染表格，而不是把数据埋在自由文本里。

设计：
- 这些工具是"数据展示协议"：不真正执行任何操作（返回空字符串），
  作用只是让 LLM 把数据以 JSON 形式从 tool_calls 参数里"交出来"。
- 后端（src/stream_chat.py）在最终回复的 tool_calls 里提取这些 JSON，
  随 SSE 的 done 事件带 data 字段发给前端。
- System prompt 中说明：只要回复里包含需要表格化展示的产品/订单/退款数据，
  就必须调用对应 render 工具，把数据作为参数传入，并在正文里写简要说明。

Schema 与数据库字段严格对应（products/orders/refunds 表结构）。
"""

import json
from typing import Any, Optional

# ============================================================
# 1. 展示工具的 JSON Schema（绑定给 LLM 的 bind_tools 用）
# ============================================================

PRODUCT_ITEM = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "面料名称，如 T400、380T 尼丝纺"},
        "color": {"type": "string", "description": "颜色"},
        "width": {"type": "string", "description": "门幅，如 150/160cm"},
        "weight": {"type": "string", "description": "规格/纱支，如 75D×75D、40D×40D"},
        "stock": {"type": ["integer", "string"], "description": "库存（米）"},
        "moq": {"type": ["integer", "string"], "description": "起订量（米）"},
        "price": {"type": ["number", "string"], "description": "单价（元/米）"},
        "delivery_days": {"type": ["integer", "string"], "description": "交期（天）"},
    },
    "required": ["name", "color", "price"],
}

ORDER_ITEM = {
    "type": "object",
    "properties": {
        "order_no": {"type": "string", "description": "订单号，如 ORD-20260822-..."},
        "product_name": {"type": "string", "description": "产品名称"},
        "color": {"type": "string", "description": "颜色"},
        "quantity": {"type": ["integer", "string"], "description": "数量（米）"},
        "unit_price": {"type": ["number", "string"], "description": "单价（元/米）"},
        "total": {"type": ["number", "string"], "description": "总价（元）"},
        "status": {"type": "string", "description": "订单状态，如 待付款/已付款/已发货"},
        "created_at": {"type": "string", "description": "下单时间"},
        "phone": {"type": "string", "description": "联系电话（如有）"},
        "address": {"type": "string", "description": "收货地址（如有）"},
        "delivery_date": {"type": "string", "description": "交期（如有）"},
    },
    "required": ["order_no", "product_name", "status"],
}

REFUND_ITEM = {
    "type": "object",
    "properties": {
        "order_no": {"type": "string", "description": "关联订单号"},
        "reason": {"type": "string", "description": "退款原因"},
        "status": {"type": "string", "description": "退款单状态"},
        "created_at": {"type": "string", "description": "创建时间"},
    },
    "required": ["order_no", "reason", "status"],
}

RENDER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "render_products",
            "description": (
                "【表格展示】当需要向客户展示一块或多块布的规格/价格/库存数据时调用。"
                "参数传产品数组，每项包含 name/color/width/weight/stock/moq/price/delivery_days。"
                "调用后在正文里写简要说明即可。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "products": {
                        "type": "array",
                        "items": PRODUCT_ITEM,
                        "description": "要展示的产品列表",
                    },
                },
                "required": ["products"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "render_order",
            "description": (
                "【表格展示】当需要向客户展示一笔订单的信息时调用（仅限两种情况："
                "① 通过 query_order_status 工具查询到的真实订单；"
                "② 下单流程中 create_order 真实生成订单后的结果展示）。"
                "参数传订单对象（order_no/product_name/color/quantity/unit_price/total/status 等）。"
                "⚠️ 严禁编造订单号或订单状态：没有真实查询/生成结果时，绝不能调用本工具，"
                "应如实告知客户未查到订单。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order": ORDER_ITEM,
                },
                "required": ["order"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "render_refund",
            "description": (
                "【表格展示】当需要向客户展示一张退款单的信息（退货/退款处理结果）时调用。"
                "参数传退款单对象（order_no/reason/status/created_at）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "refund": REFUND_ITEM,
                },
                "required": ["refund"],
            },
        },
    },
]

RENDER_TOOL_NAMES = {t["function"]["name"] for t in RENDER_TOOLS}

# ============================================================
# 2. System Prompt 补充：告诉 LLM 何时调用展示工具
# ============================================================

RENDER_PROMPT_HINT = """\
## 数据展示规则（重要）
当你的回复中包含以下数据时，必须调用对应的"展示工具"把数据以 JSON 传出去，
正文里只写简要说明（不要用纯文本重复整张表格）：
- 一块或多块布的价格/规格/库存 → 调用 render_products，参数传产品数组
- 订单信息（查订单状态、下单后的确认单）→ 调用 render_order，参数传订单对象
- 退款单信息 → 调用 render_refund，参数传退款单对象
查询不到数据、或纯闲聊/知识问答时，不需要调用这些工具。

## 展示工具真实性红线（违反即事故）
- render_order / render_refund **只能展示工具调用（query_order_status 等）返回的真实数据**。
- 严禁编造订单号、订单状态、金额、电话、地址——没有真实查询结果就如实说"未查到该订单"，
  绝不能自己造一张"订单卡片"给客户看。
- 订单号格式必须是 query_order_status 返回的真实值（如 ORD-20260822-165808931234）；
  你自行拼出的 ORD-xxxx 一律是编造。

## 重要：正文不能依赖表格
调用展示工具后，正文仍必须用文字给出**可独立理解的关键结论**：
价格区间、推荐款、现货情况等，不能只写"以上为……"这类指代表格的话——
客户在纯文本环境（终端、评测）看不到表格，正文要自带完整信息。
"""

# ============================================================
# 3. 从最终 state 提取展示数据
# ============================================================

def extract_render_data(messages: list) -> Optional[dict]:
    """
    从最终消息列表里查找最后一个展示工具调用，返回其 JSON 参数。

    Returns:
        {"type": "products"|"order"|"refund", "data": <结构化数据>} 或 None
    """
    # 优先：消息上显式挂载的 render_data（order/after_sales agent 用）
    for msg in reversed(messages):
        kw = getattr(msg, "additional_kwargs", None) or {}
        if kw.get("render_data"):
            return kw["render_data"]
        resp_meta = getattr(msg, "response_metadata", None) or {}
        if resp_meta.get("render_data"):
            return resp_meta["render_data"]
    # 兜底：从后往前找最后一个携带 render 工具调用的 AIMessage
    for msg in reversed(messages):
        calls = getattr(msg, "tool_calls", None) or []
        for tc in calls:
            name = tc.get("name", "")
            if name not in RENDER_TOOL_NAMES:
                continue
            args = tc.get("args") or {}
            kind = {
                "render_products": "products",
                "render_order": "order",
                "render_refund": "refund",
            }.get(name)
            if kind == "products" and args.get("products"):
                return {"type": "products", "data": args["products"]}
            if kind in ("order", "refund") and args.get(kind):
                return {"type": kind, "data": args[kind]}
    return None


def attach_render_data(message, render_data: Optional[dict]):
    """
    把结构化展示数据挂到 AIMessage 的 additional_kwargs 上，
    便于 order/after_sales agent 在返回时随回复一起进入最终 state。
    """
    if not render_data:
        return message
    kw = dict(getattr(message, "additional_kwargs", None) or {})
    kw["render_data"] = render_data
    message.additional_kwargs = kw
    return message


def find_render_data_in_msgs(msgs: list) -> Optional[dict]:
    """在任意消息列表（含 agent 内部 history_msgs）里找 render 调用数据。"""
    return extract_render_data(msgs)


# ============================================================
# 4. 后端兜底：LLM 没调 render 工具时，从工具结果解析数据
# ============================================================

def extract_data_from_tools(messages: list) -> Optional[dict]:
    """
    扫描消息里的工具调用/工具结果，尝试解析结构化数据。
    用于 LLM 未乖乖调用 render 工具时的兜底（保证订单等关键场景必出表格）。

    Returns: 同 extract_render_data 格式，或 None。
    """
    # 解析 create_order 之后紧跟的 ToolMessage（订单结果）
    for i, msg in enumerate(messages):
        calls = getattr(msg, "tool_calls", None) or []
        for tc in calls:
            if tc.get("name") == "create_order":
                args = tc.get("args") or {}
                # 查找其后的工具结果
                for later in messages[i + 1:]:
                    if getattr(later, "type", "") == "tool":
                        order_no = _find_order_no(str(later.content))
                        if order_no:
                            return {
                                "type": "order",
                                "data": {
                                    "order_no": order_no,
                                    "product_name": args.get("product_name", ""),
                                    "color": args.get("color", ""),
                                    "quantity": args.get("quantity", ""),
                                    "unit_price": args.get("unit_price", ""),
                                    "total": args.get("quantity", 0) * (args.get("unit_price") or 0)
                                    if isinstance(args.get("quantity"), (int, float))
                                    and isinstance(args.get("unit_price"), (int, float)) else "",
                                    "status": "待付款",
                                    "phone": args.get("phone", ""),
                                    "address": args.get("address", ""),
                                    "delivery_date": args.get("delivery_date", ""),
                                },
                            }
    # 解析 refund 工具结果
    for i, msg in enumerate(messages):
        calls = getattr(msg, "tool_calls", None) or []
        for tc in calls:
            if tc.get("name") == "create_refund":
                args = tc.get("args") or {}
                for later in messages[i + 1:]:
                    if getattr(later, "type", "") == "tool":
                        order_no = _find_order_no(str(later.content))
                        if order_no or args.get("order_no"):
                            return {
                                "type": "refund",
                                "data": {
                                    "order_no": order_no or args.get("order_no", ""),
                                    "reason": args.get("reason", "") or "",
                                    "status": args.get("status", "") or "处理中",
                                },
                            }
    return None


def _find_order_no(text: str) -> str:
    """从文本里提取订单号 ORD-xxx。"""
    import re as _re
    m = _re.search(r"ORD-\d{8}-\d{4,}|ORD-\d{6,}", text)
    return m.group(0) if m else ""