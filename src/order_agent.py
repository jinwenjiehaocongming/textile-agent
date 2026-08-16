"""
下单 Agent — 独立 Agent，只管订单
====================================
自带产品查询工具 + 订单写入工具。不依赖售前 Agent。
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

order_llm = ChatOpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
    model="deepseek-v4-flash",
    temperature=0,
)

# ============================================================
# 工具已移至 MCP Server
#   product_server.py → search_product
#   order_server.py   → create_order
# ============================================================


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

## 客户标识（重要）
- create_order 的 customer_id 参数必须填系统提供的值：{customer_id}
- 不要用电话号码、"客户"、"微信客户" 等代替，不要自己编造

处理命令："""


def order_agent_node(messages: list, customer_id: str = "guest") -> AIMessage:
    """
    下单 Agent：先查产品确认，再下单。
    工具通过 MCP Client 自动发现，不再硬编码 Schema。

    Args:
        messages:    对话历史
        customer_id: 系统注入的当前用户标识（微信 external_userid），
                     直接作为 create_order 的 customer_id 参数。
    """
    mcp = get_mcp()

    # 下单只绑查产品 + 创建订单
    llm_with_tools = order_llm.bind_tools(
        mcp.get_tools_for_langchain(["search_product", "create_order"])
    )

    # 对话历史作为真正的 message 列表传入（不再拼进 system prompt）
    history_msgs = [
        HumanMessage(content=m.content) if m.type == "human" else AIMessage(content=m.content)
        for m in messages[-10:]
        if m.type in ("human", "ai") and m.content
    ]

    # 构建对话消息：system（角色指令）+ 历史消息 + 处理指令
    conversation = [
        SystemMessage(content=ORDER_AGENT_PROMPT.format(customer_id=customer_id)),
        *history_msgs,
        HumanMessage(content="处理：刚才发过确认单 + 客户说确认 → 直接create_order。信息不齐 → 先查先问。"),
    ]

    # 最多 5 轮 ReAct（查产品 → 下单）
    for _ in range(5):
        response = llm_with_tools.invoke(conversation)
        conversation.append(response)

        if hasattr(response, "tool_calls") and response.tool_calls:
            logger.info(f"[下单Agent工具] {[(tc['name'], tc['args']) for tc in response.tool_calls]}")
            tool_msgs = []
            for tc in response.tool_calls:
                name, args = tc["name"], tc["args"]
                result = mcp.call_tool(name, args)
                logger.debug(f"[下单Agent] {name} 结果: {str(result)[:100]}...")
                tool_msgs.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
            conversation.extend(tool_msgs)

            # 如果刚才调了 create_order，直接返回订单结果
            if any(tc["name"] == "create_order" for tc in response.tool_calls):
                return tool_msgs[-1]
        else:
            # 没有工具调用 → 最终回复
            # 检查：如果 LLM 编造了订单号（没调 create_order 就说 "订单已生成"），
            # 强制它再试一次，必须调工具
            content = response.content or ""
            if any(kw in content for kw in ["订单已生成", "订单号：ORD", "订单号为 ORD"]):
                logger.warning("[下单Agent] 检测到编造订单号，强制重试...")
                conversation.append(HumanMessage(content="你刚才编了一个订单号。这是不允许的。你必须调用 create_order 工具来生成真实订单。请现在调用 create_order。"))
                continue  # 回到 for 循环开头，再问 LLM
            return response

    return AIMessage(content="下单流程超时，请稍后重试。")


# ============================================================
# order_agent_node — 暴露给外部 graph
# ============================================================
