# 04 · 服务启动 · FastAPI 入口与 MCP 初始化

> 目标：回答三个问题——**服务是怎么"活"起来的、关的时候怎么"善后"、
> 一个 HTTP 请求进来先过哪些关卡、最后落在哪行代码上**。
> 覆盖 `python app.py` 之后的一切：uvicorn、lifespan 钩子、MCP 子进程、
> CORS、静态托管顺序、`/api` 前缀兼容、`/healthz`。
> MCP 客户端的协议细节只点到位，深挖在 11 步。

---

## 一、先建立心智模型：三个时间段

一个 Web 服务的一生，可以切成三段：

| 时间段 | 谁在干活 | 代码在哪 | 一句话 |
|---|---|---|---|
| 启动前（import 阶段） | Python 解释器 | `app.py:1-432` 模块顶层 | 把所有类/函数/路由"登记"好，**还没开张** |
| 启动后（服务期） | uvicorn + 事件循环 | `app.py:426-432` 的 `uvicorn.run` | 开店营业，一个请求进来走一遍路由 |
| 生命周期钩子 | FastAPI lifespan | `app.py:51-57` | 开店前**备货**（连 MCP 子进程），打烊后**锁门**（关子进程） |

类比：**lifespan 是餐厅的开店/打烊流程**——开店前厨师（MCP 子进程）必须就位、
食材（工具）盘点好；营业期间只管接客；打烊后厨具归位、煤气关掉。
如果直接在模块顶层就启动 MCP 子进程，事件循环还没跑起来，异步连不上；
如果永远不关，进程退出时会留下一堆孤儿子进程。

顶层注释一句话交代了历史（`app.py:1-7`）：「FastAPI + 原生 HTML，模仿企业微信界面」——
早期版本用 HTML 页面，后来换成了 React（`web/dist`），但服务入口一直没变。

## 二、入口：`__main__` 里的 uvicorn.run（app.py:426-432）

```python
if __name__ == "__main__":
    import uvicorn
    print("="*50)
    print("🏭 交易智能体 Web 版")
    print("   打开 http://127.0.0.1:8005")
    print("="*50)
    uvicorn.run(app, host="0.0.0.0", port=8005, log_level="warning")
```

逐词翻译：
- `import uvicorn` 放在 `if __name__ == "__main__"` **里面**而不是文件顶部——
  uvicorn 只是"跑起来"的工具，import 本模块（比如测试里 `import app`）不该拖它下水。
- `app` 是模块级已建好的 FastAPI 实例（`app.py:60`），`uvicorn.run(app, ...)` =
  告诉 uvicorn「用这个 app 对象开 HTTP 服务器」。
- `host="0.0.0.0"`：监听所有网卡——Docker 容器里必须这样，否则外部访问不到。
- `port=8005`：记得 01 步跑起来的口诀 `python app.py → http://127.0.0.1:8005`。
- `log_level="warning"`：只打警告以上日志，**请求日志被静音**——跑起来很干净，
  但排错时看不到请求，这是故意的（日志统一走 `src/logging_config.py`，15 步讲）。

启动顺序的真相：`uvicorn.run(app)` 会先触发 **lifespan 启动钩子**，再开始接请求。
而 `app` 这个对象在建出来的时候（`app.py:60-61`），已经把路由和一张 Agent 图都准备好了：

```python
app = FastAPI(title="交易智能体", lifespan=lifespan)  # 60
agent_graph = build_graph()                            # 61
```

注意 `build_graph()` 是**模块 import 时就执行**的（不是启动钩子里）。
它只搭图的骨架 + 建一个 MemorySaver（内存版 checkpointer），不连数据库不建 LLM——
所以 import 很快、没有副作用（`agent.py` 的懒加载设计见 07 步）。
`agent_graph` 是全局单例，聊天端点直接拿它 `ainvoke/astream`。

## 三、lifespan：MCP 子进程的生与死（app.py:51-57）

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🔌 连接 MCP 工具服务器...")
    await init_mcp(SERVERS)          # 启动时：拉起三个 MCP 子进程并握手
    yield                            # ← 这一行开始才正式接客
    from src.mcp_client import get_mcp
    await get_mcp().shutdown()       # 关闭时：优雅关掉所有子进程会话
```

语法要点（细讲在 03 步）：`@asynccontextmanager` 把函数变成"异步上下文管理器"，
`yield` 前面的代码 = 启动钩子，`yield` 后面 = 关闭钩子。FastAPI 保证：**先跑启动段，
然后才接收请求；收到关闭信号后，跑完关闭段再退出进程**。

被拉起来的"员工"是谁？注册表就在 lifespan 上面（`app.py:44-48`）：

```python
SERVERS = {
    "product": ["python3", "src/mcp_servers/product_server.py"],
    "order":   ["python3", "src/mcp_servers/order_server.py"],
    "refund":  ["python3", "src/mcp_servers/refund_server.py"],
}
```

每个条目是一条启动命令：跑一个 FastMCP server 脚本，通过 **stdio**（标准输入输出流）
跟主进程通信。`init_mcp`（`src/mcp_client.py:110-116`）内部做了三件事：

1. 全局单例 `_client` 没有就 new 一个（`mcp_client.py:113-114`）；
2. `await _client.connect_all(servers)` 逐个连接；
3. `connect_all`（`mcp_client.py:33-63`）每个 server 都走**官方 SDK 的四步**：
   `stdio_client` 开子进程 → `ClientSession` 包一层 → `session.initialize()` 协议握手 →
   `list_tools()` 把工具清单登记进 `self.tools`。

两个"黑话"级的细节（记下来面试用）：
- **命令里写的是 `"python3"`，但真正启动用的是 `sys.executable`**（`mcp_client.py:42-45`）：
  代码故意把 `cmd[0]` 丢掉、换成"当前解释器"——因为你可能跑在 venv 里，
  系统 `python3` 没有装 mcp 依赖，而 `sys.executable` 才是你 venv 里那个 python。
- **给子进程注入 `PYTHONPATH=项目根`**（`mcp_client.py:45`）：server 脚本里
  `import src.*` 才能解析到——子进程的工作目录不一定是项目根。
- `_hold` 列表（`mcp_client.py:29,62`）专门"攥住"这些 `stdio_client`/`ClientSession`
  的上下文管理器——官方 SDK 的异步 CM 若不持有引用，可能被 Python GC 提前回收，
  连接莫名其妙断掉。这是异步生态的经典坑，代码注释里写明了。

关闭钩子（`app.py:56-57`）调 `get_mcp().shutdown()`（`mcp_client.py:93-102`）：
逐个 `session.__aexit__` 优雅关闭并清空注册表。为什么关闭也重要？
MCP 子进程是**独立进程**，主进程退出前不打招呼，它们会变孤儿进程一直挂着。

> 注意一个文档与代码的漂移（诚实点出）：`init_mcp` 的 docstring 自称
> "幂等：重复调用只补连新 Server"（`mcp_client.py:111`），但实现是每次调用都
> `connect_all` 全部重连（`mcp_client.py:115`），老会话会留在 `_hold` 里没人关。
> 实际代码只在 lifespan 里调用一次，所以没出过事——这种"注释比代码乐观"的地方，
> 面试主动说出来是加分项。协议握手的细节留给 11 步，这里只记住结论：
> **MCP 子进程的生命周期 == lifespan 钩子**。

## 四、一次请求进来，依次过什么（必背小流程图）

```
浏览器 / curl
   │  HTTP GET /api/sessions  （带 Authorization: Bearer <token>）
   ▼
① uvicorn（ASGI 服务器）   —— 解析 HTTP，交给 FastAPI app
② CORSMiddleware           —— 浏览器跨域检查（app.py:64-70）
③ FastAPI 路由表           —— 按【注册顺序】逐个匹配（★ 下面细讲）
     命中 /api/sessions → Depends(get_current_user) 验 token → 401/403 关卡
     → sessions_list() 执行 → return JSON
④ 若没命中任何路由 → 落到最后挂载的 StaticFiles（web/dist）→ 静态文件
```

接口匹配是 **Starlette 按注册顺序从上到下找**的，这是本文件最关键的一条规则。
`app.py` 里注册顺序是这样的（从上往下数）：

1. `@app.post("/chat")` 等**无前缀**端点（app.py:92 起，按书写顺序注册）；
2. `app.include_router(_api)`（`app.py:412`）——把 `/api` 前缀的**同一批 handler**
   再注册一遍（下面细讲）；
3. **最后**才 `app.mount("/", StaticFiles(...))`（`app.py:418-419`）。

为什么静态文件必须最后挂？因为 `mount("/")` 是**兜底接盘侠**：它匹配一切路径。
如果把它放最前面，`GET /api/sessions` 会被它先接走——StaticFiles 去磁盘上找
`api/sessions` 这个文件，找不到就 404，你的接口就全"消失"了。注释写得很直白
（`app.py:415`）：「新前端（web/dist 构建产物）：必须最后挂载，避免吞掉 /chat /api 等接口」。

### 为什么要注册两遍路由？/api 兼容层（app.py:389-412）

```python
# ── /api 前缀兼容：web/dist 生产前端请求 /api/xxx（vite 开发代理剥前缀后也是后端无前缀路由）──
# 与上方无前缀路由共享同一组 handler，仅路径不同
# ⚠️ 必须注册在静态 mount 之前（Starlette 按注册顺序匹配）
from fastapi import APIRouter

_api = APIRouter(prefix="/api")
_api.post("/chat")(chat)          # 把已经定义好的 chat 函数再挂到 /api/chat
_api.get("/history")(get_history)
...
app.include_router(_api)          # 412：全部登记进 app
```

两个前端世界（13 步细讲）各走一条路，后端用"注册两遍"一网打尽：

| 场景 | 前端请求 | 后端命中 |
|---|---|---|
| 开发（Vite dev，5173） | `/api/chat/stream` | vite 代理**剥掉 `/api`** 转发到 8005（`web/vite.config.js:10-15` 的 `rewrite`）→ 命中无前缀路由 |
| 生产（同源托管 dist） | `/api/chat/stream` | 直接命中 `/api` 前缀路由（`app.py:404`） |

看 `web/src/api.js:13`：前端所有请求的 BASE 都是 `'/api'`，**一套代码两个环境通用**，
代价就是后端每个接口"双份登记"。技术上是"APIRouter 复用已有函数对象再注册一次"——
注意这里不是重复定义 handler，而是同一个函数挂两个路径，所以业务逻辑只有一份。

### 静态托管与 placeholder（app.py:415-423）

```python
_DIST = Path(__file__).parent / "web" / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="web")
else:
    @app.get("/", response_class=HTMLResponse)
    def index_placeholder():
        return "前端未构建：请先 cd web && npm run build（或开发模式 npm run dev → http://localhost:5173）"
```

- `html=True`：访问 `/` 自动找 `index.html`，访问 `/assets/xxx.js` 直接吐文件；
- dist **不存在**时（比如只 clone 了仓库没构建前端），服务照常能起，根路径返回一行
  提示——**不会因为缺前端就拒绝启动后端**，这个降级设计很实用（CI 里只测后端时也靠它）。

## 五、CORS：一行中间件，全开（app.py:63-70）

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应收紧为具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

- CORS 是**浏览器**的同源策略：8080 的前端页面 fetch 8005 的接口，浏览器先发
  `OPTIONS` 预检，服务器用这些响应头告诉浏览器"这个来源可以调"。
- `allow_origins=["*"]` = 任何网站都能调这个后端——注释自己承认"生产应收紧"。
- 诚实取舍：本项目前端 Vite 代理已规避 CORS，这个全开中间件更多是**演示/直连兜底**。
  真正的生产姿势是列白名单域名。另外注意：CORS ≠ 防 CSRF——本项目身份用
  `Authorization` 头而非 Cookie，所以没有 Cookie 可被"借用"，CORS 全开的实际风险
  主要剩"其他网站恶意调用你的公开接口"。

## 六、哪些接口公开、哪些要验 token（app.py:72-76）

```python
# 用户身份：一律来自 JWT（Authorization: Bearer <token>）
# 2026-09：去掉 X-User-Id / guest 回退（防任意冒充），无 token 一律 401。
# /auth/*、/healthz 与静态页保持公开；/dev/login 仅 DEV_MODE=1 注册（本地演示用）。
```

权限地图一句话：**公开的只有注册/登录、健康检查、静态资源；其余全部
`Depends(get_current_user)` 或 `Depends(require_admin)`**（02 步细讲）。
- `/healthz` 是例外中的例外，连登录都不要——Docker 健康检查没法带 token
  （`app.py:303-310`）：

```python
@app.get("/healthz")
def healthz():
    from src.task_queue import get_extraction_queue
    return {"status": "ok", "queue": get_extraction_queue().metrics()}
```

`metrics()`（`src/task_queue.py:104-113`）返回四项计数：
`queued / submitted / processed / dropped`。task_queue 是一个**有界线程任务队列**
（默认 2 个 worker、上限 200 条、满了直接丢弃并告警，`task_queue.py:42-47,83-102`），
docstring 说明它替代了早期"每请求裸开 threading.Thread"的写法。
诚实指出一个现状：**当前生产代码路径里没有 submit() 调用点**——记忆提取走的是
`asyncio.create_task`（见 `app.py:137`，全链路 async 后不再需要线程），
healthz 首次被访问时才会懒创建这个队列（`task_queue.py:137-149`）。
所以它是"保留的线程型任务逃生口 + 可观测面"，目前四项计数基本是 0——
能把这个"设计意图 vs 实际使用"讲清楚，比背概念更显你真的读过代码。

- `/dev/login`（`app.py:214-227`）：仅当 `DEV_MODE == "1"`（`app.py:41`）才注册。
  它是个 **mock 登录**：传 `role` 和可选的 `user_id`，后端直接签一个任意身份的 JWT
  （`app.py:222-226`，还过一遍 `is_valid_user_id` 字符集校验）。给本地演示/测试用的
  后门，**生产环境 DEV_MODE 没设就不存在这个路由**——比"代码里留着开关"更安全
  （路由层面消失，而不是 403）。`/me`（`app.py:202-211`）是登录态探测：无 token 401，
  前端启动时调它决定跳不跳登录页（`web/src/App.jsx:76-81`）。

## 七、把三段串起来：`python app.py` 到底发生了什么

```
1. import app.py 各模块
   → 建 FastAPI app、建 agent_graph、注册全部路由（含 /api 双份）、挂 StaticFiles
2. uvicorn 启动
   → lifespan 启动钩子：拉起 3 个 MCP 子进程（product/order/refund）+ 协议握手
   → 开始监听 0.0.0.0:8005
3. 请求进来（以聊天为例）
   → CORS → 路由表（命中 /chat/stream）→ 验 token → _resolve_session 校验会话归属
   → 跑 agent 图 → SSE 逐字推回（06 步全讲）
4. Ctrl+C 退出
   → lifespan 关闭钩子：MCP 所有会话优雅关闭 → 进程结束
```

---

## Q&A

**Q1：为什么用 lifespan 钩子连 MCP，而不是模块顶层直接连？**
顶层 `await` 是不合法的（不在事件循环里），即使勉强在 `asyncio.run` 里连了，
时机也在"接客之前"很远的地方，且**没有任何机制保证进程退出时关闭子进程**。
lifespan 恰好提供"首个请求前 / 最后一个请求后"两个精确时机，还和 uvicorn 的
生命周期绑定（FastAPI 文档的标准做法）。类比：开店准备和打烊清理本该是餐厅老板的
固定流程，而不是每次来客人临时拉厨师。

**Q2：为什么要按"先接口、后静态"的顺序注册？如果反过来会怎样？**
Starlette 按注册顺序逐个匹配路由；`mount("/")` 会匹配一切路径，是兜底。
反过来注册的话，`/api/sessions`、`/chat/stream` 全被 StaticFiles 接走，
去磁盘找同名"文件"，找不到就 404——**所有接口凭空消失**，只剩静态页能开。
这就是注释"必须注册在静态 mount 之前"（`app.py:391,415`）的含义。顺带记住：
`/api` 双份注册也是为了这套顺序服务的——生产前端从同源拿静态页 + `/api` 接口，
两者互不干扰的前提就是静态永远排最后。

**Q3：为什么同一批接口要注册两遍（无前缀 + /api 前缀）？值不值？**
开发环境：Vite 代理把 `/api` 剥掉再转给后端（`vite.config.js:14`），后端只需无前缀路由。
生产环境：前端构建产物和后端同源托管，请求保留 `/api`，需要前缀路由。
"注册两遍"让**前端 api.js 只用一套 `/api` BASE**，不用感知环境差异——这是用后端
"双份注册"换前端"零分支"。代价：路由表翻倍、新增接口容易漏注册 `/api` 版
（这是真实维护坑，代码里用"与上方共享同一组 handler"的注释提醒后人）。
如果想更干净，可以统一只留 `/api` 前缀 + 让 vite 代理不剥前缀，但那是重构题。

**Q4：`allow_origins=["*"]` 加 `allow_credentials=True` 是全开吗？生产怎么办？**
在浏览器规范里，带凭证（Cookie/Authorization 非默认）时服务器不能回 `*`，
Starlette 在这种情况下会回显请求的 Origin 头——效果上仍是"谁都能调"。
本项目前端走 Vite 代理和同源托管，CORS 基本用不上，全开只是兜底；且鉴权走
Bearer 头不走 Cookie，天然没有 CSRF 借 Cookie 的面。生产的正确姿势：
`allow_origins=["https://你的域名"]`。能讲出"CORS 管浏览器不管 curl、白名单要配域名"，
面试官就知道你真懂而不是背参数。

**Q5：MCP 子进程用 stdio 通信，为什么关闭钩子那么重要？**
stdio 连接 = 主进程和子进程之间两根管道。主进程退出而子进程不知道，
子进程会**变孤儿**（`python3 src/mcp_servers/product_server.py` 常驻等待 stdin），
反复重启会积累一堆僵尸进程。`shutdown()`（`mcp_client.py:93-102`）逐个
`__aexit__` 让 SDK 走完关闭握手并回收资源。这也是"生命周期 == lifespan"这一
心智模型的落点：**启动和关闭是对称的两个钩子，只做一半就会漏资源**。

**Q6（追问）：`init_mcp` 的 docstring 说"幂等只补连新 Server"，代码却每次全量重连，矛盾吗？**
矛盾。docstring（`mcp_client.py:111`）与实现（`mcp_client.py:113-115`：
`connect_all` 无条件遍历全部 server）不符——重复调用会把同名 server 的
`ClientSession` 覆盖掉，旧会话留在 `_hold` 里没人关闭，等于泄漏。
实际只有一个调用点（lifespan），所以从未触发。面试主动点出这种"注释与实现漂移"
并给出修法（先检查 `self._sessions` 是否已存在该名字再连），是"真读过代码"的铁证。

**Q7（追问）：既然全链路 async，为什么 task_queue 还在，healthz 里 metrics 有什么意义？**
task_queue 的文档写它替代了早期每请求开线程的写法；现在记忆提取等后台任务已改走
`asyncio.create_task`（`app.py:137`），线程队列只剩 `/healthz` 触发的懒实例化。
它的真实价值变成：(1) 给"未来可能要跑的 CPU/阻塞型后台任务"留一个**有界、会丢弃、
带计数**的线程逃生口（async 里跑 CPU 密集任务会卡死事件循环）；(2) `/healthz` 的
`queue` 字段是一个观测面。诚实版本：metrics 目前基本恒 0，讲成"已上线观测设施"
是过度包装；讲成"为线程型任务预留的接缝 + 健康检查字段"才站得住。

**Q8（追问）：dist 不存在时服务能正常启动吗？为什么这样设计？**
能。`app.py:418-423`：`if _DIST.exists()` 才挂 StaticFiles，否则注册一个根路径
placeholder 返回提示文本。设计意图：**后端不依赖前端产物**——CI 只测后端、
本地只想调 API、或者 docker 里分开构建时，服务都能起来。前端没构建只是"没页面"，
不是"起不来"，这正是分层该有的样子（app.py 只负责 HTTP，不负责前端构建产物存在与否）。
