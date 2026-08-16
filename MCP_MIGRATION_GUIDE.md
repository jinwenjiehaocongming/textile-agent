# MCP 改造对比指南

> 本文档对比"当前做法"和"MCP 做法"，帮你理解 MCP 协议以及改造涉及哪些步骤。

---

## 一、MCP 是什么？

MCP（Model Context Protocol）是 Anthropic 2024 年底发布的**工具调用标准协议**。

### 核心思想：工具不再是"代码里的 JSON Schema"，而是"独立服务提供的 API"

```
当前做法:
  agent.py 里写死 SEARCH_PRODUCT_SCHEMA
  agent.py 里直接调 search_product()
  工具和 Agent 在同一个进程

MCP 做法:
  product_server.py  独立进程，暴露 search_product 工具
  agent.py           通过 MCP Client 连接，自动发现工具
  工具和 Agent 在不同进程，通过 stdio/HTTP 通信
```

### 类比：就像微服务取代单体

| 概念 | 当前做法 | MCP 做法 |
|------|---------|----------|
| 工具定义 | JSON Schema 写在代码里 | Server 启动时动态注册 |
| 工具调用 | 直接函数调用 | JSON-RPC 协议通信 |
| 工具发现 | 硬编码 `bind_tools([...])` | `client.list_tools()` 自动获取 |
| 跨项目复用 | 复制粘贴代码 | 启动同一个 Server 进程即可 |
| 调试 | print 大法 | MCP Inspector 可视化调试 |

---

## 二、当前架构 vs MCP 架构

### 当前：

```
┌──────────────────────────────────────────────┐
│                  agent.py                     │
│                                              │
│  SEARCH_PRODUCT_SCHEMA (写死在代码里)         │
│  QUERY_ORDER_SCHEMA    (写死在代码里)         │
│  search_product()      (写死在代码里)         │
│  query_order()         (写死在代码里)         │
│  tool_executor()       (写死在代码里)         │
│                                              │
│  ┌──────────────────────┐                    │
│  │ order_agent.py       │                    │
│  │ SEARCH_PRODUCT_SCHEMA│ ← 重复定义！        │
│  │ CREATE_ORDER_SCHEMA  │                    │
│  │ _search_product()    │ ← 重复实现！        │
│  │ _insert_order()      │                    │
│  └──────────────────────┘                    │
│                                              │
│  ┌──────────────────────┐                    │
│  │ after_sales_agent.py │                    │
│  │ QUERY_ORDER_SCHEMA   │ ← 又一次重复！      │
│  │ CREATE_REFUND_SCHEMA │                    │
│  │ _query_order()       │ ← 第三次实现！      │
│  └──────────────────────┘                    │
└──────────────────────────────────────────────┘
```

### MCP 改造后：

```
┌─────────────────────────────────────────────────────┐
│                   agent.py (变瘦)                    │
│                                                     │
│  mcp_client = MultiServerMCPClient({                │
│      "product":  connect_to("product_server.py"),   │
│      "order":    connect_to("order_server.py"),     │
│      "refund":   connect_to("refund_server.py"),    │
│  })                                                 │
│  tools = mcp_client.get_tools()  # 自动发现！        │
│  llm.bind_tools(tools)           # 不需要手写Schema  │
│                                                     │
└─────────────────────┬───────────────────────────────┘
                      │ stdio/HTTP (JSON-RPC)
          ┌───────────┼───────────┐
          ▼           ▼           ▼
┌──────────────┐ ┌──────────┐ ┌──────────────┐
│ product_     │ │ order_   │ │ refund_      │
│ server.py    │ │ server.py│ │ server.py    │
│              │ │          │ │              │
│ search_      │ │ search_  │ │ query_order  │
│ product      │ │ product  │ │ create_refund│
│              │ │ create_  │ │              │
│              │ │ order    │ │              │
└──────┬───────┘ └────┬─────┘ └──────┬───────┘
       │              │              │
       ▼              ▼              ▼
  products.db    orders.db       orders.db
```

**关键变化**：工具不再是 Agent 的一部分，而是 Agent 调用的外部服务。

---

## 三、完整工具调用流程对比（重点！）

> 以一个真实场景为例，追踪"LLM 决定调用 search_product → 执行 → 返回结果"的每一步。

### 场景：用户问 "T400 黑色多少钱"

---

### 3.1 当前做法：6 步，全部在 agent.py 一个文件里完成

```
步骤 ① 定义工具（写死在 agent.py 顶部）
┌─────────────────────────────────────────────────────────────┐
│ SEARCH_PRODUCT_SCHEMA = {                                   │
│     "type": "function",                                     │
│     "function": {                                           │
│         "name": "search_product",                           │
│         "description": "查询产品库存和报价。...",             │
│         "parameters": {                                     │
│             "type": "object",                               │
│             "properties": {                                 │
│                 "query": {"type": "string", ...}            │
│             },                                              │
│             "required": ["query"],                          │
│         },                                                  │
│     },                                                      │
│ }                                                           │
│                                                             │
│ def search_product(query: str) -> str:                      │
│     """实现也写在这里"""                                      │
│     conn = sqlite3.connect(str(DB_PATH))                    │
│     ...                                                     │
│     return "\n".join(lines)                                 │
└─────────────────────────────────────────────────────────────┘

步骤 ② Agent 节点绑定工具
┌─────────────────────────────────────────────────────────────┐
│ # agent_node() 里：                                         │
│ llm_with_tools = llm.bind_tools([                           │
│     SEARCH_PRODUCT_SCHEMA,   ← 硬编码绑定                    │
│     QUERY_ORDER_SCHEMA       ← 硬编码绑定                    │
│ ])                                                          │
│ response = llm_with_tools.invoke([system] + messages)       │
└─────────────────────────────────────────────────────────────┘

步骤 ③ LLM 返回 tool_calls
┌─────────────────────────────────────────────────────────────┐
│ response.tool_calls = [{                                    │
│     "name": "search_product",                               │
│     "args": {"query": "T400 黑色"},                         │
│     "id": "call_abc123"                                     │
│ }]                                                          │
│                                                             │
│ # 这个 AIMessage 被 append 到 state["messages"]             │
└─────────────────────────────────────────────────────────────┘

步骤 ④ 图路由：agent_router 判断有 tool_calls → 走 tool_executor
┌─────────────────────────────────────────────────────────────┐
│ def agent_router(state):                                    │
│     last_msg = state["messages"][-1]                        │
│     if hasattr(last_msg, "tool_calls") and \                │
│        last_msg.tool_calls:                                 │
│         return "tool_executor"  ← 有工具调用就执行工具        │
│     return "review"                                         │
└─────────────────────────────────────────────────────────────┘

步骤 ⑤ tool_executor 节点：手写 if/elif 路由
┌─────────────────────────────────────────────────────────────┐
│ def tool_executor(state):                                   │
│     last_msg = state["messages"][-1]                        │
│     results = []                                            │
│     for tc in last_msg.tool_calls:                          │
│         name, args = tc["name"], tc["args"]                 │
│                                                             │
│         if name == "search_product":      ← 手写路由        │
│             result = search_product(**args)  ← 直接函数调用  │
│         elif name == "query_order_status": ← 手写路由        │
│             result = query_order(**args)   ← 直接函数调用    │
│         else:                                               │
│             result = f"未知工具: {name}"                    │
│                                                             │
│         results.append(ToolMessage(                         │
│             content=str(result),                            │
│             tool_call_id=tc["id"]                           │
│         ))                                                  │
│     return {"messages": state["messages"] + results}        │
└─────────────────────────────────────────────────────────────┘

步骤 ⑥ 图路由：tool_executor → agent（循环，LLM 看到结果后生成回复）
┌─────────────────────────────────────────────────────────────┐
│ # 图定义：                                                  │
│ builder.add_edge("tool_executor", "agent")                  │
│                                                             │
│ # agent 节点再次执行：                                       │
│ # LLM 看到 ToolMessage(content="货号:P003 | T400 | 黑色...")│
│ # → 生成自然语言回复给用户                                   │
└─────────────────────────────────────────────────────────────┘
```

**当前做法的特点：**
- 定义和执行都在同一个文件
- 每加一个工具要改 3 处：① Schema 定义 ② 函数实现 ③ tool_executor 的 elif
- 直接函数调用，0 延迟
- 调试就是 print

---

### 3.2 MCP 做法：8 步，跨进程通信

```
步骤 ① MCP Server 启动并注册工具（独立进程，独立文件）
┌─────────────────────────────────────────────────────────────┐
│ # src/mcp_servers/product_server.py                         │
│                                                             │
│ server = Server("product-server")                           │
│                                                             │
│ @server.list_tools()                                        │
│ async def list_tools() -> list[Tool]:                       │
│     return [                                                │
│         Tool(                                               │
│             name="search_product",                          │
│             description="查询产品库存和报价。...",             │
│             inputSchema={                                   │
│                 "type": "object",                           │
│                 "properties": {                             │
│                     "query": {"type": "string", ...}        │
│                 },                                          │
│                 "required": ["query"],                      │
│             },                                              │
│         ),                                                  │
│     ]                                                       │
│                                                             │
│ @server.call_tool()                                         │
│ async def call_tool(name, arguments):                       │
│     if name == "search_product":                            │
│         return await _search_product(arguments["query"])    │
│                                                             │
│ # 通过 stdio 启动，等待 Client 连接                          │
│ async with stdio_server() as (read, write):                 │
│     await server.run(read, write, ...)                      │
└─────────────────────────────────────────────────────────────┘

步骤 ② Agent 启动时连接 MCP Server（agent.py 初始化）
┌─────────────────────────────────────────────────────────────┐
│ # agent.py 启动时：                                         │
│                                                             │
│ async def create_mcp_client():                              │
│     client = MultiServerMCPClient({                         │
│         "product": {                                        │
│             "command": "python",              ← 启动子进程   │
│             "args": ["src/mcp_servers/product_server.py"],  │
│             "transport": "stdio",             ← 通过标准IO   │
│         },                                                  │
│     })                                                      │
│     return client                                           │
│                                                             │
│ # 后台发生了什么：                                           │
│ # 1. Python 子进程启动 product_server.py                    │
│ # 2. MCP Client 发送 initialize 请求                        │
│ # 3. MCP Server 返回能力声明（支持 tools）                   │
│ # 4. MCP Client 发送 tools/list 请求                        │
│ # 5. MCP Server 返回工具列表（search_product 的 Schema）     │
└─────────────────────────────────────────────────────────────┘

步骤 ③ Agent 节点获取工具并绑定
┌─────────────────────────────────────────────────────────────┐
│ # agent_node() 里：                                         │
│                                                             │
│ client = state["mcp_client"]                                │
│ tools = await client.get_tools()  ← 从 Server 自动获取       │
│ # tools = [                                                 │
│ #   {"name": "search_product",                              │
│ #    "description": "查询产品库存...",                       │
│ #    "input_schema": {"type": "object", ...}}               │
│ # ]                                                         │
│                                                             │
│ llm_with_tools = llm.bind_tools(tools)  ← 不需要手写Schema  │
│ response = llm_with_tools.invoke([system] + messages)       │
└─────────────────────────────────────────────────────────────┘

步骤 ④ LLM 返回 tool_calls（和当前一样）
┌─────────────────────────────────────────────────────────────┐
│ response.tool_calls = [{                                    │
│     "name": "search_product",                               │
│     "args": {"query": "T400 黑色"},                         │
│     "id": "call_abc123"                                     │
│ }]                                                          │
└─────────────────────────────────────────────────────────────┘

步骤 ⑤ 不再需要 agent_router 判断是否走 tool_executor！
┌─────────────────────────────────────────────────────────────┐
│ # MCP 做法下，这步直接在 agent_node 的 ReAct 循环里处理：    │
│                                                             │
│ if hasattr(response, "tool_calls") and response.tool_calls: │
│     for tc in response.tool_calls:                          │
│         # 不需要 if/elif 判断是哪个工具！                   │
│         result = await mcp_client.call_tool(               │
│             tc["name"],       ← MCP Client 自动路由到正确的  │
│             tc["args"]        ← Server，不需要手写 switch    │
│         )                                                   │
│         conversation.append(ToolMessage(                    │
│             content=str(result),                            │
│             tool_call_id=tc["id"]                           │
│         ))                                                  │
│     continue  # 回到 ReAct 循环，LLM 看到结果后生成回复       │
└─────────────────────────────────────────────────────────────┘

步骤 ⑥ MCP Client 发送 JSON-RPC 请求到 Server（底层通信）
┌─────────────────────────────────────────────────────────────┐
│ # MCP Client 通过 stdio 发送 JSON：                          │
│                                                             │
│ → {"jsonrpc":"2.0",                                        │
│    "method":"tools/call",                                   │
│    "params":{                                               │
│      "name":"search_product",                               │
│      "arguments":{"query":"T400 黑色"}                      │
│    },                                                       │
│    "id":1}                                                  │
│                                                             │
│ # product_server.py 收到后，call_tool() 执行，返回：         │
│                                                             │
│ ← {"jsonrpc":"2.0",                                        │
│    "result":{                                               │
│      "content":[{"type":"text",                             │
│        "text":"货号:P003 | T400 | 黑色 | ..."}]},           │
│    "id":1}                                                  │
└─────────────────────────────────────────────────────────────┘

步骤 ⑦ MCP Client 把结果返回给 Agent（进程间数据回来）
┌─────────────────────────────────────────────────────────────┐
│ # client.call_tool() 返回的是 Python 对象：                  │
│ # [TextContent(type="text", text="货号:P003 | T400 | ...")] │
│                                                             │
│ # Agent 代码拿到后转成 ToolMessage：                         │
│ tool_msgs.append(ToolMessage(                               │
│     content="货号:P003 | T400 | 黑色 | ...",                │
│     tool_call_id=tc["id"]                                   │
│ ))                                                          │
└─────────────────────────────────────────────────────────────┘

步骤 ⑧ Agent 带着 ToolMessage 再调 LLM，生成最终回复
┌─────────────────────────────────────────────────────────────┐
│ # 和当前一样：LLM 看到工具结果，生成自然语言回复              │
│ # "T400 黑色目前有现货，单价 ¥12.5/米，库存 2000 米..."     │
└─────────────────────────────────────────────────────────────┘
```

---

### 3.3 逐步骤对比表

| 步骤 | 当前做法 | MCP 做法 | 核心区别 |
|------|---------|----------|---------|
| **定义工具** | 手写 JSON Schema 字典 | `@server.list_tools()` 装饰器返回 Tool 对象 | MCP 是结构化 API，不是裸字典 |
| **注册工具** | `llm.bind_tools([SCHEMA1, SCHEMA2])` | `await client.get_tools()` → `llm.bind_tools(tools)` | 前者硬编码，后者自动发现 |
| **LLM 返回 tool_calls** | 相同 | 相同 | 无区别，都是 OpenAI 格式 |
| **路由到执行器** | LangGraph 图路由：`agent_router → tool_executor` 节点 | 直接在 `agent_node` 的 ReAct 循环里处理 | MCP 不需要单独的 tool_executor 节点和图路由 |
| **执行工具** | 直接函数调用 `search_product(**args)` | 跨进程 JSON-RPC `await client.call_tool(name, args)` | 函数调用 vs 网络协议 |
| **结果返回** | 函数返回值（字符串） | JSON-RPC 响应 → `TextContent` 对象 → 转字符串 | 多一层序列化/反序列化 |
| **加新工具** | 改 3 处：Schema + 函数 + tool_executor 的 elif | 改 1 处：Server 的 list_tools + call_tool | MCP 扩展成本低 |
| **调试** | `print(f"⚙️ [工具] {name}({args})")` | MCP Inspector 可视化 + JSON-RPC 日志 | MCP 更专业但链路更长 |

---

### 3.4 下单 Agent 的工具调用对比（ReAct 循环）

当前 `order_agent.py` 的 ReAct 循环有两个工具：`search_product` + `create_order`。

**当前做法**：
```python
# order_agent.py ReAct 循环
for _ in range(5):
    response = llm_with_tools.invoke(conversation)
    conversation.append(response)

    if hasattr(response, "tool_calls") and response.tool_calls:
        tool_msgs = []
        for tc in response.tool_calls:
            name, args = tc["name"], tc["args"]
            # ↓↓↓ 手写 if/elif，每加一个工具多加一个分支
            if name == "search_product":
                result = _search_product(**args)      # 直接调函数
            elif name == "create_order":
                result = _insert_order(**args)        # 直接调函数
            else:
                result = f"未知工具: {name}"
            tool_msgs.append(ToolMessage(
                content=str(result),
                tool_call_id=tc["id"]
            ))
        conversation.extend(tool_msgs)

        # ↓↓↓ 特殊逻辑：create_order 被调了就返回
        if any(tc["name"] == "create_order" for tc in response.tool_calls):
            return tool_msgs[-1]
    else:
        # ↓↓↓ 防编造检查
        if any(kw in content for kw in ["订单已生成"]):
            conversation.append(HumanMessage("必须调用 create_order！"))
            continue
        return response
```

**MCP 做法**：
```python
# order_agent.py ReAct 循环 — MCP 版
for _ in range(5):
    response = llm_with_tools.invoke(conversation)
    conversation.append(response)

    if hasattr(response, "tool_calls") and response.tool_calls:
        tool_msgs = []
        for tc in response.tool_calls:
            # ↓↓↓ 不需要 if/elif！MCP Client 自动路由到正确的 Server
            result = await mcp_client.call_tool(tc["name"], tc["args"])
            tool_msgs.append(ToolMessage(
                content=str(result),        # result 是 TextContent 列表
                tool_call_id=tc["id"]
            ))
        conversation.extend(tool_msgs)

        if any(tc["name"] == "create_order" for tc in response.tool_calls):
            return tool_msgs[-1]
    else:
        if any(kw in content for kw in ["订单已生成"]):
            conversation.append(HumanMessage("必须调用 create_order！"))
            continue
        return response
```

**关键变化**：
- 删掉了 `if name == "search_product": ... elif name == "create_order": ...`
- 变成 `await mcp_client.call_tool(name, args)` — 一个统一入口处理所有工具
- `search_product` 在 `product_server.py` 里，`create_order` 在 `order_server.py` 里，但调用方完全不需要知道

---

### 3.5 MCP 通信底层：JSON-RPC 协议

MCP 底层用的是 **JSON-RPC 2.0**，一个轻量级的远程调用协议。下面是 Agent 和 Server 之间的完整对话：

```
===== 初始化阶段（Agent 启动时自动完成） =====

Client → Server (stdio):
{"jsonrpc":"2.0","method":"initialize","params":{
  "protocolVersion":"2024-11-05",
  "capabilities":{},
  "clientInfo":{"name":"langgraph-agent","version":"1.0.0"}
},"id":1}

Server → Client (stdio):
{"jsonrpc":"2.0","result":{
  "protocolVersion":"2024-11-05",
  "capabilities":{"tools":{}},          ← Server 说我支持 tools
  "serverInfo":{"name":"product-server","version":"1.0.0"}
},"id":1}

Client → Server:
{"jsonrpc":"2.0","method":"notifications/initialized"}  ← 握手完成

===== 工具发现阶段 =====

Client → Server:
{"jsonrpc":"2.0","method":"tools/list","id":2}
                            ↑ 问 Server "你有什么工具？"

Server → Client:
{"jsonrpc":"2.0","result":{"tools":[
  {"name":"search_product",
   "description":"查询产品库存和报价...",
   "inputSchema":{"type":"object","properties":{"query":{"type":"string"}}}}
]},"id":2}
  ↑ 返回工具列表（等效于当前的 SEARCH_PRODUCT_SCHEMA）

===== 工具调用阶段（用户问 "T400 黑色多少钱"） =====

# LLM 决定调 search_product → Agent 代码执行：

Client → Server:
{"jsonrpc":"2.0","method":"tools/call","params":{
  "name":"search_product",
  "arguments":{"query":"T400 黑色"}
},"id":3}
  ↑ 等效于当前的 search_product(query="T400 黑色")

Server → Client:
{"jsonrpc":"2.0","result":{
  "content":[{"type":"text","text":"货号:P003 | T400 | 黑色 | 门幅:150cm | 库存:2000米 | ¥12.5/米"}]
},"id":3}
  ↑ 等效于当前函数的 return 值
```

**对比**：
- 当前：`search_product(query="T400 黑色")` → 函数调用，返回字符串
- MCP：`{"method":"tools/call", "params":{...}}` → JSON-RPC 请求，返回 JSON 响应
- 核心逻辑不变（SQL 查询 → 计分排序 → 格式化），只是"输入方式"和"输出方式"变了

---

## 五、逐文件代码对比

### 文件 1: `src/agent.py` — 当前做法

```python
# ===== 当前：工具 Schema 写在 agent.py 里 =====

SEARCH_PRODUCT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_product",
        "description": "查询产品库存和报价...",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "产品名、颜色或品类关键词"},
            },
            "required": ["query"],
        },
    },
}

def search_product(query: str) -> str:
    """直接操作 SQLite"""
    conn = sqlite3.connect(str(DB_PATH))
    # ... 查询逻辑 ...
    return result

# 工具执行也是手写的 if/elif 路由
def tool_executor(state: AgentState) -> dict:
    for tc in last_msg.tool_calls:
        if name == "search_product":
            result = search_product(**args)
        elif name == "query_order_status":
            result = query_order(**args)
        # 每加一个工具就要加一个 elif
```

### 文件 1: `src/agent.py` — MCP 改造后

```python
# ===== MCP：不再需要 Schema 和函数实现 =====
# 原来的 SEARCH_PRODUCT_SCHEMA、search_product()、tool_executor() 全部删除

from langchain_mcp_adapters.client import MultiServerMCPClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# 1. 连接 MCP Server（启动子进程，通过 stdio 通信）
async def create_mcp_client():
    """创建 MCP 客户端，连接多个工具服务器"""
    client = MultiServerMCPClient({
        "product": {
            "command": "python",
            "args": ["src/mcp_servers/product_server.py"],
            "transport": "stdio",  # 通过标准输入输出通信
        },
        "order": {
            "command": "python",
            "args": ["src/mcp_servers/order_server.py"],
            "transport": "stdio",
        },
        "refund": {
            "command": "python",
            "args": ["src/mcp_servers/refund_server.py"],
            "transport": "stdio",
        },
    })
    return client

# 2. 获取工具：不再手写 Schema，从 Server 自动发现
async def get_tools(client):
    tools = await client.get_tools()  # ← 自动发现所有 Server 的工具
    # tools 现在是 [{"name": "search_product", ...}, {"name": "create_order", ...}, ...]
    return tools

# 3. Agent 节点：工具从 MCP 来，不是硬编码
async def agent_node(state: AgentState) -> dict:
    client = state.get("mcp_client")
    tools = await client.get_tools()  # 自动发现，不用 bind_tools([SEARCH_PRODUCT_SCHEMA, ...])
    llm_with_tools = llm.bind_tools(tools)
    # ... 其余逻辑不变 ...

# 4. 工具执行：不再需要 tool_executor 节点！
#    MCP Client 自动处理工具调用，不需要 if/elif 路由
```

**对比总结**：`agent.py` 删掉了 ~80 行（Schema 定义 + 函数实现 + tool_executor），新增 ~30 行（MCP Client 初始化）。

---

### 文件 2: `src/order_agent.py` — 当前做法

```python
# ===== 当前：工具 Schema 和实现都写在这里 =====

SEARCH_PRODUCT_SCHEMA = { ... }  # 和 agent.py 重复！
CREATE_ORDER_SCHEMA = { ... }

def _search_product(query):       # 和 agent.py 重复！
    conn = sqlite3.connect(...)
    ...

def _insert_order(...):           # 只有这里有
    conn = sqlite3.connect(...)
    ...

# ReAct 循环里硬编码工具路由
for _ in range(5):
    response = llm_with_tools.invoke(conversation)
    for tc in response.tool_calls:
        if name == "search_product":    # 手写 if/elif
            result = _search_product(**args)
        elif name == "create_order":
            result = _insert_order(**args)
```

### 文件 2: `src/order_agent.py` — MCP 改造后

```python
# ===== MCP：工具定义和实现都不在这里了 =====
# 只保留 Agent 逻辑（提示词 + ReAct 循环）

async def order_agent_node(state: OrderAgentState) -> AIMessage:
    mcp_client = state["mcp_client"]
    tools = await mcp_client.get_tools()  # 自动发现 product_server 和 order_server 的工具
    llm_with_tools = order_llm.bind_tools(tools)

    conversation = [SystemMessage(content=ORDER_AGENT_PROMPT.format(...))]

    for _ in range(5):
        response = llm_with_tools.invoke(conversation)
        conversation.append(response)

        if hasattr(response, "tool_calls") and response.tool_calls:
            # MCP 自动路由到正确的 Server，不需要 if/elif！
            tool_msgs = []
            for tc in response.tool_calls:
                tool_result = await mcp_client.call_tool(tc["name"], tc["args"])
                tool_msgs.append(ToolMessage(
                    content=str(tool_result),
                    tool_call_id=tc["id"]
                ))
            conversation.extend(tool_msgs)

            # create_order 被调了 → 返回结果
            if any(tc["name"] == "create_order" for tc in response.tool_calls):
                return tool_msgs[-1]
        else:
            content = response.content or ""
            if any(kw in content for kw in ["订单已生成", "订单号：ORD"]):
                conversation.append(HumanMessage(content="必须调用 create_order 工具！"))
                continue
            return response

    return AIMessage(content="下单流程超时，请稍后重试。")
```

**对比总结**：`order_agent.py` 删掉了 `SEARCH_PRODUCT_SCHEMA`、`CREATE_ORDER_SCHEMA`、`_search_product()`、`_insert_order()`（~80 行），只保留 Agent 逻辑（~60 行）。

---

### 新增文件 3: `src/mcp_servers/product_server.py`

```python
"""
产品查询 MCP Server
===================
独立进程，通过 stdio 与 Agent 通信。
可以被任何 MCP Client 连接——agent.py、order_agent.py、甚至外部系统。

运行方式（MCP Client 自动启动，不需要手动运行）：
  python src/mcp_servers/product_server.py
"""
import json, sqlite3, sys
from pathlib import Path
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

DB_PATH = Path(__file__).parent.parent.parent / "data" / "products.db"

# 1. 创建 MCP Server 实例
server = Server("product-server")

# 2. 注册工具：用装饰器，不需要手写 JSON Schema！
@server.list_tools()
async def list_tools() -> list[Tool]:
    """告诉 MCP Client 这个 Server 有哪些工具可用"""
    return [
        Tool(
            name="search_product",
            description="查询产品库存和报价。按名称/颜色/品类搜索面料。",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "产品名、颜色或品类关键词",
                    }
                },
                "required": ["query"],
            },
        ),
        # 将来加新工具只需在这里加一行，Client 自动发现，不需要改 agent.py！
    ]

# 3. 实现工具调用
@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """处理来自 MCP Client 的工具调用请求"""
    if name == "search_product":
        return await _search_product(arguments["query"])
    raise ValueError(f"Unknown tool: {name}")

async def _search_product(query: str) -> list[TextContent]:
    """实际的查询逻辑（和之前完全一样）"""
    keywords = [kw.strip().lower() for kw in
                query.replace("，", " ").replace(",", " ").split() if kw.strip()]
    if not keywords:
        return [TextContent(type="text", text="请输入产品名、颜色或品类关键词。")]

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    where_parts = []
    params = []
    for kw in keywords:
        p = f"%{kw}%"
        where_parts.append("(name LIKE ? OR color LIKE ? OR category LIKE ?)")
        params.extend([p, p, p])

    rows = conn.execute(
        f"SELECT * FROM products WHERE {' OR '.join(where_parts)} LIMIT 50", params
    ).fetchall()

    # ... 计分排序逻辑（和之前一模一样）...
    conn.close()

    if not rows:
        return [TextContent(type="text", text="未找到匹配产品。")]

    lines = []
    for _, r in scored[:10]:
        lines.append(f"货号:{r['id']} | {r['name']} | ...")
    return [TextContent(type="text", text="\n".join(lines))]

# 4. 启动 Server（stdio 模式）
async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

**要点**：
- MCP Server 是一个独立的 asyncio 程序
- `list_tools()` 告诉 Client 有什么工具可用（替代手写 Schema）
- `call_tool()` 处理实际的工具调用（替代手写 if/elif/else）
- 通过 stdio（标准输入输出）通信，Client 启动时自动拉起进程

---

### 新增文件 4: `src/mcp_servers/order_server.py`

```python
"""
订单管理 MCP Server
===================
提供 create_order 工具。下单 Agent 通过 MCP 协议调用。
"""
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

server = Server("order-server")

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="create_order",
            description="为客户创建面料采购订单。仅在客户明确确认下单后调用。",
            inputSchema={
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string", "description": "客户标识"},
                    "product_id": {"type": "string", "description": "产品货号"},
                    "product_name": {"type": "string", "description": "面料名称"},
                    "color": {"type": "string", "description": "颜色"},
                    "quantity": {"type": "integer", "description": "订购数量（米）"},
                    "unit_price": {"type": "number", "description": "单价（元/米）"},
                    "phone": {"type": "string", "description": "联系电话"},
                    "address": {"type": "string", "description": "收货地址"},
                    "delivery_date": {"type": "string", "description": "期望交期"},
                },
                "required": ["customer_id", "product_id", "product_name",
                            "quantity", "unit_price", "phone", "address", "delivery_date"],
            },
        ),
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "create_order":
        return await _create_order(**arguments)
    raise ValueError(f"Unknown tool: {name}")

async def _create_order(customer_id, product_id, product_name, color,
                        quantity, unit_price, phone, address, delivery_date):
    # 和原来 _insert_order 一模一样的逻辑
    ...

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

---

### 新增文件 5: `src/mcp_servers/refund_server.py`

```python
"""
售后 MCP Server
===============
提供 query_order + create_refund 工具。
"""
# 结构同上，注册两个工具：query_order, create_refund
```

---

## 六、改造前后对比总表

| 维度 | 当前做法 | MCP 做法 | 变化 |
|------|---------|----------|------|
| **工具 Schema 在哪** | 分散在 agent.py、order_agent.py、after_sales_agent.py | 集中在各 MCP Server | 消除重复 |
| **工具实现在哪** | 和 Agent 逻辑混在同一个文件 | 独立 Server 文件 | 关注点分离 |
| **工具发现方式** | 手写 `bind_tools([SCHEMA1, SCHEMA2])` | `client.get_tools()` 自动发现 | 动态发现 |
| **工具执行路由** | 手写 `if/elif/else` | `client.call_tool(name, args)` | 统一接口 |
| **新增工具** | 改 3 个文件（加 Schema + 实现 + elif） | 改 1 个 Server 文件，加一个 Tool | 扩展性好 |
| **跨项目复用** | 复制粘贴函数代码 | 启动同一个 Server 进程 | 真正复用 |
| **调试方式** | print 大法 | MCP Inspector 可视化 | 专业调试 |
| **进程数** | 1 个 | 4 个（主进程 + 3 个 Server） | 稍多 |
| **延迟** | 函数调用，0ms | 进程间 JSON-RPC，~2ms | 可忽略 |
| **代码量变化** | agent.py 650行 | agent.py ~550行 + 3个Server ~350行 | 总量稍增，但结构更清 |

---

## 七、需要安装的新依赖

```bash
# requirements.txt 新增：
pip install mcp                    # MCP 协议核心库（Server 端）
pip install langchain-mcp-adapters # LangChain ↔ MCP 桥接（Client 端）
```

当前项目已经有的 `langchain`、`langgraph`、`openai`、`chromadb` 等全部不变。

---

## 八、不需要改的部分（放心）

以下模块**完全不动**：

| 模块 | 原因 |
|------|------|
| `retrieval.py` | 知识检索，不涉及工具调用 |
| `memory.py` | 用户记忆，不涉及工具调用 |
| `app.py` | Web 界面，只调 graph |
| `review_node` | 审核逻辑，不涉及工具 |
| `supervisor_node` | 路由逻辑，不涉及工具 |
| `query_reformulator` | 查询改写，不涉及工具 |
| LangGraph 图结构 | 节点和边不变，只是节点里的工具调用方式变了 |
| `data/*.db` | 数据库完全不变 |
| 评估脚本 | `eval_retrieval.py`、`eval_v2.py` 不变 |

---

## 九、改造步骤（分 3 步，逐步推进）

### Step 1: 抽 `search_product` 到 MCP Server（最安全的第一步）

```
目标：把售前 Agent 的 search_product 工具抽出来
验证：agent.py 能正常查产品 → 对比前后结果一致

新增：src/mcp_servers/product_server.py
修改：agent.py（工具获取方式改为 MCP Client）
删除：agent.py 中的 SEARCH_PRODUCT_SCHEMA + search_product() + tool_executor 的 if 分支
```

### Step 2: 抽 order 和 after-sales 工具

```
目标：把 create_order、query_order、create_refund 都抽出来
验证：下单流程和售后流程正常运行

新增：src/mcp_servers/order_server.py、refund_server.py
修改：order_agent.py、after_sales_agent.py（用 MCP Client 替代硬编码）
```

### Step 3: 用 MCP Inspector 调试

```bash
# MCP 官方调试工具，可以可视化测试每个工具
npx @modelcontextprotocol/inspector python src/mcp_servers/product_server.py
```

---

## 十、面试怎么说

改造完后，你可以在面试中这样描述：

> "我把工具层从 Agent 代码里解耦出来，做成了 MCP Server。
> 三个 Server 分别管产品查询、订单管理、售后处理，通过
> JSON-RPC over stdio 和 Agent 通信。
>
> 这样做的好处是：
> 1. 工具可以被多个 Agent 复用，不用重复定义
> 2. 新增工具只需要改 Server，Agent 自动发现
> 3. 可以用 MCP Inspector 可视化调试每个工具
> 4. 符合 Anthropic 的 MCP 标准协议"

---

## 十一、一步到位 vs 渐进式？

### 一步到位：
- 一次改完 3 个 Server，一次性切过去
- 风险：如果 MCP 通信有问题，整个系统挂掉
- 好处：干净利落

### 渐进式（推荐）：
- 先只抽 `search_product`，验证稳定后逐个抽
- 风险：中间状态可以用一个开关（`USE_MCP = True/False`）回退
- 好处：每一步都能测试，出问题容易定位

**建议渐进式。** 你是为了学习和面试，把每一步都搞懂比速度重要。

---

## 十二、MCP 的局限性（面试中提这个加分）

| 问题 | 说明 |
|------|------|
| **协议尚在演进** | MCP 2024.11 才发布，API 可能变动 |
| **进程管理复杂** | 多 3 个子进程，需要处理启动/崩溃/重启 |
| **调试链路变长** | 函数调用变进程间通信，排查问题多一层 |
| **对简单项目过度** | 如果只有 2 个工具、1 个 Agent，MCP 显得杀鸡用牛刀 |
| **langchain-mcp-adapters** | 桥接库还不够成熟，可能遇到兼容问题 |

**但对于你这个项目（3 个 Agent + 5 个工具 + 面试项目），MCP 改造是合理的。**

---

## 十三、总结

```
改造核心就一句话：
把"工具定义+实现+路由"从 Agent 代码里删掉，
换成"通过 MCP Client 去问 MCP Server"。
```

**删除的**：`SEARCH_PRODUCT_SCHEMA`、`CREATE_ORDER_SCHEMA`、`QUERY_ORDER_SCHEMA`、
`CREATE_REFUND_SCHEMA`、`search_product()`、`_insert_order()`、`_query_order()`、
`_create_refund()`、`tool_executor` 节点的 if/elif 路由

**新增的**：3 个 MCP Server 文件 + agent.py 里的 MCP Client 初始化

**不变的**：检索、审核、路由、记忆、评估、数据库
