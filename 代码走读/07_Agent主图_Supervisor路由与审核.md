# 07 · Agent 主图 · Supervisor 路由与审核

> 目标：看懂系统"心脏"——`src/agent.py`（646 行）的 LangGraph 图：消息按什么顺序流过
> 哪些节点、怎么分派到售前/下单/售后、为何所有回复都逃不过"审核"、checkpointer / thread_id
> 在 HITL 里的角色（挂起细节只点位置 order_agent.py:159，完整机制在 08 步）。

---

## 一、先建立心智模型：图 = 带工单的流水线

```
客户消息（+历史 20 条）
  → ① query_reformulator  改写查询（口语→适合检索的问句）
  → ② context_retriever   知识检索（必走一步，闲聊才跳过）
  → ③ supervisor          意图路由（规则分层 + LLM 兜底）
       ├─ chat(售前)      → agent ──→ 有工具调用? ──是──→ tool_executor ──↺ 回 agent
       │                          └──否──→ review → END
       ├─ place_order(下单) → order_agent       → review → END
       └─ after_sales(售后) → after_sales_agent → review → END
```

类比：**带工单的生产流水线**——每条客户消息是一张工单（state），每个节点是工位，
只改自己负责的那几栏就传给下一工位，节点之间只通过工单传话。这就是"状态机"
和"随手写循环调函数"的本质区别。

文件头三行注释是全系统的宪法，也是本文件三条暗线（agent.py:4-7）：
`① 知识库检索是必选项`（§六检索节点）、`② 产品查询是工具`（§七 bind_tools）、
`③ 审核是必选项`（§八 review 必经节点）。下方"企业级演进（2026-08）"（agent.py:13-18）
交代历史：`app.invoke`（同步）→ `graph.ainvoke`（async 节点）；手写工具 → 官方 MCP
SDK；SQLite/Chroma → PostgreSQL/Qdrant。**留意第 9-11 行还写着"图结构（4 个节点）"
——单 Agent 时代的过期注释**，今天的图有 8 个节点（§五）。能指出"注释跟不上代码"，
面试很加分。

## 二、模块头：离线设置为什么必须在 import 前（agent.py:20-26）

```python
# 离线模式：必须在所有 import 之前设置。
import os
from pathlib import Path

if Path(os.path.expanduser("~/.cache/huggingface")).exists():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
```

`HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` 告诉 HuggingFace 生态"只准用本地缓存，别联网"。
为什么必须在所有 import 之前？因为 `sentence_transformers` / `transformers` 在
**import 时**就读环境变量并尝试联网发现模型——设晚了模型已经卡在连 huggingface.co。
还做了聪明判断：`~/.cache/huggingface` **存在才设离线**（有缓存才离线，首次要下模型
的环境保持联网）。`src/retrieval.py:27-31` 有同样两行（历史重复，无害）。

## 三、LLM 懒加载：import 时不建 LLM（agent.py:59-96）

```python
_llm = None; _cheap_llm = None                       # 61-62：初始 None，第一次调用才建

def get_llm() -> ChatOpenAI:          # 74  主 LLM：节点内显式调用
    global _llm
    if _llm is None:
        _llm = _make_llm(temperature=0.3, timeout=30)   # _make_llm:65
    return _llm

def get_cheap_llm() -> ChatOpenAI:    # 82  便宜 LLM：改写/路由/审核用
    # 同样的懒加载，temperature=0.1, timeout=15
```

`_llm` / `_cheap_llm` 初始 `None`，**第一次调用 `get_*()` 才 new ChatOpenAI**。
为什么懒加载？根目录 `问题.md` 记着一堂历史课：

> 懒加载：模块 import 时不建 LLM，第一次调用才建。原因是历史教训——早期 import
> 就实例化 ChatOpenAI，CI 没有 .env 直接崩（git 提交 b1b5283 修复）。这也让
> import src.agent 无副作用，测试友好。

ChatOpenAI 构造就要 `api_key`：CI/测试没 `.env` 时 `import src.agent` 直接崩在
import 阶段，测试没跑先全红。懒加载后 **import 永远成功，缺 key 只有真调用时才
暴露**——`test_supervisor.py` 敢直接 import 节点函数，前提就是它。

两个 LLM 的分工（面试爱问"为什么要两个"）：主 LLM（temperature 0.3、timeout 30s，
agent.py:78）管售前最终回答、要质量；便宜 LLM（0.1、15s，agent.py:86）管改写/路由/
审核这些"分类活"、要快要稳（0.1 少发散）。每请求最多 3-4 次 LLM 调用，主/便宜分开省成本。

兼容层（agent.py:90-96）：

```python
def __getattr__(name: str):
    # 兼容外部 `from src.agent import cheap_llm` / `src.agent.llm` 的属性访问
    if name == "llm": return get_llm()
    if name == "cheap_llm": return get_cheap_llm()
    raise AttributeError(...)
```

模块级 `__getattr__`：属性不存在时解释器回调它兜底——老代码 `from src.agent import
cheap_llm` 这种属性式访问改造后依然不炸，是"渐变改造"典型手法（`问题.md`）。
但 get_llm 的 docstring（agent.py:75）提醒：模块级 `__getattr__` 只在**属性访问**时
触发，函数体内自由引用不生效——所以节点代码一律显式 `get_llm()`。

半诚实观察：模块第 99 行 `retriever = HybridRetriever()` 是 import 时就执行的——
它要读 `index/bm25_index.pkl`（retrieval.py:173-178），所以"无副作用"严格说是
"**不建 LLM、不碰网络**"：没 `.env` 能 import，缺索引文件会 FileNotFoundError。
Docker/评测脚本"先建索引再启动"的顺序是有讲究的（16 步）。

## 四、状态：工单长什么样（agent.py:163-169）

```python
class AgentState(TypedDict):
    messages: list            # 本轮全部消息（历史+新问题+生成中的回复）
    knowledge_chunks: List[str]  # 检索到的知识片段（喂给 LLM 当参考）
    rewrite_query: str        # 改写后的检索词
    query_type: str           # chat | place_order | after_sales（意图，边间传话用）
    user_id: str              # 谁在说话（隔离/记忆/工具调用都靠它）
    user_context: str         # 用户长期偏好摘要（Qdrant 里取，12 步）
```

关键点：**没有声明任何 reducer**（如 `add_messages`）——LangGraph 对没注解的键默认
"最后写入者覆盖"。所以节点都 `return {"messages": state["messages"] + [新消息]}`
（如 agent.py:309），**带整份列表手动追加**：状态流一目了然、无隐式合并魔法；
代价是每轮拷贝整份列表，历史只带 20 条就是控制它（06 步）。能讲出"为什么不用
add_messages 注解"，就是真懂 LangGraph 语义。

`query_type` 是图的"方向盘"：supervisor 写它、`supervisor_router` 读它决定下一步
（§六）；它独立于 messages 存在，是"路由意图跨轮延续"的载体（见 Q3）。

## 五、建图：8 个节点怎么连（agent.py:527-558）

```python
def build_graph(checkpointer=None):
    builder = StateGraph(AgentState)
    builder.add_node("query_reformulator", query_reformulator)   # 530
    builder.add_node("context_retriever", context_retriever)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("agent", agent_node)
    builder.add_node("tool_executor", tool_executor)
    builder.add_node("review", review_node)
    builder.add_node("order_agent", order_agent_node)
    builder.add_node("after_sales_agent", after_sales_agent_node)  # 537

    builder.set_entry_point("query_reformulator")                # 539
    builder.add_edge("query_reformulator", "context_retriever")  # 540
    builder.add_edge("context_retriever", "supervisor")          # 541
    builder.add_conditional_edges("supervisor", supervisor_router, {   # 542
        "agent": "agent", "order_agent": "order_agent", "after_sales_agent": "after_sales_agent"})
    builder.add_conditional_edges("agent", agent_router, {            # 547
        "tool_executor": "tool_executor", "review": "review"})
    builder.add_edge("tool_executor", "agent")                   # 551
    builder.add_edge("review", END)                              # 552
    builder.add_edge("order_agent", "review")                    # 553
    builder.add_edge("after_sales_agent", "review")              # 554

    if checkpointer is None:
        checkpointer = MemorySaver()                             # 557
    return builder.compile(checkpointer=checkpointer)            # 558
```

三种边：`add_edge` 无脑直连；`add_conditional_edges` 由**路由函数读 state 返回 key**
决定去向——`supervisor_router`（agent.py:503-509）读 `query_type`：
`place_order → order_agent`、`after_sales → after_sales_agent`、其余 → `agent`；
`agent_router`（agent.py:515-519）看最后一条消息有无 `tool_calls`——有就去
`tool_executor`，没有说明 Agent 想好答案了，去 `review`。
**循环只有一个**：`agent → tool_executor → agent`（ReAct 工具循环），直到 Agent
不再要工具才走向 review；order/after_sales 两条分支是直线（它们内部自管工具）。

**checkpointer 与 thread_config**（agent.py:522-524）：

```python
def thread_config(user_id: str) -> dict:
    """thread_id = user_id（HITL interrupt 依赖它定位会话）。"""
    return {"configurable": {"thread_id": user_id}}
```

checkpointer = 图的"存档系统"，每跑完一步把 state 按 `thread_id` 存一份；这里
**thread_id 直接用 user_id**（一人一条存档线）。作用链：`app.py:99` 起每个请求都
`thread_config(user_id)` 跑图 → 下单 interrupt 挂起时状态已按该 user 存档 → 审批时
（08 步 `/approval/approve`）用同一 thread_id + `Command(resume=...)` 唤醒**同一张被
挂起的图**继续跑——没有 checkpointer 这不可能。默认 `MemorySaver()`（agent.py:557）
= 内存存档，进程重启就丢（取舍见 Q2）。

## 六、入口三连：改写 → 检索 → 路由

### 改写 query_reformulator（agent.py:191-215）

闲聊拦截先行（agent.py:152-157）：`SKIP_KEYWORDS = ["你好","在吗","谢谢",...]`，
`should_skip_retrieval` 要求消息 **≤4 字且含关键词**——防 "多少钱" 这类短句被误伤。
改写调便宜 LLM（提示词 agent.py:175-188）：教它"消解指代、补全省略、生成 2-3 个
短语"，如"那白色的呢"→ 结合历史补成 "T400白色规格库存"。返回
`combined = f"{last_msg}\n{reformulated}"`（agent.py:213）——**原文+改写一起当检索词**
（原文保底、改写加分）。LLM 挂了走 fallback/except 回原文（agent.py:206-211）。

### 检索 context_retriever（agent.py:221-235）

对应原则 1"检索是必选项"——除了闲聊，**每轮都先搜知识库**，不许凭记忆说面料参数。
核心三行：

```python
query = state.get("rewrite_query", last_msg)
results = await retriever.retrieve(query, top_k=5, use_rerank=rerank_enabled())  # 231
chunks = [r["text"] for r in results]
return {"knowledge_chunks": chunks}
```

`retriever.retrieve` = Qdrant 向量 + BM25 + RRF 融合（10 步）；`rerank_enabled()`
默认关（retrieval.py:32-39）——CrossEncoder 加载约 1GB 内存，混合检索本身
Hit@3 已 100%（01 步数据），重排只在评测/需要时开。

### Supervisor 意图路由（agent.py:388-464）——本文件的灵魂

目标就三个：售前（sales）/ 下单（order）/ 售后（after_sales），内部用
`chat / place_order / after_sales` 表达。**不靠一次 LLM 分类定生死，而是
从快到慢逐层试**——省 LLM 调用是表层理由，深层是"多轮延续"必须靠规则先兜住（Q3）：

| 层 | 条件 | 结果 | 代码 |
|---|---|---|---|
| Layer 0 | 最后一条是**工具结果**且含 `ORD-`/`退款工单` | 刚完成 → 切回 `chat` | 393-397 |
| Layer 0.5 | `prev=="place_order"` 且用户发短确认词（"确认/可以/好的/行…"≤4 字） | 无条件延续下单 | 403-408 |
| Layer 0.6 | `prev=="place_order"` 且用户没转话题、上一条 AI 在收集信息/问句 | 客户在补电话/地址 → 继续下单 | 414-437 |
| Layer 1 | `_detect_continuation`（353-373）：上轮 AI 提问、这轮用户短答 | 延续 `prev` | 440-443 |
| Layer 2 | LLM 分类，只输出一个词 | sales/order/after_sales → 内部三态 | 445-464 |

细节与事故：
- **Layer 0.5 是 2026-09 的关键修复**（注释在 agent.py:400-402）：上一轮下单 Agent
  只查了价、没输出标准确认单时，`_detect_continuation` 的关键词命中会失效，
  "确认"会被 LLM 误判成售前 → 下单挂半路。修法：prev=place_order + 短确认词 →
  不依赖上轮 AI 文本、不调 LLM，直接延续；
- Layer 2 的提示词（agent.py:376-385）只让模型"只输出一个词"，结果做白名单
  （agent.py:458-459）：不在三词内一律回落 `sales`——**不确定就选售前，宁放错不瞎派工**；
- 为什么规则层不够、还要 LLM？自然语言变体太多（"发错了我要换"）；为什么纯 LLM 不够？
  多轮延续会被误判。**规则管确定性的延续，LLM 管开放式的分类**。

### query_type 的状态传播（对应 06 步的"关键修复"）

跨轮延续的完整链路（多轮下单能走通的核心，务必背下）：第 N 轮开头
`state.query_type` 取自 profile 里存的 `last_query_type`（app.py:113 / stream_chat.py:34）
→ supervisor 据此做延续判断 → 图跑完后再 `save_last_query_type(...)` 写回 profile
（app.py:131 / stream_chat.py:180-182）→ 第 N+1 轮读出作 prev，延续生效。

06 步讲过：流式路径曾**不持久化 query_type**，"下单确认/补全信息"永远路由不回下单
Agent（stream_chat.py:178-179 注释自述关键修复）。诚实补个隐患：`save_last_query_type`
按 **user_id 不分 session**（memory.py:127-133）——同用户两个会话交错（一个下单、一个
闲聊）会互相污染起点。演示量级没暴露，但这是多会话时代的模型缺口，主动点出很加分。

## 七、分支三兄弟

### 售前 agent 节点（agent.py:267-309）

组装 SystemMessage（system prompt，agent.py:241-264）→ **bind 工具**
（agent.py:278-281）：`mcp.get_tools_for_langchain(["search_product","query_order_status"])`
（MCP 动态发现，04/11 步）+ `RENDER_TOOLS`。prompt 红线（agent.py:244-256）：
价格必须查工具不许编、知识优先参考检索结果、不透露成本价、**查单只许回显工具返回的
真实订单，严禁自编 ORD- 订单号**（14 步评测断言项）。调 LLM 前先过滤"孤立
ToolMessage"（agent.py:283-297）——工具调用与结果必须成对 API 才收。然后
（agent.py:299-303）：

```python
response = await _safe_llm_async(
    llm_with_tools, [system] + safe,
    fallback=AIMessage(content="系统繁忙，请稍后重试。如有紧急需求请联系销售经理。"),
    stream_tokens=True,          # ← 全图唯一开流的位置（06 步"只在最终回复开流"的落点）
)
```

若返回带 `tool_calls` → 追加 messages → `agent_router` 送 tool_executor。

### 工具执行 tool_executor（agent.py:315-328）

逐个执行最后一条 AI 消息的 `tool_calls`：**render 工具跳过执行**（agent.py:320-323）——
`RENDER_TOOLS` 是"数据展示协议"：让 LLM 把产品/订单数据以 JSON 从 tool_calls 里"交出来"
给前端画表格（render_tools.py:1-16），后端只补空 ToolMessage 占位让格式成对；其余走
`await get_mcp().call_tool(name, args)`（agent.py:325）——官方 MCP 异步客户端
（mcp_client.py:65-77，失败返回可读文本不抛错，会当工具结果喂回 LLM 解释）。

**工具循环没有显式上限**：全仓无 recursion_limit/max_iterations 配置，兜底靠 LangGraph
默认递归上限（25 步）抛 GraphRecursionError → stream_chat 的 except 转 error SSE
（stream_chat.py:97-98）——目前是"靠框架默认值兜着"的取舍。

### 下单 / 售后节点（agent.py:483-500）

```python
async def order_agent_node(state: AgentState) -> dict:
    reply = await order_agent(state["messages"], customer_id=state.get("user_id", "guest"))
    result = {"messages": state["messages"] + [reply]}
    if getattr(reply, "additional_kwargs", {}).get("order_completed"):   # 489
        result["query_type"] = "chat"     # 真实下单成功 → 意图复位
    return result
```

- 真正下单逻辑（查产品 → 确认单 → **interrupt 挂起** → 写订单）在 `src/order_agent.py`，
  本节点只是"接线员"；**interrupt() 调用点在 order_agent.py:159**（08 步全讲），
  它把整张图"冻结"在那里等审批；
- 完成判定的讲究（agent.py:487-491 + `_is_order_completed` 467-481）：完成与否只看
  `create_order` 工具真实成功后挂的 `additional_kwargs["order_completed"]` 标记——
  **不扫 LLM 文本**：LLM 可能复述历史订单号（"您之前的订单 ORD-xxx"），按文本判定会
  把还在下单的用户误判成完成、踢回售前（2026-09 修复）。文本判断降级成 CLI/旧消息兜底。
  **机器可验证的信号永远优先于 LLM 的自由文本**——最值得写进简历的工程原则；
- 售后节点同理（agent.py:494-500）：回复含"退款工单已生成"才复位 query_type。

## 八、review：必经收费站（agent.py:124-146, 334-347）

原则 3"审核是必选项"在图上的体现：**agent/order_agent/after_sales_agent 三条分支
最后都指向 review，只有 review 通向 END**（agent.py:552-554）——没有任何"说人话"的
路径能绕过。审核是**双层**：

```python
async def review_response(text: str) -> dict:
    """双层审核：规则快速拦截（0ms）→ LLM 深度审查。"""
    fast_check = ["成本价", "进货价", "拿货价", "底价", "利润多少", "加我微信"]   # 126
    for word in fast_check:
        if word in text:
            is_refusal = any(w in text for w in ["抱歉", "不能", "无法提供", "不方便"])  # 129
            has_price_number = bool(re.search(r"\d+\.?\d*元|\$\d+|¥\d+", text))
            if is_refusal and not has_price_number:
                return {"safe": True, ...}   # 特赦：AI 已在拒绝且没报价 → 放行
            return {"safe": False, ...}       # 拦截，换固定话术
    # LLM 深度审查：{verdict, reason, rewrite} JSON，见 135-146 与提示词 104-121
```

1. **规则层（0ms）**：词表命中即拦，带"特赦"分支（agent.py:129-132）——回复同时含
   "抱歉/不能/无法提供"（正在拒绝）且无价格数字 → 是**合规拒绝话术**，放行不冤枉好人；
   快词拦截省的是"大多数违规是关键词级，不必花 LLM 调用"；
2. **LLM 层**：回复截 1500 字丢便宜 LLM 按 `REVIEW_PROMPT`（agent.py:104-121）审查。
   提示词先列**放行清单**——正常报价、订单确认单里的价格/电话/地址（下单正常操作不是
   泄露）、客户主动给的联系方式回显——防止误杀业务；再列真该拦的：泄露成本/进货价、
   私自给员工私人手机号、不合理承诺、辱骂歧视。输出 JSON `{verdict, reason, rewrite}`，
   unsafe 用 `rewrite` 重写。

节点侧 review_node（agent.py:334-347）：最后一条不是"有内容的 AI 消息"就跳过
（工具中间轮不审）；判 unsafe 时 `state["messages"][:-1] + [safe]`（agent.py:345）——
**把最后那条 AI 回复整体换成安全版**，图的输出自然安全。两层都不中招才放行。

诚实取舍三连（面试直接背）：**① 审核失败 = 放行**——LLM 层整体包 `try/except pass`
（agent.py:135-146），解析失败默认 safe：审核是"提水位"不是业务关键路径，宁可偶漏
也别让审核故障打死对话（fail-open，要更严可改 fail-closed + 告警）；
**② 词表是中文业务黑话**（"底价""加我微信"），换个说法（"能不能走内部价"）就漏到
LLM 层——规则只当第一道网；**③ 审的是回复文本**，拦不住工具动作本身（真的下了一单）
——那部分靠 08 步 HITL 人工审批兜底。两道防线管不同的事，别混。

## 九、节点事件：前端怎么知道"跑到哪了"（src/node_events.py）

`agent.py` 只负责跑；"过程如何变前端步骤条"在 node_events.py：
`graph.astream(..., stream_mode="updates")`（stream_chat.py:95）每跑完一个节点吐一次
`{节点名: 该节点返回}`，`NODE_LABELS`（node_events.py:18-27）给 8 个节点配中文标签，
`describe_node(node, update)`（node_events.py:32-61）按该节点**这次的返回**生成一行
细节文案——UI 步骤条的映射如下（describe_node 是纯函数零依赖、可单测，node_events.py:5）：

| 节点 | describe_node 输出 | 读的字段 |
|---|---|---|
| supervisor | `→ 下单` / `→ 售后` / `→ 售前` | update 里的 `query_type`（37-40） |
| context_retriever | `命中 3 条知识` / `闲聊，跳过检索` | `knowledge_chunks` 长度（42-46） |
| agent | `调用 search_product、render_product` | 倒序找带 tool_calls 的消息（64-71） |
| tool_executor | `执行 search_product` | 按 tool_call_id 反查工具名（74-87） |
| query_reformulator | 改写短语（60 字内） | `rewrite_query`（56-60） |

顺带一提（agent.py:564-642）：`python src/agent.py` 用同一张图开终端对话——**业务层
不认识 HTTP**（01 步 Q2），Web/CLI/评测（14 步）共用一图；CLI 里 interrupt 是当场
y/n 审批（agent.py:616-632），Web 换成管理员面板（08 步），图机制完全一样。

---

## Q&A

**Q1：为什么用 LangGraph，而不是自己写状态机或普通 while 循环？**
自己写循环做三分支 = if/while 嵌套，改流程动控制流；更要命的是**做不了 interrupt
恢复与状态持久化**。LangGraph 的价值：(1) 图即文档——节点/边声明式，加分支只加一条边
（agent.py:542-546）；(2) checkpointer 自动存档——interrupt 挂起时状态已存好，审批
恢复只是 `Command(resume=...)` 再 ainvoke（08 步），手写"断点续跑"得自造重放机制；
(3) `stream_mode="updates"` 免费给节点事件流（§九）。一句话：
**"图编排 + 存档恢复"是选它的核心，普通循环给不了 HITL**。

**Q2：checkpointer 是什么？MemorySaver 和 thread_config 在 HITL 里的角色？**
checkpointer = 存档系统：每节点执行完把 state 按 `thread_id` 存一份；
`thread_config(user_id)`（agent.py:522-524）把 thread_id 设成 user_id——一人一条
存档线。下单走到 `interrupt()`（order_agent.py:159）图被"冻结"但存档在；审批端用同一
thread_id + `Command(resume={"approved":...})` 唤醒同一张图从断点重放（app.py:266-269）。
**没 checkpointer，interrupt 只是暂停、无法从外部恢复**。取舍：`MemorySaver`（agent.py:557）
放内存、重启即丢——生产应换 PostgresSaver（重启不丢、多实例共享），这是 README
"企业级演进"没走完的一步。

**Q3：意图路由已有 LLM 分类，为什么还要 query_type 延续 + 那么多规则层？**
多轮路由难点不在第一句，在**后面的句子**。真实事故（agent.py:400-402 注释）：上轮
下单 Agent 只查了价、没输出标准确认单，客户回"确认"，LLM 判成售前 → **下单挂半路**。
规则层让确定性延续不花 LLM 还更可靠（Layer 0.5：prev=place_order + 短确认词即延续，
agent.py:403-408；Layer 0.6 兜"补电话/地址"这类无关键词补全，414-437）；而 query_type
让规则有前提：上轮意图作为状态持久化再传回下轮（app.py:113/131）。一句话：
**LLM 管开放式分类，规则管确定性延续，query_type 是两者间的记忆接力棒**。

**Q4：审核为什么是"必选项"（原则 3）？双层（规则+LLM）各解决什么？**
Agent 会调工具、会写订单，输出直接面向客户与业务——一条泄露"进货价"或乱承诺的回复，
损失的是真实交易信任。所以"所有回复必须过"，图上只有 review 能到 END（agent.py:552-554）。
双层是成本/精度拆解：规则层 0ms 拦确定性违规（词表 126）带拒绝特赦防误杀（129-132）；
LLM 层拦开放式违规（135-146），放行清单防把正常报价/订单回显误判成泄露（104-121）。
加分点：审的是回复文本，拦不了工具动作本身——工具侧坏事靠 HITL 审批兜底，各管一段。

**Q5：为什么 import src.agent 不建 LLM？CI 那次事故教会了什么？**
早期 import 直接 `ChatOpenAI(...)`，构造要读 key——CI 没 .env，`import src.agent`
抛异常、测试全军覆没（`问题.md`，提交 b1b5283）。教训：**可测试代码的 import 必须
无副作用**（测试才敢直接 import 节点函数，test_supervisor.py 连 LLM 都不起）；
**资源构造延迟到首次使用**，错误在真用时才暴露。配套 `__getattr__` 兼容层
（agent.py:90-96）保老访问不炸，是渐变改造典型手法。半句实话：import 仍读 BM25
索引文件（agent.py:99），"无副作用"严格讲是"不建 LLM、不碰网络"级。

**Q6：工具循环会不会死循环？代码哪里兜底？**
图上 `agent ⇄ tool_executor` 是无条件环（agent.py:547-552），只要 Agent 一直要工具
就绕圈。代码**没显式配 recursion_limit / max_iterations**，兜底靠 LangGraph 默认
递归上限（25 步），超限抛 GraphRecursionError → stream_chat 转 error 事件
（stream_chat.py:97-98）。取舍：默认值能防卡死但体验是"突然报错"，更优雅是显式配
上限或让 agent 自断；且每轮带整份 messages（§四），循环越深拷贝越贵。

**Q7（追问）：头部注释写"图结构（4 个节点）"，实际 8 个，怎么解释？**
agent.py:9-11 是单 Agent 时代残留（入口→检索→Agent→审核→END），现在加了
supervisor / tool_executor / order_agent / after_sales_agent——演进留下的文档债。
主动指出它比背 8 个节点名更显真读代码，还能讲叙事：一条链走到黑 → 加 Supervisor
分流三业务 → 加工具循环支持 ReAct → **加 review 节点把"必过审核"从口头约定变成
图结构约束**——用架构约束代替口头约定，这一句很能打。

**Q8（追问）：`_is_order_completed` 为什么降级成兜底、不再参与路由？**
它按文本找"订单已生成"/`ORD-\d{8}-\d{13,}`（agent.py:467-481），但 LLM 回复会复述
历史订单号（"您之前的订单 ORD-... 已发货"）——文本判定会把还在下单流程的会话误判
完成、query_type 重置、用户被踢回售前。2026-09 修复（agent.py:487-491）：完成与否
只看 `create_order` 真实成功后挂的 `additional_kwargs["order_completed"]`——
**工具结果（机器可验证）优先于 LLM 文本（自由发挥）**：Agent 系统的信号分可信等级，
路由决策只信最高级——全项目最值得讲的一个 bug 修复。
