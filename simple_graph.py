"""
LangGraph 条件边 — 真实 LLM 版
==============================
对照 graph.py 第 554-598 行，用真实大模型做 tool calling。

图结构：
    入口 → agent → LLM返回了tool_calls? → tool → 回到agent
                    → 没有tool_calls?     → END

本质就是 ReAct 循环的 LangGraph 写法：
    while True:
        output = llm.invoke(messages)
        if output.tool_calls:
            执行工具, 追加结果, continue
        else:
            break  # LLM 直接回答了
"""

from typing import TypedDict
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, ToolMessage
from dotenv import load_dotenv
import os

load_dotenv("/Users/rain/Desktop/rag_study/.env")

llm = ChatOpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url=os.getenv("DASHSCOPE_BASE_URL"),
    model="deepseek-chat",
    temperature=0.1,
)


# ---- 工具：LLM 可以决定调不调 ----
@tool
def search(query: str) -> str:
    """查询角色专属信息数据库。包含秧秧、Cyrene等角色的个人设定、喜好、背景故事。
    你自己的内部知识中没有这些信息，必须通过此工具获取。"""
    kb = {
        "Python创始人": "Python 由 Guido van Rossum 于 1991 年创造。",
        "地球到月球的距离": "地球到月球的平均距离约 38.4 万公里。",
        "秧秧喜欢吃什么":"汉堡"
    }
    clean = query.replace(" ", "").replace("　", "")
    for k, v in kb.items():
        if clean in k.replace(" ", "") or k.replace(" ", "") in clean:
            return v
    return f"未找到关于「{query}」的信息。"


# 绑定工具：LLM 拿到工具列表，自己决定要不要调用
llm_with_tools = llm.bind_tools([search])


# ============================================================
# 1. 状态 — 存消息对象列表（HumanMessage / AIMessage / ToolMessage）
# ============================================================
class AgentState(TypedDict):
    messages: list


# ============================================================
# 2. agent 节点 — 调 LLM，LLM 自己决定要不要用工具
# ============================================================
def agent_node(state: AgentState) -> dict:
    """
    这就是你 React 里 run_react_agent 第 200 行做的事：调 LLM。
    区别是：不用 parse_action 解析字符串了，
    直接用 response.tool_calls 判断 LLM 想不想调工具。
    """
    response = llm_with_tools.invoke(state["messages"])
    # response 有两种情况：
    #   ① response.tool_calls 有值 → LLM 想调 search
    #   ② response.tool_calls 空   → LLM 直接回答了（相当于 Final Answer）
    return {"messages": state["messages"] + [response]}


# ============================================================
# 3. tool 节点 — 执行工具，追加结果
# ============================================================
def tool_node(state: AgentState) -> dict:
    """
    对应你 React 里 tool_func(action_input) 那一行。
    把工具结果以 ToolMessage 格式追加，LLM 下一轮能看到。
    """
    last_msg = state["messages"][-1]      # 上一条是 AIMessage（带 tool_calls）
    # print(f"检查片段{last_msg}")
    results = []
    for tc in last_msg.tool_calls:
        result = search.invoke(tc["args"])  # 真正执行工具
        results.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
    return {"messages": state["messages"] + results}


# ============================================================
# 4. 路由函数 — 判断要不要继续循环
# ============================================================
def should_continue(state: AgentState) -> str:
    """
    这就是你 React 里的：
        if "Action:" in output → 执行工具
        if "Final Answer:" in output → 停

    只是换成了检查 tool_calls。
    """
    last_msg = state["messages"][-1]
    if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
        return "tool"    # LLM 想调工具 → 走 tool 节点
    return END           # LLM 直接回答了 → 停


# ============================================================
# 5. 建图
# ============================================================
def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("agent", agent_node)
    builder.add_node("tool", tool_node)
    builder.set_entry_point("agent")
    builder.add_conditional_edges("agent", should_continue, {
        "tool": "tool",
        END: END,
    })
    builder.add_edge("tool", "agent")    # 工具执行完回到 agent，形成循环
    return builder.compile()


# ============================================================
# 6. 跑起来
# ============================================================
def main():
    app = build_graph()

    questions = [
        "你好，今天天气不错",
        "Python 是谁创造的？",
        "秧秧喜欢吃什么"
    ]

    for q in questions:
        print(f"\n{'='*50}")
        print(f"❓ {q}")
        result = app.invoke({"messages": [HumanMessage(content=q)]})
        # print(result)
        for s in result["messages"]:
            if s.type=="tool":
                print(f"检查片段{s.content}")
        for m in result["messages"]:
            role = m.type
            extra = ""
            if hasattr(m, "tool_calls") and m.tool_calls:
                extra = f" [🔧 调用: {[tc['name'] for tc in m.tool_calls]}]"
            snippet = m.content[:80] if m.content else ""
            print(f"  [{role}]{extra} {snippet}")


if __name__ == "__main__":
    main()
