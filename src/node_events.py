"""
图节点事件描述（节点真流式）
============================
把 LangGraph 每个节点的执行过程映射成人类可读的步骤，供 SSE 推给前端
渲染「执行步骤」丝带。纯函数、零依赖，可单测。

节点名 ↔ 中文标签：
    query_reformulator  改写查询
    context_retriever   知识检索
    supervisor          意图路由
    agent               智能应答（售前 ReAct）
    tool_executor       工具执行
    order_agent         下单处理
    after_sales_agent   售后处理
    review              安全审核
"""

NODE_LABELS = {
    "query_reformulator": "改写查询",
    "context_retriever": "知识检索",
    "supervisor": "意图路由",
    "agent": "智能应答",
    "tool_executor": "工具执行",
    "order_agent": "下单处理",
    "after_sales_agent": "售后处理",
    "review": "安全审核",
}

_QUERY_TYPE_LABELS = {"chat": "售前", "place_order": "下单", "after_sales": "售后"}


def describe_node(node: str, update: dict) -> str:
    """生成该节点的细节描述（无细节返回空串）。"""
    update = update or {}
    msgs = update.get("messages") or []

    if node == "supervisor":
        qt = update.get("query_type")
        if qt:
            return f"→ {_QUERY_TYPE_LABELS.get(qt, qt)}"

    if node == "context_retriever":
        chunks = update.get("knowledge_chunks") or []
        if chunks:
            return f"命中 {len(chunks)} 条知识"
        return "闲聊，跳过检索"

    if node in ("agent", "order_agent", "after_sales_agent"):
        if node == "agent":
            return _describe_tool_calls(msgs)  # ReAct 决策/工具调用
        return "处理中…"

    if node == "tool_executor":
        return _describe_executed_tools(msgs)

    if node == "query_reformulator":
        rq = update.get("rewrite_query")
        if rq:
            rq = rq.replace("\n", " ｜ ")
            return rq[:60]
    return ""


def _describe_tool_calls(msgs) -> str:
    """Agent 节点的 LLM 响应里带了哪些工具调用（ReAct 决策可见）。"""
    for m in reversed(msgs):
        calls = getattr(m, "tool_calls", None) or []
        if calls:
            names = [tc.get("name", "") for tc in calls]
            return "调用 " + "、".join(names)
    return ""


def _describe_executed_tools(msgs) -> str:
    """工具执行节点：按 tool_call_id 回查工具名。"""
    names = []
    for m in msgs:
        if getattr(m, "type", "") != "tool":
            continue
        tid = getattr(m, "tool_call_id", None)
        for m2 in msgs:
            for tc in (getattr(m2, "tool_calls", None) or []):
                if tc.get("id") == tid:
                    names.append(tc.get("name", ""))
    if names:
        return "执行 " + "、".join(names)
    return ""