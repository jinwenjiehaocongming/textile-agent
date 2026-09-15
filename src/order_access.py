"""工具调用的身份边界（安全关键，2026-09）
==========================================
**问题（真实可复现）**：订单级工具的 `order_no` 是 LLM 从对话里拿的，而工具自己
**只按订单号查库、完全不校验调用者**。实测三条越权路径：

| 攻击（普通客户账号即可） | 改之前的结果 |
|---|---|
| 让 Agent「查一下订单 ORD-别人的单号」 | 返回**别人的电话与地址**（`refund_server.query_order`） |
| 让 Agent「退掉订单 ORD-别人的单号」 | 工单建在别人的单上，且该订单被推进「退款中」（跨租户写） |
| 提示词注入让 Agent 下单时带上别人的 `customer_id` | 订单**记在别人名下**（`create_order` 只信参数） |

根因不是"某个工具忘了校验"，而是**身份只写在提示词里**：
`ORDER_AGENT_PROMPT` 里那句"customer_id 必须填系统提供的值"是**建议**，不是约束；
三处调度点（`order_agent` / `after_sales_agent` / `agent.tool_executor`）都是
`mcp.call_tool(name, args)` —— **LLM 给的 args 原样透传**。

修法（两层，缺一不可）
======================
1. **服务端强制覆盖**（本模块）：调用工具前，把可信身份**写进 args**（同名键直接覆盖，
   不是 `setdefault`）。LLM 在 args 里塞的任何身份值都会被冲掉。
   > `setdefault` 写法踩过：`app.py` 审批兜底里 `args.setdefault("customer_id", 可信值)`
   > 本意是"LLM 漏传时补齐"，结果**LLM 传了就保留** —— 攻击者填的值反而赢。
2. **工具侧校验**（`mcp_servers/*`）：订单级工具必须收 `caller_id` 并按
   `order_no + customer_id` 一起查；**缺 caller_id 直接拒绝（fail-closed）**，
   查不到与不属于自己返回**同一句话**（不泄露订单是否存在）。

为什么两层都要：只有第 1 层，将来新增调用点忘了注入就静默开洞；
只有第 2 层，`caller_id` 本身来自 LLM args，攻击者可以自己填成受害者的 id 绕过。
"""

# 需要"按订单归属校验调用者"的工具（order_no 由 LLM 提供）
ORDER_SCOPED_TOOLS = frozenset({"query_order_status", "query_order", "create_refund"})

# 需要"把身份换成调用者"的写工具（身份即数据本身，不能被 LLM 指定）
IDENTITY_TOOLS = frozenset({"create_order"})

IDENTITY_KEYS = ("caller_id", "customer_id")

# 工具侧统一话术：查不到与无权访问**同一句**，避免"存在性"泄露
NOT_YOURS = "未找到该订单，或该订单不属于您。请核对订单号（ORD- 开头）后重试。"


def with_trusted_identity(name: str, args: dict, customer_id: str) -> dict:
    """把可信身份注入工具参数（**覆盖**同名键）。

    这是唯一允许构造订单级工具参数的地方：三处调度点都走它，避免"某处忘了注入"。
    身份为空（未登录/guest）时不注入 —— 工具侧会 fail-closed 拒绝，
    比在这里静默放行安全。
    """
    args = dict(args or {})
    if customer_id:
        if name in ORDER_SCOPED_TOOLS:
            args["caller_id"] = customer_id          # 覆盖 LLM 可能塞进来的值
        elif name in IDENTITY_TOOLS:
            args["customer_id"] = customer_id
    else:
        # 没有可信身份：把 LLM 提供的身份类参数**清掉**，让工具按"缺身份"拒绝，
        # 而不是拿一个来源不明的 id 去查别人的单
        for k in IDENTITY_KEYS:
            args.pop(k, None)
    return args
