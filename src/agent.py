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

# 离线模式：必须在所有 import 之前，否则 chromadb/huggingface_hub 会联网
import os
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import readline  # 修复终端中文输入退格残留
import json, re, sys
from pathlib import Path
from typing import TypedDict, List
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage
from dotenv import load_dotenv
import os

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.retrieval import HybridRetriever
from src.order_agent import order_agent_node as order_agent, query_order
from src.after_sales_agent import after_sales_node as after_sales_agent_fn
from src.memory import get_user

load_dotenv()

# ============================================================
# 0. 配置
# ============================================================
import time as _time

def _safe_llm(llm_inst, messages, fallback=None):
    """重试2次（1s/2s）→ 降级 → 保底。所有LLM调用的统一入口。"""
    for i in range(3):
        try:
            return llm_inst.invoke(messages)
        except Exception as e:
            if i < 2:
                _time.sleep(i + 1)
            else:
                if fallback is not None:
                    print(f"   ⚠️ LLM 降级: {str(e)[:60]}")
                    return fallback
                raise

# 主 LLM：Agent 对话用 DeepSeek
llm = ChatOpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
    model="deepseek-v4-flash", temperature=0.3,
    max_retries=2, timeout=30,
)

# 辅助 LLM：改写 + 审核 + Supervisor
cheap_llm = ChatOpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
    model="deepseek-v4-flash", temperature=0.1,
    max_retries=2, timeout=15,
)

import sqlite3
DB_PATH = Path(__file__).parent.parent / "data" / "products.db"

retriever = HybridRetriever()

# ============================================================
# 1. 产品查询工具 — JSON Schema 约束
# ============================================================
# 下单 Agent 也要能查订单
QUERY_ORDER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "query_order_status",
        "description": "查询订单状态。客户提供了订单号(ORD-开头的)就能查。",
        "parameters": {
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "订单号，格式 ORD-20260720-xxxxxx"},
            },
            "required": ["order_no"],
        },
    },
}

SEARCH_PRODUCT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_product",
        "description": "查询产品库存和报价。用于客户询问价格、库存、MOQ、交期，按名称/颜色/品类搜面料，对比不同产品。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "产品名、颜色或品类关键词，如 'T400 黑色'、'牛津布'、'里料'",
                }
            },
            "required": ["query"],
        },
    },
}


def search_product(query: str) -> str:
    """
    从 SQLite 查询产品。数据库做过滤 + 截断，Python 只做轻量计分排序。
    """
    keywords = [kw.strip().lower() for kw in query.replace("，", " ").replace(",", " ").split() if kw.strip()]
    if not keywords:
        return "请输入产品名、颜色或品类关键词。"

    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row

        where_parts = []
        params = []
        for kw in keywords:
            p = f"%{kw}%"
            where_parts.append("(name LIKE ? OR color LIKE ? OR category LIKE ?)")
            params.extend([p, p, p])

        rows = conn.execute(
            f"SELECT * FROM products WHERE {' OR '.join(where_parts)} LIMIT 50",
            params
        ).fetchall()

        if not rows:
            fragments = set()
            for kw in keywords:
                if len(kw) > 2:
                    for i in range(len(kw) - 1):
                        fragments.add(kw[i:i+2])
            if fragments:
                where_parts = []
                params = []
                for fg in fragments:
                    p = f"%{fg}%"
                    where_parts.append("(name LIKE ? OR color LIKE ? OR category LIKE ?)")
                    params.extend([p, p, p])
                rows = conn.execute(
                    f"SELECT * FROM products WHERE {' OR '.join(where_parts)} LIMIT 50",
                    params
                ).fetchall()

        conn.close()

        if not rows:
            return "未找到匹配产品。请尝试直接用面料名称（如 T400、牛津布、春亚纺、尼丝纺）搜索。"

        scored = []
        for r in rows:
            text = f"{r['name']} {r['color']} {r['category']} {r['weight']} {r['id']}".lower()
            score = sum(1 for kw in keywords if kw in text)
            scored.append((score, r))

        scored.sort(key=lambda x: x[0], reverse=True)
        top10 = [r for _, r in scored[:10]]

        lines = []
        for r in top10:
            lines.append(
                f"货号:{r['id']} | {r['name']} | {r['color']} | 门幅:{r['width']}cm "
                f"| {r['weight']} | 库存:{r['stock']}米 | MOQ:{r['moq']}米 "
                f"| ¥{r['price']}/米 | 交期:{r['delivery_days']}天"
            )
        return "\n".join(lines)
    except Exception:
        return "产品查询暂时不可用，请稍后重试或联系销售经理。"


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
        print(f"\n📝 [检索词] 跳过（闲聊）")
        return {"rewrite_query": last_msg}

    # 取最近几轮摘要
    history = "\n".join(
        f"{'客户' if m.type == 'human' else '客服'}: {m.content[:60]}"
        for m in messages[-6:]
    )

    print(f"\n📝 [检索词] 原文: {last_msg}")

    try:
        resp = _safe_llm(cheap_llm, [
            HumanMessage(content=QUERY_REFORMULATOR_PROMPT.format(history=history, query=last_msg))
        ], fallback=HumanMessage(content=last_msg))  # 挂了用原话检索
        reformulated = resp.content.strip()
    except Exception:
        reformulated = last_msg

    combined = f"{last_msg}\n{reformulated}"
    print(f"   → 改写: {reformulated}")
    return {"rewrite_query": combined}


# ============================================================
# 5. 检索节点（必选）
# ============================================================
def context_retriever(state: AgentState) -> dict:
    """用生成的检索词查知识库。闲聊跳过，省 embedding + Rerank 调用。"""
    last_msg = state["messages"][-1].content
    if should_skip_retrieval(last_msg):
        print(f"\n🔍 [检索] 跳过（闲聊）")
        return {"knowledge_chunks": []}

    query = state.get("rewrite_query", last_msg)
    print(f"\n🔍 [检索] 查询: {query}")

    results = retriever.retrieve(query, top_k=5, use_rerank=True)
    chunks = [r["text"] for r in results]

    print(f"   → 命中 {len(chunks)} 条: {[r['category'] for r in results]}")
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

## 面料知识参考
{knowledge}

## 客户历史档案
{user_context}"""


def agent_node(state: AgentState) -> dict:
    messages = state["messages"]
    chunks = state.get("knowledge_chunks", [])
    knowledge_text = "\n---\n".join(chunks) if chunks else "（无参考知识）"

    print(f"\n🤖 [Agent] msgs={len(messages)}, chunks={len(chunks)}")

    user_context = state.get("user_context", "")
    system = SystemMessage(content=SYSTEM_PROMPT.format(
        knowledge=knowledge_text,
        user_context=user_context or "（新客户，暂无历史档案）"
    ))
    llm_with_tools = llm.bind_tools([SEARCH_PRODUCT_SCHEMA, QUERY_ORDER_SCHEMA])
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
    response = llm_with_tools.invoke([system] + safe)
    response = _safe_llm(llm_with_tools, [system] + safe,
        fallback=AIMessage(content="系统繁忙，请稍后重试。如有紧急需求请联系销售经理。"))
    if hasattr(response, "tool_calls") and response.tool_calls:
        print(f"   🔧 调用: {[(tc['name'], tc['args']) for tc in response.tool_calls]}")
    else:
        print(f"   💬 回答长度: {len(response.content) if response.content else 0} 字")

    return {"messages": state["messages"] + [response]}


# ============================================================
# 6. 工具执行节点
# ============================================================
def tool_executor(state: AgentState) -> dict:
    last_msg = state["messages"][-1]
    results = []
    for tc in last_msg.tool_calls:
        name, args = tc["name"], tc["args"]
        print(f"\n⚙️ [工具] {name}({args})")
        if name == "search_product":
            result = search_product(**args)
        elif name == "query_order_status":
            result = query_order(**args)
        else:
            result = f"未知工具: {name}"
        print(f"   ✅ 结果: {result}")
        results.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
    return {"messages": state["messages"] + results}


# ============================================================
# 7. 审核节点（必选）
# ============================================================
def review_node(state: AgentState) -> dict:
    last_msg = state["messages"][-1]
    if last_msg.type != "ai" or not last_msg.content:
        print("\n🛡️ [审核] 跳过（非 AI 消息）")
        return {}

    verdict = review_response(last_msg.content)
    if not verdict["safe"]:
        print(f"\n🛡️ [审核] 拦截: {verdict['reason']}")
        rewrite = verdict.get("rewrite", "抱歉，这个信息暂时无法提供。")
        safe = AIMessage(content=rewrite)
        return {"messages": state["messages"][:-1] + [safe]}
    print(f"\n🛡️ [审核] 通过")
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
            print("\n🧭 [Supervisor] → 售前 Agent（已完成）")
            return {"query_type": "chat"}

    # Layer 1: 规则延续
    if _detect_continuation(messages):
        labels = {"chat": "售前", "place_order": "下单", "after_sales": "售后"}
        print(f"\n🧭 [Supervisor] 延续 → {labels.get(prev, prev)}")
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
    print(f"\n🧭 [Supervisor] → {labels[result]}")
    return {"query_type": qtypes[result]}


def order_agent_node(state: AgentState) -> dict:
    """下单 Agent。完成后状态切回售前。"""
    print("\n📋 [下单Agent] 处理订单...")
    reply = order_agent(state["messages"])
    result = {"messages": state["messages"] + [reply]}
    # 下单完成 → 切回售前，避免"你好"也走下单
    if hasattr(reply, "content") and "ORD-" in str(reply.content):
        result["query_type"] = "chat"
    return result


def after_sales_agent_node(state: AgentState) -> dict:
    """售后 Agent。完成后状态切回售前。"""
    print("\n🔧 [售后Agent] 处理售后...")
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
def build_graph():
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

    return builder.compile()


# ============================================================
# 10. 测试
# ============================================================
def main():
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
            state = {"messages": [], "knowledge_chunks": [], "rewrite_query": "",
                     "query_type": "chat", "user_id": user_id, "user_context": ""}
            print("🔄 对话记录已清除")
            continue

        state["messages"] = state["messages"] + [HumanMessage(content=user_input)]
        state["knowledge_chunks"] = []
        state["rewrite_query"] = ""

        state["messages"] = state["messages"] + [HumanMessage(content=user_input)]
        state["knowledge_chunks"] = []
        state["rewrite_query"] = ""

        result = app.invoke(state)
        state = result

        print(f"\n🤖 客服: {result['messages'][-1].content}")

        # 存档本轮新消息
        memory.save_messages([HumanMessage(content=user_input), result["messages"][-1]])

        # 异步提取偏好（后台线程，不阻塞）
        import threading
        threading.Thread(
            target=memory.extract_and_store,
            args=(state["messages"], cheap_llm),
            daemon=True,
        ).start()


if __name__ == "__main__":
    main()
