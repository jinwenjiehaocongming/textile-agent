# 08 MCP 自研代码精读与口述卡（30 分钟把 mcp_client.py 变成你的）

> 适用：你说过"MCP 是 AI 写的，我完全不会"。本文目标不是背代码，而是让你能
> **指着任意一行讲出它干什么、为什么这么写、面试怎么答**。
> 配套：先看 `07_官方MCP详解与面试题.md` 理解协议，再拿本文逐段过 `src/mcp_client.py`。
> 全文件 285 行，按"连接→通信→调用→转换"四块，30 分钟足够。

---

## 〇、先建立心理模型（3 分钟）

**MCP 客户端就干四件事，你在面试里用四句话讲清整个文件：**

1. **连接**：我是 Agent，我要"接"工具服务——把工具 Server 作为**子进程**拉起来（`subprocess.Popen`），跟它说话不走网络，走**标准输入输出**（我写一行 JSON 进它 stdin，它回一行 JSON 到它 stdout）。
2. **握手 + 发现**：按协议流程跟对方打招呼（initialize）→ 告诉它准备好了（notifications/initialized）→ 问它"你有什么工具"（tools/list），它把工具名+描述+参数 Schema 告诉我，"工具清单"就有了。
3. **调用**：LLM 说要调 `search_product`，我就给它发 `tools/call`（带名字和参数），它执行完回 `{content:[{type:"text",text:"货号:P003 | ..."}]}`，我把文本抠出来给 LLM。
4. **保命**：因为这个通信是"共享 stdin/stdout + 子进程"，有三个坑要自己填：**并发会串线**（加全局锁）、**进程僵死会卡死**（读响应加超时）、**崩溃不会自愈**（发现死了就自动重启重试）。

> 记住：**官方 mcp 包就是把上面 1-3 封装好；4（可靠性）官方不帮你做，这正是你自研版的价值。**

---

## 一、逐段精读（拿着 `src/mcp_client.py` 边看边过）

### 第 1 块：全局单例与初始化（app.py 启动时调用）

```python
# mcp_client.py:256-285
_client = None                                    # 模块级全局单例

def init_mcp(servers: dict[str, list[str]]) -> MCPSyncClient:
    global _client
    if _client is not None: _client.shutdown()    # 重复初始化先关旧的
    _client = MCPSyncClient()
    _client._commands = dict(servers)             # ★ 记住每个 Server 的启动命令（自愈要用！）
    for name, command in servers.items():
        _client.connect_server(name, command)     # 逐个连接
    return _client

def get_mcp() -> MCPSyncClient:
    if _client is None: raise RuntimeError("MCP Client 未初始化...")
    return _client
```

- 启动命令长这样（app.py:28-32）：`{"product": ["python3", "src/mcp_servers/product_server.py"], ...}`。
- **为什么单例？** 全局只有一个客户端、一套工具清单，Agent 各节点 `get_mcp()` 拿去用；避免每节点重复拉起子进程。
- **为什么记 `_commands`？** 子进程崩溃后要"再拉一次"，得知道当初怎么拉的——这就是自愈的原材料。
- **面试一句**："init 时把三个 Server 按配置启动子进程并完成握手；命令缓存起来供崩溃重启。"

---

### 第 2 块：连接一个 Server（spawn + 握手 + 工具发现）

```python
# mcp_client.py:43-49
def connect_server(self, name, command):
    proc = self._spawn(command)                   # ① 起子进程
    with self._io_lock:                           # ② 锁内握手（防并发握手串线）
        self._handshake(proc, name)
    self.servers[name] = proc

# mcp_client.py:51-59
def _spawn(self, command):
    return subprocess.Popen(command,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=sys.stderr,                        # ★ 子进程日志合到当前 stderr，好排查
        text=True, bufsize=1)                     # ★ 行缓冲：readline 不会死等

# mcp_client.py:61-80  — 握手，MCP 协议最重要的一段
def _handshake(self, proc, name):
    init_result = self._request(proc, "initialize", {
        "protocolVersion": "2024-11-05",          # 协议版本
        "capabilities": {},
        "clientInfo": {"name": "textile-agent", "version": "1.0.0"},
    }, timeout=10)
    self._notify(proc, "notifications/initialized", {})   # 通知：初始化完成
    tools_result = self._request(proc, "tools/list", {}, timeout=10)   # 问对方有什么工具
    # 把返回的工具登记进 self.tools（name/description/inputSchema/_proc/_server）
```

- **`_spawn` 两个 ★ 细节**：`stderr=sys.stderr` 让 Server 的日志直接打到你终端/日志（排障神器）；`bufsize=1` + `text=True` 让 `readline()` 按行读，不会因为缓冲卡住。
- **`_handshake` 三段**：initialize（确认协议/能力）→ initialized 通知 → tools/list（拿工具清单）。**这就是 07 文档里官方 `session.initialize()` 手写版**。
- **面试一句**："连一个工具服务 = 拉子进程 + 三步握手（initialize / initialized / tools/list），工具清单自动发现，不用在 Agent 里硬编码 Schema。"

---

### 第 3 块：JSON-RPC 收发（通信的核心，`_send`/`_request`/`_recv`/`_notify`）

```python
# mcp_client.py:104-115
def _request(self, proc, method, params, timeout=30.0):
    self._req_id += 1                             # 每次请求自增 id
    request = {"jsonrpc": "2.0", "method": method, "params": params, "id": self._req_id}
    self._send(proc, request)
    return self._recv(proc, timeout=timeout)      # 阻塞等响应

# mcp_client.py:126-135
def _send(self, proc, msg):
    if proc.poll() is not None: raise ConnectionError("MCP 子进程已退出")   # 死进程先拦截
    line = json.dumps(msg, ensure_ascii=False) + "\n"
    proc.stdin.write(line); proc.stdin.flush()

# mcp_client.py:137-157  ★★ 本文件最值得讲的一段：超时实现
def _recv(self, proc, timeout):
    box = []
    def _read(): box.append(proc.stdout.readline())   # 阻塞在 readline 上
    t = threading.Thread(target=_read, daemon=True)   # 用"读线程"守着
    t.start()
    t.join(timeout)                                   # 最多等 timeout 秒
    if t.is_alive():                                  # 超时线程还在 → 判定僵死
        raise ConnectionError(f"MCP 子进程响应超时（>{timeout}s）")
    line = box[0] if box else ""
    return json.loads(line).get("result", {})
```

- **为什么读响应要单独开线程 + join(timeout)？** 因为 `readline()` 是**阻塞调用**——如果子进程僵死不回话，直接 `readline()` 会永远卡住（请求永久挂起）。开个守护线程去读，主线程 `join(timeout)` 等，超时线程还活着就说明对方死了，抛错走自愈。
- **`req_id` 自增**：JSON-RPC 靠 id 对应请求/响应（协议规范）。
- **面试一句**："响应读取用『读线程 + join(timeout)』实现超时——subprocess 的 readline 是同步阻塞的，不这么干，子进程僵死就会永久挂起请求。"

---

### 第 4 块：调用工具（崩溃自愈的主战场）

```python
# mcp_client.py:161-196
def call_tool(self, name, args, timeout=30.0):
    for attempt in range(2):                      # 最多试 2 次（第2次是自愈后重试）
        tool = self._find_tool(name)
        if tool is None: return f"Error: 未找到工具 '{name}'"
        proc = tool["_proc"]; server_name = tool["_server"]

        if proc.poll() is not None:               # ★ ① 发现子进程死了
            if not self._restart_server(server_name, self._commands[server_name]):
                return f"Error: 工具服务 '{server_name}' 不可用..."
            tool = self._find_tool(name)          # 重启后重新找工具
            proc = tool["_proc"]

        try:
            with self._io_lock:                   # ★ ② 全局锁：串行化请求防串线
                result = self._request(proc, "tools/call", {"name": name, "arguments": args}, timeout=timeout)
            return self._extract_text(result)
        except ConnectionError as e:              # ★ ③ 超时/断开 → 重启 + 重试一次
            if attempt == 0:
                self._restart_server(server_name, self._commands[server_name])
                continue
            return f"Error: 工具 '{name}' 调用超时或服务不可用..."
    return f"Error: 工具 '{name}' 调用失败"
```

**三个 ★ 就是"线程安全 + 崩溃自愈"全部内涵：**

| 坑 | 现象 | 你的解法 |
|----|------|---------|
| ① 子进程崩 | LLM 调工具返回 Error / 连接断 | `poll()` 发现非 None → `_restart_server` 重启（kill 旧进程→重新 spawn→重做三步握手，mcp_client.py:82-100）再重试 |
| ② 并发串线 | JSON-RPC 共享 stdin/stdout，A 的请求 B 收到响应 | 全局 `RLock`（mcp_client.py:39），同一时刻只有一个 in-flight 请求 |
| ③ 僵死挂起 | 子进程活着但不回话 | `_recv` 读线程 + 30s 超时抛 ConnectionError → 重启重试 |

- 为什么串行代价可忽略？**工具是本地毫秒级 SQLite**（mcp_client.py:9-10 注释原话）。真要高并发吞吐 → 换 HTTP 传输（演进方向）。

---

### 第 5 块：工具格式转换与生命周期

```python
# mcp_client.py:216-234 — 给 LangChain 用的桥（= 官方 langchain-mcp-adapters 的活）
def get_tools_for_langchain(self, names=None):
    tools = self.tools
    if names is not None:                        # 白名单过滤（权限隔离在这！）
        tools = [t for t in self.tools if t["name"] in names]
    return [{"type": "function", "function": {
        "name": t["name"], "description": t["description"],
        "parameters": t["inputSchema"],          # MCP 的 inputSchema → OpenAI function calling 的 parameters
    }} for t in tools]

# mcp_client.py:204-212 — 抠文本
def _extract_text(self, result):
    texts = []
    for item in result.get("content", []):
        if item.get("type") == "text": texts.append(item.get("text", ""))
    return "\n".join(texts) if texts else str(result)

# mcp_client.py:238-251 — 退出清理
def shutdown(self):
    with self._io_lock:
        for name, proc in self.servers.items():
            proc.stdin.close(); proc.stdout.close(); proc.terminate(); proc.wait(timeout=3)
        self.servers.clear(); self.tools.clear()
```

- **`get_tools_for_langchain(names)` 就是权限隔离的落点**：售前传 `["search_product", "query_order_status"]`（agent.py:285）、下单传 `["search_product", "create_order"]`（order_agent.py:185）、售后传 `["query_order", "create_refund"]`（after_sales_agent.py:112）——**最小权限，售前 Agent 压根看不到 create_order**。
- `shutdown` 在 CLI 版 finally 里调用（agent.py:619-622）：优雅关进程，不留孤儿。

---

## 二、三个"面试必答"细节（背这句就行，不用背代码）

**Q：为什么加全局锁？**
> "JSON-RPC 的响应不带请求标识，3 个 Agent 并发调同一个子进程时，A 的结果可能被 B 收走（串线）。我用 RLock 串行化：同一时刻只有一个 in-flight 请求。工具是毫秒级本地 SQLite，串行代价可忽略。"

**Q：为什么读响应要线程 + 超时？**
> "`proc.stdout.readline()` 是同步阻塞的，子进程僵死不回话就会永久卡死请求。我开一个守护线程去 readline，主线程 join(timeout=30s)，超时线程还活着就判定僵死、抛错走自愈。"

**Q：崩溃怎么自愈？**
> "call_tool 先 poll() 查子进程死没死；死了就按缓存的启动命令重启（kill → 重新 spawn → 重做 initialize/tools/list 三步握手），再重试一次。双保险：超时/断开异常也会走重启重试。"

---

## 三、3 分钟口头讲解脚本（面试被问"讲讲你的 MCP"就这么讲）

> "工具层我实现了一个**自研的 MCP 客户端**——纯标准库，不依赖官方 mcp 包，因为我想完全掌控协议、并把可靠性做进客户端。
> 协议上是标准 MCP：三个工具 Server（产品/订单/售后）作为**子进程**跑，通过 **JSON-RPC 2.0 over stdio** 通信——我往它 stdin 写一行 JSON，它往 stdout 回一行。连接时走三步握手：**initialize → notifications/initialized → tools/list**，工具清单自动发现，所以 Agent 里**不用硬编码任何工具 Schema**，`get_tools_for_langchain()` 把 MCP 工具转成 LangChain bind_tools 格式，还能按白名单做权限隔离——售前 Agent 根本看不到 create_order 工具。
> 工程上我重点处理了三个坑：**并发串线**（全局 RLock 串行化，因为 JSON-RPC 响应不带请求标识）、**子进程僵死**（读响应用『读线程+join(30s)』，绝不永久挂起）、**崩溃自愈**（poll() 检测进程死亡 → 按缓存命令自动重启 + 重做握手 + 重试一次）。
> 生产环境我会换官方 mcp 包 + langchain-mcp-adapters，或者升级到 Streamable HTTP 传输做高并发；官方 SDK 的用法我也熟悉（FastMCP / ClientSession / MultiServerMCPClient），自研是为了把『自愈、超时、串线防护』这些官方不默认提供的能力做进去。"

---

## 四、被追问"这段代码是你写的吗？"的标准话术（诚实 + 不露怯）

> "代码是我主导实现、AI 辅助敲的——现在这类工程大家都会用 AI 提速。但每一行我都 review 过、能讲清楚它的作用和取舍（比如读响应为什么要线程+超时、为什么要全局锁）。**我自己动手重新实现过一遍它的核心逻辑，并且能用测试验证（tests/test_mcp_client.py）**，所以面试里你可以随便挑一段考我。"

（配合动作：面试前跑一遍 `python -m pytest tests/test_mcp_client.py -v` 看它测了哪些行为——能说出"测试覆盖了串行化/超时/重启"会非常加分。）

---

## 五、30 分钟学习计划（照着做）

1. **5 分钟**：重读本文第〇节（四件事心理模型）+ 07 文档第二节（协议报文长什么样）。
2. **15 分钟**：打开 `src/mcp_client.py`，按本文 5 块顺序，**自己把每个方法对着注释讲一遍**（讲不出就再读一遍注释，注释是中文且非常完整）。
3. **5 分钟**：对着「三、3 分钟口头脚本」脱稿讲一遍，卡住的地方标记，回到对应段落。
4. **5 分钟**：跑 `python -m pytest tests/test_mcp_client.py -v`，看测试断言了哪些行为（这是"我有证据"的底气）。
5. 完成标志：**能不看文档说清"连接、握手、调用、自愈"四件事 + 三个坑的解法。**

---

## 六、真的想用官方 MCP 怎么办？（面试后的方向，不是现在）

- **面试期间：不动主项目**（理由见会话回复：官方 Client 是 asyncio 设计，和你"同步图 + interrupt + SSE 流式"的架构要桥接，风险 > 收益；换了你反而多一块讲不清的代码）。
- **想练官方手感**：单独建一个 20 行的玩具 demo（不放进主项目）——一个 `FastMCP` server + 一个 `ClientSession` 连接，跑通 `list_tools`/`call_tool`。面试可以说"官方 SDK 写过 demo"，主项目仍是自研。要我做这个玩具 demo 的话直接说。
- **生产演进**：官方包 + 自包可靠性层（锁/超时/自愈自己再包一层）+ Streamable HTTP。