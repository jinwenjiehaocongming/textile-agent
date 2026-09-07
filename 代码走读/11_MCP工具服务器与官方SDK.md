# 11 · MCP 工具服务器与官方 SDK（异步子进程生命周期）

> 目标：看懂 app.py 顶部那三行 SERVERS 字典，是怎么变成「三个能独立干活的外部工具服务」的。
> 覆盖官方 MCP SDK 的 `ClientSession` + `stdio_client` 异步子进程管理、工具动态发现、协议握手、
> 三个 FastMCP server 各自注册什么工具，以及工具结果怎么结构化回传前端。
> 04 步讲的是 init 被谁触发；这一份聚焦**协议与生命周期**。

---

## 一、先建立心智模型：从「import 函数」到「发一条消息给子进程」

| 角色 | 文件 | 干什么 | 进程边界 |
|---|---|---|---|
| Agent 大脑 | `src/agent.py` `order_agent.py` | LLM 只会对着 JSON Schema 说"我要调这个工具" | 主进程 |
| 客户端网关 | `src/mcp_client.py` | 管 3 个子进程的连/断、按工具名路由调用 | 主进程 |
| 工具工人 | `src/mcp_servers/*.py`（3 个） | 真查真写 PostgreSQL | **独立子进程** |
| 数据库 | `src/db.py` | 工具和 Agent 共用同一套 PG（不是各连各的） | PG 服务 |

MCP（Model Context Protocol）是 Anthropic 2024 年底发布的**工具调用标准协议**。
类比 USB-C：以前"给设备充电"各家一个接口（每个 Agent 自己写一套工具定义 + 实现），
MCP 之后统一成一根线——**Server 声明能力，Client 动态发现，谁都能插**。

本项目最直观的落地：

```
旧（改前）：agent.py 里写死 SEARCH_PRODUCT_SCHEMA，直接调 search_product() —— 工具和大脑同进程
新（现在）：product_server.py 独立进程暴露 search_product
          agent.py 通过 MCP Client 连上后自动发现工具 —— 大脑和工具隔着 JSON-RPC
```

**stdio 传输 = 子进程的标准输入/输出当电话线**：SDK 把子进程的 stdin/stdout 变成两条
异步流，JSON-RPC 消息从这条管线走；日志走 stderr，不污染协议通道（SDK 内部处理，
见 `src/mcp_client.py:7` 注释"原自研版日志走 stderr 防污染协议通道由 SDK 内部处理"）。

## 二、SERVERS 注册表与 lifespan：三个子进程从哪里来（`app.py:44`）

```python
SERVERS = {
    "product": ["python3", "src/mcp_servers/product_server.py"],
    "order":   ["python3", "src/mcp_servers/order_server.py"],
    "refund":  ["python3", "src/mcp_servers/refund_server.py"],
}
```

每条命令都是 **`[解释器, 脚本路径]`**，即"用 python 拉起这个文件"。这三行就是
启动清单。谁消费它？lifespan（`app.py:51`）：

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🔌 连接 MCP 工具服务器...")
    await init_mcp(SERVERS)          # 启动阶段：连好才对外服务（fail-fast）
    yield
    from src.mcp_client import get_mcp
    await get_mcp().shutdown()       # 关闭阶段：逐个释放子进程
```

三个要点：

1. **在 `yield` 之前连接**——服务还没开始收请求，子进程已就绪。缺依赖 / 缺 PG 会在
   启动时就炸，而不是第一个客户来问价时炸（启动即校验，fail-fast）。
2. `shutdown` 挂在 yield 之后，FastAPI/uvicorn 优雅退出时执行（04 步讲 init 触发时机，
   这里聚焦"关了之后谁收拾"）。
3. SERVERS 是模块级 dict，可随时加第四个 server（比如未来接个汇率查询服务），
   `init_mcp` 只认这个结构。

## 三、connect_all：一条命令如何变成一个活进程（`mcp_client.py:33`）

`AsyncMCPClient.connect_all` 是整份文件的核心，拆成六小步：

```python
async def connect_all(self, servers: dict[str, list[str]]) -> None:
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name, cmd in servers.items():
        params = StdioServerParameters(
            command=sys.executable,          # ① 换成"当前解释器"！
            args=cmd[1:],                    #    命令里的 "python3" 被剥掉
            env={**os.environ, "PYTHONPATH": project_root},  # ② 注入项目根
        )
        stdio_cm = stdio_client(params)                    # ③ 拿到进程上下文管理器
        read, write = await stdio_cm.__aenter__()          #    spawn 子进程 + 两条流
        session_cm = ClientSession(read, write)
        session = await session_cm.__aenter__()            # ④ 建会话
        await session.initialize()                         # ⑤ 协议握手
        listed = await session.list_tools()                #    动态发现工具
        for t in listed.tools:
            self.tools.append({... "_server": name})
            self._tool_server[t.name] = name               # ⑥ 登记路由表
        self._sessions[name] = session
        self._hold.extend([stdio_cm, session_cm])          # 防 GC 提前关闭
```

**① 为什么命令写 `python3`，代码却用 `sys.executable`？**
`StdioServerParameters(command=sys.executable, args=cmd[1:])` —— `cmd[0]`（"python3"）
被**丢弃**，真正拉起子进程的是当前进程的解释器。原因写在注释（mcp_client.py:37）：
用系统 `python3` 可能没装 `mcp`/`sqlalchemy` 依赖，而 `sys.executable` 一定是跑
主服务的那个 venv 解释器——**依赖保证**。SERVERS 里的 `"python3"` 只是给人读的意图。

**② PYTHONPATH=project_root**：子进程里 `from src.db import ...` 才能解析
（server 和主进程 import 同一套代码，见第四节）。

**③④ 两次 `__aenter__`**：`stdio_client` 是异步上下文管理器，`__aenter__` 帮你
`subprocess.Popen` 并把 stdin/stdout 变成两条 asyncio 流（read/write）；`ClientSession`
再包一层会话。**⑤ initialize** 是 MCP 三步握手的第一次对话（initialize → tools/list →
tools/call，见 mcp_client.py:6 注释）——等 server 确认协议版本兼容才继续。

**⑥ 两张登记表 + `_hold`**：`_tool_server`（工具名→哪个 server）、`_sessions`
（server 名→会话）——`call_tool` 靠它们路由。`_hold` 是本文件最容易被忽略却最关键的
一行：`stdio_cm` / `session_cm` 如果只是局部变量，函数结束被 GC 时 asyncio 流会关闭、
子进程会退出；把它们**留作实例字段**就是告诉 Python"别扔，我还要用"。
（面试点：async 资源管理最容易踩的坑就是"上下文管理器被回收"。）

### 全局单例（mcp_client.py:107-122）

```python
_client: Optional[AsyncMCPClient] = None

async def init_mcp(servers):     # 幂等：第一次建对象，之后只补连新 Server
    global _client
    if _client is None:
        _client = AsyncMCPClient()
    await _client.connect_all(servers)
    return _client

def get_mcp() -> AsyncMCPClient:
    if _client is None:
        raise RuntimeError("MCP 未初始化：请先 await init_mcp(servers)")
    return _client
```

`init_mcp` 只管启动（lifespan 调一次）；`get_mcp()` 供图里任何节点随时取用——
拿不到就抛错，杜绝"还没连就调工具"的静默空指针。CLI 入口也用同一套
（agent.py:566-570 init、641-642 shutdown）。

## 四、三个 FastMCP server：@mcp.tool() 注册的到底是什么

三个文件结构完全同构：顶部 `mcp = FastMCP("xx-server")`，中部若干 `@mcp.tool()`
装饰的 async 函数，底部统一收尾：

```python
if __name__ == "__main__":
    sys.exit(mcp.run(transport="stdio"))   # product_server.py:68
```

`mcp.run(transport="stdio")` 就是"我是工具服务方，我从标准输入读 JSON-RPC 请求，
干完活把结果写回标准输出"。FastMCP 自动完成握手和参数校验——连"拒绝未声明字段"
这种校验都由内建 schema 取代（product_server.py 模块注释第 8 行）。

| Server | 工具（函数名） | 读写 | 谁把它绑给 LLM |
|---|---|---|---|
| product（1 个工具） | `search_product` | products 只读 | 售前 Supervisor（agent.py:280） |
| order（2 个工具） | `query_order_status` `create_order` | orders 读/写 | 售前（只查状态）+ 下单 Agent（order_agent.py:186） |
| refund（2 个工具） | `query_order` `create_refund` | orders 读 + refunds 写 | 售后 Agent（after_sales_agent.py:110） |

工具内部查/写 PostgreSQL 全部走 `src/db` 的通用层：`query_all` / `query_one` / `execute`
（如 product_server.py:14+39、order_server.py:12+22+54）。注意**这不是子进程自建连接**，
而是 import 主项目同一套异步 DB 层——MCP 解耦的是"进程边界"，不是"存储"。

### product_server：搜面料（product_server.py:19-65）

```python
@mcp.tool()
async def search_product(query: str) -> str:
    keywords = [kw.strip().lower() for kw in
                query.replace("，", " ").replace(",", " ").split() if kw.strip()]
```

一个工具、一个参数 `query: str`，内部三段式搜索：

1. **主查询**：关键词拆开后 `_compose_search_sql` 生成 `(name LIKE :n0 OR color LIKE :c0
   OR category LIKE :g0) OR ...`，命名参数防注入，`LIMIT 50`（product_server.py:19-26, 39）。
   所以**搜索维度是名称/颜色/品类三个字段的 OR LIKE**，别的不搜。
2. **Bigram 回退**：整词没命中就拆 2 字片段再查——"羽绒服"拆成 羽绒/绒服
   （product_server.py:42-46），救中文分词式输入。
3. **打分排序取 Top10**：命中关键词越多越靠前（51-56），然后拼成一行行给人读的文本
   （59-65）：货号 | 名称 | 颜色 | 门幅 | 规格 | 库存 | MOQ | 单价 | 交期。

为什么返回**文本**而不是 JSON？因为读它的是 LLM（不是前端）——下一条消息会把这段文本
继续喂给模型推理。前端要的表格走 render 工具（第六节），两层各司其职。

### order_server：查单 + 下单（order_server.py:19-90）

`create_order` 的签名就是 LLM 必须集齐的字段（order_server.py:34-45）：

```python
async def create_order(
    customer_id: str, product_id: str, product_name: str, color: str,
    quantity: int, unit_price: float,
    phone: str = "", address: str = "", delivery_date: str = "",  # 可选
) -> str:
    """为客户创建面料采购订单。仅在客户明确确认下单后调用。"""
```

两个被追问最多的点：

- **total 谁算**：`total = round(quantity * unit_price, 2)`（order_server.py:51）——
  服务端自己乘，不信任调用方传 total。但 `quantity>0`、`product_id` 是否存在这类业务
  校验**没有**——工具信任"能走到这里的人"（docstring 的"仅在客户明确确认后调用"是靠
  Agent 提示词 + HITL 人工审批拦住的，见 08 步）。
- **order_no 怎么生成**（order_server.py:49-50）：

```python
order_no = (f"ORD-{now.strftime('%Y%m%d')}-"
            f"{now.strftime('%H%M%S')}{now.microsecond:06d}{random.randint(1000, 9999)}")
```

`日期-时分秒+6位微秒+4位随机` → 同一秒并发多单也不会撞 `orders.order_no` 的 UNIQUE。
写入时状态写死 `'待付款'`（:59）；失败则 `_logger.exception` + 返回客户能听懂的话术
（69-71："订单生成失败……销售同事会尽快联系您"）。

`query_order_status`（19-31）就一行 SELECT + 格式化输出，是售前 Agent 回答"我的订单
到哪了"的唯一通道。

### refund_server：售后（refund_server.py:15-48）

- `query_order(order_no)`：和 order_server 的查单功能重复但**多返回电话/地址**——
  售后要联系客户（refund_server.py:16-28）。同一逻辑在按领域拆的两个进程里各有一份，
  这是"领域边界 > DRY"的取舍（诚实点：真要复用应抽公共库）。
- `create_refund(order_no, reason)`：INSERT refunds，状态 `'待审核'`（36-39）。
  注意它**不校验订单归属、不校验是否符合退货规则**——规则判断在上游售后 Agent
  （09 步），工具只管落库。

## 五、调用时：LLM 说"我要 search_product"之后发生了什么

LLM 永远看不到子进程——它拿到的是 `get_tools_for_langchain` 转出来的
**OpenAI function 规范描述**（mcp_client.py:79-91，`bind_tools` 专用），
Agent 层根本不知道"有三个进程"这回事。真正执行在图的工具节点：

```python
# agent.py:315-328  售前工具执行节点
async def tool_executor(state):
    ...
    for tc in last_msg.tool_calls:
        name, args = tc["name"], tc["args"]
        if name in RENDER_TOOL_NAMES:              # render 工具是"假执行"
            results.append(ToolMessage(content="", tool_call_id=tc["id"]))
            continue
        result = await get_mcp().call_tool(name, args)   # ← 真调用在这
        results.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
```

而 `call_tool`（mcp_client.py:65-77）内部四步：查 `_tool_server` 路由 → 找不到返回
`"未知工具: xxx"` → 找到则 `session.call_tool(name, arguments=args)` → 出异常也**不抛**，
返回 `"工具 xxx 调用失败: ..."`（截断 120 字符）。

```python
try:
    result = await self._sessions[server].call_tool(name, arguments=args or {})
except Exception as e:
    return f"工具 {name} 调用失败: {str(e)[:120]}"      # 失败文本化，交给 LLM 消化
if getattr(result, "isError", False):
    return f"工具 {name} 执行错误"                       # 服务端自己报的错也文本化
parts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
return "\n".join(parts) if parts else str(result)
```

设计动机：**调用失败不要打断 Agent 思考**——LLM 拿到"工具 xxx 调用失败"能自己换说法、
换工具重试；把异常抛给上层只会让整轮对话崩掉。代价（诚实取舍）：错误被压成字符串，
业务层拿不到结构化错误码；`call_tool` 本身**没有超时参数**，真卡死只能靠外层
（SSE 的队列哨兵 / 前端 60s AbortController）兜底。

下单是特殊路径：`create_order` 不是直接走 call_tool，而是被 HITL 拦一道——
`_approve_then_create`（order_agent.py:158-168）先 `interrupt` 挂起等人工审批，
审批通过才 `return await get_mcp().call_tool("create_order", args)`（:166）。
所以**工具是可信执行的最后一公里，前面有 08 步那套人工闸门**。

## 六、结构化回传前端：render_tools 是怎么"逼"LLM 交 JSON 的

工具 server 返回的是给人/给 LLM 读的文本，但前端要画表格。方案：
**让 LLM 在需要展示数据时强制调"展示工具"，把数据以 JSON 参数交出来**——这就是
`render_tools.py`（模块 docstring 1-16 写得很直白：这些工具不执行任何操作，
返回空字符串，作用是让数据从 tool_calls 参数里"交出来"）。

三层设计：

1. **Schema 与表字段严格对应**：`PRODUCT_ITEM`（render_tools.py:25）、`ORDER_ITEM`（:40）、
   `REFUND_ITEM`（:58）。字段类型故意写成 `["integer", "string"]` 这种宽容联合
   （如 :32 stock）——LLM 输出数字常带引号，校验太严会把表格逼疯。
2. **描述即规矩**：三个工具的 description 告诉 LLM 何时调用；`render_order` 更是写了
   红线条文"严禁编造订单号/状态，没有真实查询/生成结果绝不能调用"（:97-102）。
   同款红线还注入 System Prompt（`RENDER_PROMPT_HINT`，:138-158，agent.py:276 拼进
   system）。这就是"先画靶子再放箭"——不是事后解析，而是事前逼 LLM 结构化。
3. **提取双保险**（stream_chat.py:161-170）：

```python
final_msgs = values.get("messages") or []
render_data = extract_render_data(final_msgs)       # ① 找 render 工具调用参数
if not render_data:
    render_data = extract_data_from_tools(final_msgs)  # ② 兜底：从工具结果反推
evt = {"type": "done", "content": final_reply}
if render_data:
    evt["data"] = render_data                         # 挂到 done 事件 → SSE → 前端
```

`extract_render_data`（render_tools.py:164-196）先找消息上显式挂载的 `render_data`
（order/after_sales Agent 返回前用 `attach_render_data` 挂好，:199-209），再兜底倒序
扫最后一个 render 工具调用。`extract_data_from_tools`（:221-274）是"LLM 没听话"时的
急救：扫到 `create_order`/`create_refund` 的 tool_calls 后，去它**后面紧跟的
ToolMessage** 文本里用正则抠订单号（`_find_order_no`，:277-281，`ORD-\d{8}-\d{4,}`），
再拿调用参数拼出完整对象（total 用 args 里的数量×单价重算，:247-249）。

于是不管 LLM 乖不乖，下单/退款场景几乎必出结构化数据。诚实说：**售前查产品没有这条
兜底**——若 LLM 不调 `render_products`，产品就只以纯文本呈现（售后/下单场景因事关
订单真实性做了兜底，产品展示没做）。这就是数据与前端渲染的那座桥，13 步会讲 React
侧怎么把它画出来。

## 七、生命周期全景：谁负责回收子进程

```
lifespan 进入 ──► init_mcp(SERVERS)
                  └─► connect_all：每 server spawn 子进程 → initialize → list_tools → 登记
请求期          ──► 图节点 get_mcp().call_tool(name, args) → 按 _tool_server 路由 → session 转发
lifespan 退出   ──► shutdown()
                  └─► 每个 session.__aexit__(...) → SDK 关闭会话、关管道 → 子进程退出
```

正常退出路径是**显式且尽力而为**的：`shutdown`（mcp_client.py:93-102）逐个
`await session.__aexit__(None, None, None)`，异常吞掉继续关下一个，最后清空三张表。
那么异常退出呢？诚实盘点：

- **connect_all 中途失败**（某 server 起不来 / initialize 超时）→ 直接抛 → 服务启动失败。
  fail-fast，不留半残状态。
- **运行期某子进程自己崩了** → 没有健康检查、**没有自动重连**：下次 call_tool 会因管道
  关闭抛错，被兜底成"工具 xxx 调用失败"文本（第五节的 except）。主进程不受影响，
  但那个工具从此"哑"到服务重启。生产该加 watchdog/重连，当前是取舍。
- **主进程被 kill -9** → shutdown 没机会跑，子进程读到自己 stdin 管道关闭（EOF）后由
  FastMCP 主循环自然退出；极端残留靠 OS 收拾，无守护进程兜底。

类比：**部门经理 + 三个外包工人**。lifespan 是入职流程（面试→发工牌→排工位），
call_tool 是派活，shutdown 是离职流程。SDK 帮你把"雇人/开工资/辞退"这些脏活标准
化，但"工人病了要不要招替补"（重连）得你自己写——这正好是面试可以往下聊的地方。

---

## Q&A

**Q1：MCP 相比"Python 直接 import 工具函数"到底换来了什么、代价是什么？**
换来：① 工具与大脑进程解耦——崩溃隔离（查单 server 崩了聊天还活着）；
② 协议标准化——三步握手、参数 schema 校验、动态发现都归 SDK，`bind_tools` 只消费
`get_tools_for_langchain` 的产物，加个新工具零改动 Agent 代码；③ 演进空间——stdio
换成 http/sse 就能变远程服务，`StdioServerParameters` 那一段是唯一要动的地方。
代价：跨进程 JSON 序列化慢几个数量级（本地工具调用是 μs，现在是 ms 级）；调试要从
"print 大法"升级为看协议；错误被字符串化（见第五节的 except）；以及本项目一个特殊
现实——三个 server 和主进程 **import 同一套 src.db、连同一个 PG**，所以它不是安全
边界也不是性能优化，主要是**架构规范化和面向未来的接口约定**。面试这样答才显诚实。

**Q2：stdio_client 拉起的子进程，异常退出谁回收？代码里有没有兜底？**
正常：lifespan yield 后 `shutdown()`（app.py:56-57）→ 逐个 `session.__aexit__` →
SDK 关会话、关管道，子进程随 stdin EOF 退出。`shutdown` 里每个 aexit 有 try/except
（mcp_client.py:94-98），一个关失败不影响其他。
异常：**没有兜底**——运行期无健康检查、无自动重连（Q1 已述）；主进程被强杀时 shutdown
跑不到，靠"父死管道断 → 子进程读 EOF 自然退出"这个 OS 机制兜着。面试追问"如果子进程
hang 死呢？"——诚实答：当前会一直 hang 到外层超时，生产应加 per-call 超时 +
Liveness 探测 + 按需重连，这正是自研版换官方版后仍要补的运维功课。

**Q3：工具返回的是文本，前端凭什么能拿到结构化表格？**
因为表格数据根本不指望工具文本——它走 `render_tools.py` 这条"展示协议"：
LLM 在要展示产品/订单/退款数据时**必须调 render 工具把 JSON 当参数交出来**
（schema 与表字段严格对应，render_tools.py:25-67），`stream_chat.py:163-169` 从最终
消息里提取这个 JSON 挂到 SSE `done` 事件的 `data` 字段（结构 `{type, data}`）。工具文本
只是给 LLM 自己看的中间产物。双保险：`extract_data_from_tools` 兜底（render_tools.py:221）
让"LLM 忘了调 render"也能从 create_order 的 ToolMessage 里用正则反推出订单对象。
一句话：**展示数据从"LLM 的嘴"改走"LLM 的手"（结构化 tool_calls），前端才敢画表**。

**Q4：面试让讲"官方 SDK 怎么异步管理子进程生命周期"，该按什么顺序展开？**
按"建 → 发现 → 路由 → 销"四段讲，每段背一行代码：
① **建**：lifespan（app.py:51）调 `init_mcp(SERVERS)`；`connect_all` 里
`StdioServerParameters(command=sys.executable, args=cmd[1:], env=PYTHONPATH…)`
（mcp_client.py:42-46）——注意用当前解释器而非命令里的 python3，保证依赖；
`stdio_client(...).__aenter__()` spawn 进程拿到两条异步流。
② **发现**：`ClientSession(read, write).__aenter__()` → `initialize()` 握手 →
`list_tools()` 动态列出工具，登记进 `_tool_server`/`tools`，`_hold` 持有 CM 防 GC
（:47-62）。
③ **路由**：Agent 侧 `get_tools_for_langchain` 转 OpenAI schema 绑给 LLM
（:79-91，agent.py:280-281）；LLM 要调用时工具节点 `get_mcp().call_tool(name, args)`
按工具名查 `_tool_server` 找到会话转发（:65-77），异常文本化不打断 Agent。
④ **销**：lifespan 退出 `shutdown()` 逐个 `session.__aexit__`（:93-102）。
再补一句取舍：无超时、无重连、杀进程靠 EOF，是当前演示量级的诚实边界。

**Q5：SERVERS 命令明明写 `["python3", "src/..."]`，为什么实际跑的不是 python3？**
看 mcp_client.py:43-44：`StdioServerParameters(command=sys.executable, args=cmd[1:])`。
`cmd[0]` 的 `"python3"` 只是给人读的"意图声明"，真正拉起子进程的是 `sys.executable`
——也就是正在跑 FastAPI 的那个解释器（venv 里的 python），保证子进程 import 得到
`mcp`/`sqlalchemy` 等全部依赖。列表里留 `"python3"` 是文档化设计（默认系统解释器），
代码层永远用自己人。面试可补：如果将来 server 要单独用别的运行环境，只需把这条命令
换成 `["/path/to/venv/bin/python", ...]`，client 端零改动。

**Q6：create_order 为什么让服务端算 total、还要自己拼 order_no？**
`total = round(quantity * unit_price, 2)`（order_server.py:51）：金额是钱，绝不能信任
调用方（LLM 或任何中间层）传的 total——价格它可能记错、可能幻觉，服务端用入参
重算是唯一可信口径。order_no 同理：数据库 UNIQUE（db.py:96 `order_no TEXT UNIQUE`），
撞了整单 INSERT 失败；时间戳到秒 + 6 位微秒 + 4 位随机（order_server.py:49-50）让并发
同一秒的订单也几乎不可能撞号。代价（诚实）：随机段不是真正的分布式 ID 方案，将来
多实例 + 高并发应换发号器/雪花——当前单 PG 单进程完全够用。追问"两个客户恰好同时
下单会怎样？"——撞 UNIQUE 会抛异常走 :69 的 except 返回"请稍后重试"，不会写脏数据。

**Q7：如果未来想把某个工具迁到远程 MCP server（跑在别的机器），要改哪些代码？**
几乎只有 client 的连接段：把 `StdioServerParameters` + `stdio_client` 换成远程传输
（官方 SDK 的 streamable-http client），`connect_all` 其余部分（initialize/list_tools/
登记/路由）一字不用改——这就是协议标准化的红利。Server 侧把
`mcp.run(transport="stdio")` 换成 http 传输、配好地址即可。真正要重新想的是**安全与
鉴权**：本地 stdio 靠"同机进程 + PYTHONPATH"隐式信任；远程后每个 call_tool 都要
身份校验和审计，SERVERS 注册表也得支持配置化而不是写死在 app.py:44。
