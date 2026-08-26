# 07 官方 MCP 详解与面试题（你项目的自研实现 vs 官方 SDK）

> 背景：你项目 `src/mcp_client.py` 是**自研的协议兼容 MCP Client**（纯标准库，JSON-RPC 2.0 over stdio）。
> 项目根目录还有一份 `MCP_MIGRATION_GUIDE.md`（982 行）专门讲"自研 → 官方 MCP"的改造路线。
> 面试官很可能顺着你的自研实现追问："**官方 mcp 包你用过吗？具体怎么用？**""**为什么不用官方的？**"
> 本文把官方 MCP 的用法讲透 + 给你一套不管怎么问都能接住的话术。

---

## 一、先分清三个概念（别混）

| 概念 | 是什么 | 你项目里的对应物 |
|------|--------|----------------|
| **MCP 协议** | Anthropic 2024 发布的工具调用**标准协议**：工具怎么被描述(description/schema)、发现(tools/list)、调用(tools/call)、传输(stdio/HTTP) | 你的 Server/Client 通信的正是这套协议（方法名、JSON-RPC 格式完全兼容） |
| **官方 mcp SDK** | `pip install mcp`：帮你封装协议的服务端（`FastMCP`/`Server`）和客户端（`ClientSession`/`stdio_client`） | 你没有用，`mcp_client.py` 是手写的协议实现 |
| **langchain-mcp-adapters** | `pip install langchain-mcp-adapters`：MCP ↔ LangChain/LangGraph 的**桥接库**，把 MCP 工具转成 `bind_tools` 格式 | 你手写了 `get_tools_for_langchain`（mcp_client.py:216-234）做了同样的事 |

> 面试结论句：**"我的项目实现的是协议层兼容（自研 client），不是用的官方 SDK；SDK 我也熟，两套我都能讲。"**

---

## 二、官方 MCP：Server 端怎么写（两种写法都要会）

### 写法 A：FastMCP（现代推荐写法，几行搞定）

```python
# product_server.py — 官方 FastMCP 版
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("product-server")

@mcp.tool()
def search_product(query: str) -> str:
    """查询产品库存和报价。按名称/颜色/品类搜索面料。"""
    # 业务逻辑与现在 product_server.py:25-87 完全相同
    return "\n".join(lines)

if __name__ == "__main__":
    mcp.run()   # 默认 stdio 传输，自动处理 initialize/notifications/tools/list/tools/call
```

- `@mcp.tool()` 从**函数签名**自动生成 inputSchema（docstring 作 description）——比手写 Schema 更省。
- 这就是现在自研 Server 主循环（product_server.py:147-215 的 `for line in sys.stdin`）被官方封装后的样子。

### 写法 B：经典 Server API（`MCP_MIGRATION_GUIDE.md` 里的写法）

```python
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

server = Server("product-server")

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(name="search_product",
                 description="查询产品库存和报价，按名称/颜色/品类搜面料",
                 inputSchema={"type": "object",
                              "properties": {"query": {"type": "string"}},
                              "required": ["query"]})]

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "search_product":
        return [TextContent(type="text", text=search_product(arguments["query"]))]
    raise ValueError(f"Unknown tool: {name}")

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

### 两种写法选哪个（面试可主动讲）
- **FastMCP**：写新工具最快，函数即工具；适合大多数业务 Server。
- **Server API**：精细控制（自定义 tool 对象、资源、提示词、握手选项）；适合要完全掌控协议细节的场景。
- 你项目的自研版 ≈ Server API 手写版（连 `list_tools`/`call_tool` 的 JSON 收发都自己写）。

---

## 三、官方 MCP：Client 端怎么写（两种层次）

### 层次 A：底层 `ClientSession`（对应你自研 client 的每一步）

```python
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(
        command="python",
        args=["src/mcp_servers/product_server.py"],
        env=None,              # 默认继承环境变量
        cwd=None,
    )
    async with stdio_client(params) as (read, write):   # 启动子进程 ≈ 你的 _spawn
        async with ClientSession(read, write) as session:
            await session.initialize()                   # 握手 ≈ 你的 _handshake
            tools = await session.list_tools()           # 工具发现 ≈ tools/list
            print([t.name for t in tools.tools])
            result = await session.call_tool("search_product", {"query": "T400 黑色"})
            for c in result.content:                     # ≈ 你的 _extract_text
                if c.type == "text":
                    print(c.text)

asyncio.run(main())
```

对照你自研的 `MCPSyncClient`（mcp_client.py）：

| 自研实现 | 官方 SDK |
|---------|---------|
| `_spawn`（:51-59） | `stdio_client(params)` |
| `_handshake` initialize+tools/list（:61-80） | `session.initialize()` + `session.list_tools()` |
| `_request` tools/call JSON-RPC（:104-115） | `session.call_tool(...)` |
| `_extract_text` 提 text（:204-212） | `result.content` 的 TextContent 对象 |
| RLock 串行（:39,184） | session 本身设计为一次一个 await 调用 |

### 层次 B：`langchain-mcp-adapters` 桥接（配你项目的 LangGraph 用）

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

client = MultiServerMCPClient({
    "product": {"command": "python", "args": ["src/mcp_servers/product_server.py"], "transport": "stdio"},
    "order":   {"command": "python", "args": ["src/mcp_servers/order_server.py"],   "transport": "stdio"},
    "refund":  {"command": "python", "args": ["src/mcp_servers/refund_server.py"],  "transport": "stdio"},
})

# 进入上下文后：
tools = await client.get_tools()            # → [{"name", "description", "input_schema"}, ...]
llm_with_tools = llm.bind_tools(tools)      # 等效你的 agent.py:284-287

# LLM 返回 tool_calls 后执行：
result = await client.call_tool(tc["name"], tc["args"])   # 等效你的 tool_executor / ReAct 循环
```

`get_tools()` 内部就是 `session.list_tools()` × N 个 server；`call_tool()` 内部就是 `session.call_tool()` + 文本提取——**这两件事你的 `get_tools_for_langchain`（mcp_client.py:216-234）和 `call_tool`（mcp_client.py:161-196）也做了，只是自己写的。**

---

## 四、官方 MCP 和你自研版的深度对比（面试考点）

| 维度 | 你项目自研（mcp_client.py + 三个 server） | 官方 mcp + langchain-mcp-adapters |
|------|------------------------------------------|----------------------------------|
| 协议 | JSON-RPC 2.0 over stdio，**协议兼容**（方法名/格式一致） | 协议本来就是你实现的这层，官方顺手封装 |
| Server 写法 | 手写 `for line in sys.stdin` 逐行解析 + `_respond`（product_server.py:147-215） | `FastMCP`/`@server.list_tools()`+`@server.call_tool()` |
| Client 写法 | 手写 `_request`/`_recv`/`_send`（mcp_client.py:104-157） | `ClientSession` 或 `MultiServerMCPClient` |
| 并发安全 | **自研 RLock 串行**（:39,184）——防止 JSON-RPC 响应串线 | 官方 ClientSession 设计为串行 await；高并发要自己挂连接池 |
| 超时保护 | **自研读线程+join(30s)**（:137-157）——防子进程僵死 | 传输层可配超时，但"进程僵死"要自己处理 |
| 崩溃自愈 | **自研重启+重放握手+重试一次**（:82-100, 174-194） | **官方默认没有**，要自己包一层（这就是你自研版的价值点） |
| 工具格式转换 | `get_tools_for_langchain`（:216-234） | `langchain-mcp-adapters` 做 |
| 调试 | print 日志 | **MCP Inspector**（`npx @modelcontextprotocol/inspector python src/mcp_servers/product_server.py`） |
| 依赖 | 零外部依赖 | `mcp` + `langchain-mcp-adapters` 两个新依赖 |
| 现代传输 | 只有 stdio | stdio + Streamable HTTP（远程部署用） |

---

## 五、如果你要把项目换成官方 MCP（照着 `MCP_MIGRATION_GUIDE.md` 的路线）

**核心就一句话**：把"工具定义+实现+路由"从 Agent 代码里删掉，换成"通过 MCP Client 去问 MCP Server"。

1. **三个 Server 改用 `mcp` 包**：`Server("product-server")` + `@server.list_tools()` + `@server.call_tool()`（业务逻辑 `search_product`/`create_order`/`create_refund` 原样搬，返回值包 `TextContent`）。
2. **agent.py 删 ~80 行**：`SEARCH_PRODUCT_SCHEMA`、`search_product()`、`tool_executor` 节点 —— 因为工具发现改 `client.get_tools()`，执行改 `client.call_tool(name, args)` 统一入口（不再 if/elif 路由）。
3. **order_agent.py / after_sales_agent.py 同理瘦身**：删 Schema 与实现，ReAct 循环里的 `mcp.call_tool` 已是统一入口（现在就是），只换实现来源。
4. **新增依赖**：`pip install mcp langchain-mcp-adapters`。
5. **不动的**：retrieval/memory/app/review/supervisor/图结构/数据库/评测脚本。

**渐进式建议**（指南原文）：先只抽 `search_product` 一个工具验证稳定，再用 `USE_MCP=True/False` 开关回退，最后逐个抽完。面试可以讲这个"渐进式迁移"思路 = 工程意识。

---

## 六、官方 MCP 面试题（含追问链）

### Q1. 官方 MCP 的 SDK 你熟吗？具体怎么用？
**答**：熟。Server 端两种写法：FastMCP（函数即工具，`@mcp.tool()`，自动生成 Schema）或经典 `Server` API（`@server.list_tools()` + `@server.call_tool()`）；Client 端底层是 `stdio_client` + `ClientSession`（initialize → list_tools → call_tool），配 LangGraph 用 `langchain-mcp-adapters` 的 `MultiServerMCPClient`：`get_tools()` 自动发现 → `llm.bind_tools(tools)` → `call_tool(name, args)`。**我项目里这些环节都有对应物，因为我自研版就是把这个协议手写了一遍。**

### Q2. 为什么你项目不用官方包，要自己实现？
**答**：三个理由：①**零依赖可控**：早期为了不引入维护压力、亲手吃透协议（能讲清 JSON-RPC 每一步）；②**可靠性工程是自研优势**：官方 SDK 不默认提供"进程崩溃自愈、响应超时、RLock 串行防串线"——这几个恰恰是我在并发/稳定性上做的核心工作（mcp_client.py:39,137,174），自己实现可以精确控制；③**教学/面试价值**：手写一遍协议让我对 MCP 的理解远比"调包"深。**代价是协议演进要自己跟**，生产环境更倾向官方包 + 自包可靠性层。

### Q3. 你自研的 MCP 和官方协议兼容吗？
**答**：兼容。同样是 JSON-RPC 2.0 over stdio，方法名一致：initialize（协议版本 2024-11-05）/ notifications/initialized / tools/list / tools/call，响应格式 `{content:[{type:"text",text:...}]}`。**所以我的三个 server 理论上能被任何官方 MCP Client 连接**——协议兼容是 MCP 的核心价值。

### Q4. FastMCP 和 Server API 什么区别？什么时候选哪个？
**答**：FastMCP 是高层封装：函数签名→Schema、docstring→description，写新工具最快，适合绝大多数业务；Server API 是底层：手动构造 Tool/资源/提示词、控制初始化选项，适合要精细控制或老代码兼容。**类比：FastMCP 像 Flask 路由装饰器，Server API 像 WSGI 裸接口。**

### Q5. stdio 和 Streamable HTTP 传输的区别？什么时候用 HTTP？
**答**：stdio 走子进程标准输入输出，**同机部署**、零网络配置、天然安全（进程隔离），但一个连接一个进程，无法跨机器；Streamable HTTP 走 HTTP 请求，支持**远程部署、连接复用、鉴权、多进程服务**。选型：开发/单机/教学用 stdio（本项目场景）；生产多副本、工具服务独立部署、要横向扩容 → HTTP。**这也是我项目"高并发演进方向"的答案之一。**

### Q6. langchain-mcp-adapters 内部做了什么？
**答**：两个桥：①`get_tools()` —— 对每个 server 调 `tools/list`，把 MCP 格式（name/description/inputSchema）转成 LangChain `bind_tools` 要的格式；②`call_tool()` —— 调 `tools/call`，把返回的 `content`（TextContent 等）展平成字符串/结构化给 Agent。**我自研版 `get_tools_for_langchain`（mcp_client.py:216-234）就是它的手写替代。**

### Q7. 官方 MCP 的局限？（主动说 = 加分）
**答**：①协议 2024.11 才发布，还在演进（API 可能变动）；②多 3 个子进程，进程管理/调试链路变长；③对"2 个工具 1 个 Agent"的简单项目是杀鸡用牛刀；④桥接库成熟度需验证；⑤官方 SDK 默认不负责"进程僵死自愈/超时"这类可靠性，要自己包。**对一个 3 Agent + 5 工具 + 面试展示的项目，MCP 是合理的；但对简单 demo 反而过度设计。**

### Q8. 用 MCP Inspector 调试过吗？
**答**：`npx @modelcontextprotocol/inspector python src/mcp_servers/product_server.py` 启动可视化调试器，能逐个调工具、看 schema、看 JSON-RPC 原始报文——比 print 大法专业。面试提一下表示你用过官方调试生态。

### Q9. MCP 和 Function Calling 的关系？（高频）
**答**：**Function Calling 是"模型侧"能力**——模型如何在生成时表达"我要调用某工具"（tool_calls）；**MCP 是"服务侧"标准**——工具如何被描述/发现/调用。两者是上下游：MCP Server 提供工具 → Client 转成 function-calling schema 给模型 → 模型输出 tool_calls → Client 再经 MCP 执行。**可以说 MCP 是 Function Calling 时代的"工具供应协议"。**

### Q10. MCP 生态还了解什么？
**答**：①Resources/Prompts：MCP 不止工具，还能暴露数据资源和提示模板；②Remote MCP / Streamable HTTP：远程服务化；③A2A（Agent-to-Agent）：Anthropic 后续推的 Agent 间通信协议；④各大生态（Claude、LangChain、Cursor、IDE、云厂商）都接入了 MCP——**"写一次工具，全生态可用"**是它的终极价值。

---

## 七、一句话速记

```
官方 MCP = 协议标准化 + SDK 封装（FastMCP / ClientSession / langchain-mcp-adapters）
你项目   = 协议手写实现 + 自己加的可靠性工程（锁/超时/自愈）

面试话术：我的自研版把官方 SDK 的活手写了一遍（initialize/tools/list/tools/call），
官方包我熟（FastMCP、ClientSession、MultiServerMCPClient、MCP Inspector），
自研是为了零依赖 + 把"崩溃自愈/超时/串线防护"这些可靠性做进客户端；
生产环境我倾向官方包 + 自包可靠性层 + Streamable HTTP。
```