"""
售后 Agent — 退货退款、质量问题投诉
=====================================
独立 Agent，查订单 + 对照退货规则 + 生成退款工单。
不碰产品库、不报价。
"""

import sys
from pathlib import Path
from dotenv import load_dotenv
import os

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()
from src.logging_config import get_logger
logger = get_logger(__name__)

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from src.mcp_client import get_mcp
from src.llm_utils import _safe_llm
from src.render_tools import (
    RENDER_TOOLS, RENDER_TOOL_NAMES, RENDER_PROMPT_HINT,
    attach_render_data, find_render_data_in_msgs,
)

after_sales_llm = ChatOpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
    model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
    temperature=0,
    max_retries=2, timeout=30,
)

# ============================================================
# 工具已移至 MCP Server
#   refund_server.py → query_order, create_refund
# ============================================================


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

请处理客户的售后需求。"""


def after_sales_node(messages: list) -> AIMessage:
    """售后 Agent 的对话节点。工具通过 MCP Client 自动发现。"""
    mcp = get_mcp()

    # 售后只绑查订单 + 创建退款 + 展示工具
    llm_with_tools = after_sales_llm.bind_tools(
        mcp.get_tools_for_langchain(["query_order", "create_refund"]) + RENDER_TOOLS
    )

    # 对话历史作为真正的 message 列表传入（不再拼进 system prompt）
    history_msgs = [
        HumanMessage(content=m.content) if m.type == "human" else AIMessage(content=m.content)
        for m in messages[-10:]
        if m.type in ("human", "ai") and m.content
    ]

    conversation = [
        SystemMessage(content=AFTER_SALES_PROMPT + "\n" + RENDER_PROMPT_HINT),
        *history_msgs,
        HumanMessage(content="请处理客户的售后需求。"),
    ]

    for _ in range(5):
        response = _safe_llm(llm_with_tools, conversation,
                             fallback=AIMessage(content="系统繁忙，请稍后重试。您的售后需求已记录，客服会尽快联系您。"),
                             stream_tokens=True)  # 真流式：回复 token 逐字推给 SSE
        conversation.append(response)

        if hasattr(response, "tool_calls") and response.tool_calls:
            tool_msgs = []
            for tc in response.tool_calls:
                name, args = tc["name"], tc["args"]
                # 展示工具是数据透传协议，不执行
                if name in RENDER_TOOL_NAMES:
                    tool_msgs.append(ToolMessage(content="", tool_call_id=tc["id"]))
                    continue
                result = mcp.call_tool(name, args)
                logger.info(f"[售后] {name}({args})")
                tool_msgs.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
            conversation.extend(tool_msgs)

            if any(tc["name"] == "create_refund" for tc in response.tool_calls):
                tool_msg = tool_msgs[-1]
                return attach_render_data(tool_msg, find_render_data_in_msgs(conversation))
        else:
            return attach_render_data(response, find_render_data_in_msgs(conversation))

    return AIMessage(content="售后处理超时，请稍后重试或转人工客服。")
