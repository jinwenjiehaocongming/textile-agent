"""
纺织 B2B 客服 Agent
===================
设计原则：
  1. 知识库检索是必选项 — 每次对话都先搜，保证面料知识不错
  2. 产品查询是工具 — LLM 按需调用 search_product
  3. 审核是必选项 — 所有回复必须过

图结构（4 个节点）：
  入口 → 检索 → Agent → 审核 → END
                  ↻ (有工具调用则循环)

运行: python src/agent.py
"""

# 离线模式：必须在所有 import 之前设置。
# 本机已有模型缓存 → 强制离线（免费、快、不联网）；
# 缓存缺失（如 CI 首次运行）→ 允许自动下载，否则"离线模式+空缓存"会加载失败。
import os
from pathlib import Path

if Path(os.path.expanduser("~/.cache/huggingface")).exists():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

import readline  # 修复终端中文输入退格残留
import json, re, sys
from pathlib import Path
from typing import TypedDict, List
from langgraph.graph import StateGraph, END
from langgraph.types import Command
from langgraph.checkpoint.memory import MemorySaver
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage
from langsmith import traceable
from dotenv import load_dotenv
import os

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.retrieval import HybridRetriever
from src.order_agent import order_agent_node as order_agent
from src.after_sales_agent import after_sales_node as after_sales_agent_fn
from src.memory import get_user
from src.mcp_client import init_mcp, get_mcp
from src.render_tools import RENDER_TOOLS, RENDER_TOOL_NAMES, RENDER_PROMPT_HINT

load_dotenv()
from src.logging_config import get_logger
from src.llm_utils import _safe_llm
logger = get_logger(__name__)

# ============================================================
# 0. 配置
# ============================================================
# LLM 调用统一走 src.llm_utils._safe_llm（重试 → 降级 → 保底）

# 主 LLM：Agent 对话用 DeepSeek（模型名可从 .env 的 DEEPSEEK_MODEL 覆盖，官方 API 用 deepseek-chat）
_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

llm = ChatOpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
    model=_MODEL, temperature=0.3,
    max_retries=2, timeout=30,
)

# 辅助 LLM：改写 + 审核 + Supervisor
cheap_llm = ChatOpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
    model=_MODEL, temperature=0.1,
    max_retries=2, timeout=15,
)

retriever = HybridRetriever()

# ============================================================
# 1. 工具定义已移至 MCP Server
#    product_server.py → search_product
#    order_server.py   → query_order_status, create_order
#    refund_server.py  → query_order, create_refund
#    Agent 通过 MCP Client 自动发现工具，不再需要硬编码 Schema
# ============================================================


# ============================================================
# 2. 审核 Agent（LLM 判断 + 规则快速拦截）
# ============================================================
REVIEW_PROMPT = """你是纺织企业的安全审核员。审查客服回复是否安全合规。

## 重要：以下内容不算违规，必须放行
- 正常产品报价（¥12.5/米、¥8.5/米、总价¥15,6000 等）
- **订单确认单中的价格、电话、地址**——这是下单流程的正常操作，不是泄露
- 客户主动提供、AI 回显确认的联系方式（"请确认您的电话 138xxx"）

## 真正该拦截的违规
1. 泄露内部财务信息（成本价、进货价、利润空间、毛利率）
2. 私自给出老板/员工的个人手机号——下单确认单里回显客户电话不是泄露
3. 不合理承诺
4. 辱骂、歧视、不专业言论

## 输出 JSON
{{"verdict": "safe" 或 "unsafe", "reason": "一句话说明原因", "rewrite": "如果是unsafe，给出安全版回复"}}

客服回复：
{response}"""


@traceable(run_type="chain", name="review_response")
def review_response(text: str) -> dict:
    """
    双层审核：先规则快速拦截，再 LLM 深度审查。
    返回 {"safe": bool, "reason": str, "rewrite": str}
    """
    # 第一层：规则快速拦截（0ms，免费）
    fast_check = ["成本价", "进货价", "拿货价", "底价", "利润多少", "加我微信"]
    for word in fast_check:
        if word in text:
            is_refusal = any(w in text for w in ["抱歉", "不能", "无法提供", "不方便"])
            has_price_number = bool(re.search(r"\d+\.?\d*元|\$\d+|¥\d+", text))
            if is_refusal and not has_price_number:
                return {"safe": True, "reason": "", "rewrite": ""}
            return {"safe": False, "reason": f"规则拦截: 包含 '{word}'", "rewrite": "抱歉，这个信息暂时无法提供。请问还有其他关于面料规格、价格或用途的问题吗？"}
    # 第二层：LLM 深度审查
    try:
        resp = cheap_llm.invoke([HumanMessage(content=REVIEW_PROMPT.format(response=text[:1500]))])
        result = json.loads(resp.content)
        if result.get("verdict") == "unsafe":
            return {
                "safe": False,
                "reason": result.get("reason", "LLM判定不安全"),
                "rewrite": result.get("rewrite", "抱歉，这个信息暂时无法提供。"),
            }
    except Exception:
        pass

    return {"safe": True, "reason": "", "rewrite": ""}


# ============================================================
# 3. 闲聊预判 — 低风险快速过滤，跳过检索省 LLM 调用
# ============================================================
SKIP_KEYWORDS = ["你好", "在吗", "谢谢", "再见", "好的", "嗯", "哦", "行", "OK", "ok"]

def should_skip_retrieval(msg: str) -> bool:
    """短消息 + 白名单关键词 = 闲聊，跳过检索。误拦率 0。"""
    msg = msg.strip()
    return len(msg) <= 4 and any(kw in msg for kw in SKIP_KEYWORDS)


# ============================================================
# 4. 状态
# ============================================================
class AgentState(TypedDict):
    messages: list
    knowledge_chunks: List[str]
    rewrite_query: str
    query_type: str
    user_id: str
    user_context: str  # 客户偏好，启动时从记忆库加载


# ============================================================
# 4. 生成检索词节点 — 结合历史消解指代，存到 rewrite_query
# ============================================================
QUERY_REFORMULATOR_PROMPT = """根据对话历史，把客户的最后一句话改写为适合检索知识库的查询短语。

规则：
- 消解指代："黑色的多少钱" → "T400黑色价格"
- 补全省略："那白色的呢" → "T400白色规格库存"
- 生成 2-3 个检索短语，换行分隔，每行不超过 15 字
- 闲聊（你好/谢谢/在吗）直接输出原话

对话历史：
{history}

客户最后一句话：{query}

检索短语："""


def query_reformulator(state: AgentState) -> dict:
    """结合对话历史生成检索查询。闲聊跳过。"""
    messages = state["messages"]
    last_msg = messages[-1].content

    if should_skip_retrieval(last_msg):
        logger.debug("[检索词] 跳过（闲聊）")
        return {"rewrite_query": last_msg}

    # 取最近几轮摘要
    history = "\n".join(
        f"{'客户' if m.type == 'human' else '客服'}: {m.content[:60]}"
        for m in messages[-6:]
    )

    logger.info(f"[检索词] 原文: {last_msg}")

    try:
        resp = _safe_llm(cheap_llm, [
            HumanMessage(content=QUERY_REFORMULATOR_PROMPT.format(history=history, query=last_msg))
        ], fallback=HumanMessage(content=last_msg))  # 挂了用原话检索
        reformulated = resp.content.strip()
    except Exception:
        reformulated = last_msg

    combined = f"{last_msg}\n{reformulated}"
    logger.info(f"[检索词] → 改写: {reformulated}")
    return {"rewrite_query": combined}


# ============================================================
# 5. 检索节点（必选）
# ============================================================
def context_retriever(state: AgentState) -> dict:
    """用生成的检索词查知识库。闲聊跳过，省 embedding + Rerank 调用。"""
    last_msg = state["messages"][-1].content
    if should_skip_retrieval(last_msg):
        logger.debug("[检索] 跳过（闲聊）")
        return {"knowledge_chunks": []}

    query = state.get("rewrite_query", last_msg)
    logger.info(f"[检索] 查询: {query}")

    results = retriever.retrieve(query, top_k=5, use_rerank=True)
    chunks = [r["text"] for r in results]

    logger.info(f"[检索] 命中 {len(chunks)} 条: {[r['category'] for r in results]}")
    return {"knowledge_chunks": chunks}


# ============================================================
# 5. Agent 节点
# ============================================================
SYSTEM_PROMPT = """你是【宏润纺织】的 AI 客服。工厂主营化纤面料（涤塔夫、春亚纺、尼丝纺、牛津布等）。

## 规则
1. 产品价格、库存、规格必须通过 search_product 工具查询，不要编造
2. 面料知识优先参考上下文提供的检索结果，不要凭记忆乱说参数
3. 报价用正常售价，不透露成本价和进货价
4. 不知道就诚实说不知道，不要编
5. 简洁专业，用中文
6. 不要把思考过程说出来。不要说任何 "我来帮你查一下""好的让我确认一下" 之类描述你正在做什么的废话。直接给出结果。
7. 用户说"好的""谢谢""知道了"时，只需简短回应。不要重复展示订单号或订单详情——订单已经完成了
8. 【知识/推荐类问题】客户问"推荐什么面料""用什么面料""面料怎么选""哪个适合"时，
   先用检索知识直接给出面料类型结论（并举 1-2 个例子），**不要急着调 search_product 报价**；
   客户明确要价格/现货/下单时再查工具。

## 面料知识参考
{knowledge}

## 客户历史档案
{user_context}

{RENDER_HINT}"""


def agent_node(state: AgentState) -> dict:
    messages = state["messages"]
    chunks = state.get("knowledge_chunks", [])
    knowledge_text = "\n---\n".join(chunks) if chunks else "（无参考知识）"

    logger.debug(f"[Agent] msgs={len(messages)}, chunks={len(chunks)}")

    user_context = state.get("user_context", "")
    system = SystemMessage(content=SYSTEM_PROMPT.format(
        knowledge=knowledge_text,
        user_context=user_context or "（新客户，暂无历史档案）",
        RENDER_HINT=RENDER_PROMPT_HINT,
    ))
    # MCP 动态工具发现：售前只用查产品 + 查订单
    mcp = get_mcp()
    mcp_tools = mcp.get_tools_for_langchain(["search_product", "query_order_status"])
    # 绑定展示工具（结构化 JSON 输出，供前端表格渲染）
    llm_with_tools = llm.bind_tools(mcp_tools + RENDER_TOOLS)
    # 过滤孤立的 ToolMessage（订单结果等），避免 API 报错
    safe = []
    pending = 0
    for m in messages:
        if m.type == "system":
            continue
        if hasattr(m, "tool_calls") and m.tool_calls:
            pending = len(m.tool_calls)
            safe.append(m)
        elif m.type == "tool":
            if pending > 0:
                safe.append(m)
                pending -= 1
        else:
            safe.append(m)
    response = _safe_llm(llm_with_tools, [system] + safe,
        fallback=AIMessage(content="系统繁忙，请稍后重试。如有紧急需求请联系销售经理。"),
        stream_tokens=True)  # 真流式：最终回复 token 逐字推给 SSE
    if hasattr(response, "tool_calls") and response.tool_calls:
        logger.info(f"[Agent] 工具调用: {[(tc['name'], tc['args']) for tc in response.tool_calls]}")
    else:
        logger.debug(f"[Agent] 回答长度: {len(response.content) if response.content else 0} 字")

    return {"messages": state["messages"] + [response]}


# ============================================================
# 6. 工具执行节点
# ============================================================
def tool_executor(state: AgentState) -> dict:
    """统一工具执行：通过 MCP Client 路由到正确的 Server，不再需要 if/elif"""
    last_msg = state["messages"][-1]
    results = []
    for tc in last_msg.tool_calls:
        name, args = tc["name"], tc["args"]
        # 展示工具是"数据透传协议"，不真正执行
        if name in RENDER_TOOL_NAMES:
            logger.debug(f"[工具] {name} (render, 跳过执行)")
            results.append(ToolMessage(content="", tool_call_id=tc["id"]))
            continue
        logger.info(f"[工具] {name}({args})")
        result = get_mcp().call_tool(name, args)
        logger.debug(f"[工具] {name} 结果: {result}")
        results.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
    return {"messages": state["messages"] + results}


# ============================================================
# 7. 审核节点（必选）
# ============================================================
def review_node(state: AgentState) -> dict:
    last_msg = state["messages"][-1]
    if last_msg.type != "ai" or not last_msg.content:
        logger.debug("[审核] 跳过（非 AI 消息）")
        return {}

    verdict = review_response(last_msg.content)
    if not verdict["safe"]:
        logger.warning(f"[审核] 拦截: {verdict['reason']}")
        rewrite = verdict.get("rewrite", "抱歉，这个信息暂时无法提供。")
        safe = AIMessage(content=rewrite)
        return {"messages": state["messages"][:-1] + [safe]}
    logger.info("[审核] 通过")
    return {}


# ============================================================
# 8. Supervisor — 状态机 + LLM 意图分类
# ==========================================================
# Layer 1: 规则判断对话延续（AI在追问 → 继续当前模式）
# Layer 2: LLM 分类新话题（只在 Layer 1 返回 "new" 时触发）

def _detect_continuation(messages: list) -> bool:
    """规则判断：是否在延续上一轮对话。不调 LLM。"""
    if len(messages) < 2:
        return False
    last_ai = None
    for m in reversed(messages):
        if m.type == "ai" and m.content:
            last_ai = m.content
            break
    if not last_ai:
        return False
    last_user = messages[-1].content
    # 订单已完成/闲聊 → 不延续
    if "订单已生成" in last_ai or "ORD-" in last_ai:
        return False
    if should_skip_retrieval(last_user):
        return False
    ai_asking = any(kw in last_ai for kw in ["请问", "提供", "？", "?", "选哪"])
    ai_confirm = "确认单" in last_ai or "请确认以上信息" in last_ai
    user_short = len(last_user) < 10 and not any(
        kw in last_user for kw in ["T400", "涤塔夫", "春亚纺", "尼丝纺", "牛津布", "多少钱"]
    )
    return (ai_asking or ai_confirm) and user_short


SUPERVISOR_PROMPT = """判断客户当前消息意图。历史仅作背景，以当前消息为准。
- sales: 询价、推荐、知识、闲聊、打招呼、查订单
- order: 明确要下单（"确认下单""帮我安排""就要这个"）
- after_sales: 明确要退货、退款、投诉、催货

规则：闲聊/问候永远是sales，不管历史聊过什么。不确定就选sales。

历史：{history}
当前：{last_msg}
只输出一个词："""


def supervisor_node(state: AgentState) -> dict:
    """状态机 + LLM：延续保稳，新话题 LLM 判"""
    messages = state["messages"]
    prev = state.get("query_type", "chat")

    # Layer 0: 上一轮是订单/退款完成消息 → 切回售前
    last = messages[-1] if messages else None
    if last and hasattr(last, "type") and last.type == "tool":
        if "ORD-" in str(last.content) or "退款工单" in str(last.content):
            logger.info("[Supervisor] → 售前 Agent（已完成）")
            return {"query_type": "chat"}

    # Layer 1: 规则延续
    if _detect_continuation(messages):
        labels = {"chat": "售前", "place_order": "下单", "after_sales": "售后"}
        logger.info(f"[Supervisor] 延续 → {labels.get(prev, prev)}")
        return {"query_type": prev}

    # Layer 2: LLM 分类
    history = "\n".join(
        f"{'客户' if m.type == 'human' else '客服'}: {m.content[:60]}"
        for m in messages[-6:]
    )
    last_msg = messages[-1].content
    try:
        resp = _safe_llm(cheap_llm, [
            HumanMessage(content=SUPERVISOR_PROMPT.format(history=history, last_msg=last_msg))
        ], fallback=HumanMessage(content="sales"))  # 挂了默认售前
        result = resp.content.strip().lower()
    except Exception:
        result = "sales"
    if result not in ("sales", "order", "after_sales"):
        result = "sales"

    labels = {"sales": "售前 Agent", "order": "下单 Agent", "after_sales": "售后 Agent"}
    qtypes = {"sales": "chat", "order": "place_order", "after_sales": "after_sales"}
    logger.info(f"[Supervisor] → {labels[result]}")
    return {"query_type": qtypes[result]}


def order_agent_node(state: AgentState) -> dict:
    """下单 Agent。完成后状态切回售前。"""
    logger.info("[下单Agent] 处理订单...")
    reply = order_agent(state["messages"], customer_id=state.get("user_id", "guest"))
    result = {"messages": state["messages"] + [reply]}
    # 下单完成 → 切回售前，避免"你好"也走下单
    if hasattr(reply, "content") and "ORD-" in str(reply.content):
        result["query_type"] = "chat"
    return result


def after_sales_agent_node(state: AgentState) -> dict:
    """售后 Agent。完成后状态切回售前。"""
    logger.info("[售后Agent] 处理售后...")
    reply = after_sales_agent_fn(state["messages"])
    result = {"messages": state["messages"] + [reply]}
    if hasattr(reply, "content") and "退款工单已生成" in str(reply.content):
        result["query_type"] = "chat"
    return result


def supervisor_router(state: AgentState) -> str:
    """检索后路由：售前 / 下单 / 售后"""
    qtype = state.get("query_type", "chat")
    if qtype == "place_order":
        return "order_agent"
    if qtype == "after_sales":
        return "after_sales_agent"
    return "agent"


# ============================================================
# 9. 路由
# ============================================================
def agent_router(state: AgentState) -> str:
    last_msg = state["messages"][-1]
    if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
        return "tool_executor"
    return "review"


# ============================================================
# 9. 建图
# ============================================================
def thread_config(user_id: str) -> dict:
    """每个用户的图执行配置：thread_id = user_id（HITL interrupt 依赖它定位会话）。"""
    return {"configurable": {"thread_id": user_id}}


def build_graph(checkpointer=None):
    builder = StateGraph(AgentState)

    builder.add_node("query_reformulator", query_reformulator)
    builder.add_node("context_retriever", context_retriever)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("agent", agent_node)
    builder.add_node("tool_executor", tool_executor)
    builder.add_node("review", review_node)
    builder.add_node("order_agent", order_agent_node)
    builder.add_node("after_sales_agent", after_sales_agent_node)

    builder.set_entry_point("query_reformulator")
    builder.add_edge("query_reformulator", "context_retriever")

    # 检索后 → Supervisor 三分支
    builder.add_edge("context_retriever", "supervisor")
    builder.add_conditional_edges("supervisor", supervisor_router, {
        "agent": "agent",
        "order_agent": "order_agent",
        "after_sales_agent": "after_sales_agent",
    })

    # 售前路径：agent ⇄ tool_executor → review → END
    builder.add_conditional_edges("agent", agent_router, {
        "tool_executor": "tool_executor",
        "review": "review",
    })
    builder.add_edge("tool_executor", "agent")
    builder.add_edge("review", END)

    # 下单/售后路径 → review → END
    builder.add_edge("order_agent", "review")
    builder.add_edge("after_sales_agent", "review")

    # checkpointer：HITL 下单审批依赖（interrupt 挂起/恢复）。进程内 MemorySaver，
    # 生产换持久化后端（Postgres 等）。显式传入的 checkpointer 优先。
    if checkpointer is None:
        checkpointer = MemorySaver()
    return builder.compile(checkpointer=checkpointer)


# ============================================================
# 10. 测试
# ============================================================
def main():
    # ── 初始化 MCP 工具层 ──
    print("🔌 连接 MCP 工具服务器...")
    init_mcp({
        "product": ["python3", "src/mcp_servers/product_server.py"],
        "order":   ["python3", "src/mcp_servers/order_server.py"],
        "refund":  ["python3", "src/mcp_servers/refund_server.py"],
    })
    mcp = get_mcp()
    print(f"   已发现工具: {[t['name'] for t in mcp.tools]}")

    app = build_graph()

    # 用户 ID（模拟企业微信 external_userid）
    user_id = input("请输入用户ID (默认 guest): ").strip() or "guest"
    memory = get_user(user_id)

    # 加载历史对话 + 用户偏好
    history = memory.load_recent(20)
    prefs = memory.retrieve_preferences()
    user_context = "；".join(prefs) if prefs else ""

    state = {"messages": history, "knowledge_chunks": [], "rewrite_query": "",
             "query_type": "chat", "user_id": user_id, "user_context": user_context}

    print(f"\n{'='*60}")
    print(f"🏭 宏润纺织 AI 客服 | 用户: {user_id}")
    print(f"   对话 {len(history)} 条 | 偏好 {len(prefs)} 条")
    print(f"{'='*60}")
    if prefs:
        print(f"🧠 已知偏好: {user_context[:100]}...")

    print(f"\n{'='*60}")
    print(f"🏭 宏润纺织 AI 客服 | 用户: {user_id}")
    print(f"   加载 {len(history)} 条历史 | 输入 'exit' 退出 | 'reset' 清记录")
    print(f"{'='*60}")
    try:
        while True:
            try:
                user_input = input("\n👤 您: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n👋 再见！")
                break

            if not user_input:
                continue
            if user_input.lower() == "exit":
                print("👋 再见！")
                break
            if user_input.lower() == "reset":
                memory.clear_history()
                from src.approval import remove_pending
                remove_pending(user_id)  # 重置是新的会话，作废挂起的审批
                state = {"messages": [], "knowledge_chunks": [], "rewrite_query": "",
                         "query_type": "chat", "user_id": user_id, "user_context": ""}
                print("🔄 对话记录已清除")
                continue

            state["messages"] = state["messages"] + [HumanMessage(content=user_input)]
            state["knowledge_chunks"] = []
            state["rewrite_query"] = ""

            result = app.invoke(state, config=thread_config(user_id))
            if "__interrupt__" in result:
                # ── HITL：下单挂起，等待人工审批 ──
                for it in result["__interrupt__"]:
                    val = it.value if hasattr(it, "value") else it
                    if isinstance(val, dict) and val.get("type") == "order_approval":
                        draft = val.get("draft", {})
                        print(f"\n⏸ 订单待人工审批: {draft.get('product_name')} x{draft.get('quantity')}米 "
                              f"¥{draft.get('unit_price')}/米 总价¥{draft.get('total')}")
                        decision = input("审批 (y=通过 / n=拒绝[原因]): ").strip()
                        approved = decision.lower().startswith("y")
                        reason = "" if approved else decision[1:].strip()
                        result = app.invoke(
                            Command(resume={"approved": approved, "reason": reason}),
                            config=thread_config(user_id),
                        )
                state = result

            print(f"\n🤖 客服: {result['messages'][-1].content}")

            # 存档本轮新消息
            memory.save_messages([HumanMessage(content=user_input), result["messages"][-1]])

            # 异步提取偏好（有界任务队列，不阻塞主线程）
            from src.task_queue import get_extraction_queue
            get_extraction_queue().submit(memory.extract_and_store, state["messages"], cheap_llm)
    finally:
        mcp.shutdown()
        from src.task_queue import get_extraction_queue
        get_extraction_queue().shutdown()


if __name__ == "__main__":
    main()
