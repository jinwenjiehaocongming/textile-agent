# 15 · 日志与 LangSmith 追踪：三层观测，先看日志、再开 trace

> 目标：讲清这套系统的"观测体系"——出了问题你靠什么看到现场。
> 一句话版本：**日志回答「发生了什么」，LangSmith 回答「这一次请求内部怎么走的」，
> /healthz 回答「服务还活着吗、队列堵没堵」。三者粒度不同，互为补充，不是二选一。**

---

## 一、先建立心智模型：一套观测体系，三层各管一段

面试最容易把"日志"和"追踪"混为一谈。这套项目把观测拆成三层（README.md:127-134 原文：
"日志（logging）与追踪（LangSmith）互补，一个回答「发生了什么」，一个回答「这次请求怎么走的」"）：

| 层 | 回答的问题 | 粒度 | 载体 | 谁看 | 生命周期 |
|---|---|---|---|---|---|
| 进程级日志 | 程序状态、错误、警告（发生了什么） | 进程/模块级，跨请求 | `logs/app.log` + stderr | 后端开发者，事后翻文件/grep | 进程活着就一直在写，滚动保留 |
| 请求级追踪 | 这一次请求的执行树、LLM prompt/返回、token、耗时 | 单请求级，一请求一棵树 | LangSmith 网页（第三方云） | 后端开发者，调试"某次回复为什么不对" | 每次请求一条，网页上按时间查 |
| 运行态观测 | 服务活着吗、任务队列有没有堵 | 进程级瞬时快照 | `GET /healthz` JSON | 运维 / Docker healthcheck | 探活周期调用，不落盘 |

三个记忆锚点：
- **日志是"流水账"**：只告诉你哪条消息被路由到了哪个 Agent、哪次 LLM 调用重试了——但不知道这些事
  是不是**同一次请求**干的（没有 request_id 串起来）。
- **trace 是"本次请求的档案"**：一棵树从上到下看到「改词 → 检索 → 路由 → Agent → 审核」每一步的
  输入输出和耗时。
- **/healthz 是"心跳仪"**：它不关心某次请求好坏，只回答"进程还响应吗、后台队列还健康吗"。

> 类比：日志像店铺门口的监控录像（一直在录、能回放当时发生了什么）；trace 像"这一单从顾客进门到出门"的
> 完整导购记录（每个动作几秒、推荐了什么、顾客回什么）；healthz 像心跳监护仪（只看还跳不跳）。

---

## 二、日志配置逐条拆（`src/logging_config.py` 全文只有 67 行）

这是本步的核心文件，建议打开全文对照。先看它自己的模块注释（`src/logging_config.py:1-12`）就说了三件事：
输出到 stderr（不污染 stdout，因为 **MCP Server 的 stdout 是 JSON-RPC 协议通道**）；
同时写 `logs/app.log`（滚动 5MB×3）；各模块 `get_logger(__name__)` 拿 logger，**模块导入即自动配置（幂等）**。

### 1. 路径与格式串

```python
LOG_DIR = Path(__file__).parent.parent / "logs"      # logging_config.py:19 → 项目根/logs
_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"   # :20
```

- `LOG_DIR` 是从**配置文件自身位置**向上两级算的（`src/logging_config.py` → `src/` → 项目根），
  不管从哪个目录启动都能找到 `logs/`。Docker 里没把 logs 挂成卷（docker-compose.yml:29-31 只挂了
  `./index` 和 HF 缓存）——**容器里 app.log 随容器一起消失**，容器场景真正可靠的日志通道是 stderr → `docker compose logs`。
- 格式串四个字段：时间 / 级别 / logger 名 / 消息。`%-7s` 是**左对齐占 7 字符**，所以日志里
  `INFO` 后面跟 4 个空格、`ERROR` 后跟 2 个空格，肉眼对齐很好看。真实样例（logs/app.log 第 1 行）：

  ```
  19:21:13 | INFO    | src.memory | Layer 1 热缓存: Redis
  ```

### 2. 级别设置与过滤（`src/logging_config.py:29-45`）

```python
def setup_logging(level: int = None) -> None:
    global _configured
    if _configured:              # 幂等：无论被 import 多少次只配置一次
        return
    _configured = True
    # 级别优先级：显式参数 > 环境变量 LOG_LEVEL > 默认 INFO
    if level is None:
        level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)   # 非法值安静回退 INFO
    root = logging.getLogger()
    root.setLevel(level)
    # 降噪：第三方库只记录 WARNING 以上，避免日志被 HTTP 请求/模型加载刷屏
    for noisy in ("httpx", "httpcore", "urllib3", "chromadb", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
```

要点：
- **默认 INFO**；调法两种：`LOG_LEVEL=DEBUG python src/agent.py`（README.md:141 原文），或在 `.env` 里加。
- 级别是**设在 root logger 上**（`root.setLevel`），子 logger 全继承。
- **降噪名单是硬编码 5 家**（httpx/httpcore/urllib3/chromadb/huggingface_hub）——注意名单里**没有**
  `sentence_transformers` 和 `sqlalchemy`，真实日志里就能看到它们照样刷：`INFO | sentence_transformers... | Load pretrained SentenceTransformer`（logs/app.log:3）、
  `ERROR | sqlalchemy.pool.impl... | Exception closing connection`（logs/app.log:5836）。面试可以主动说：
  "降噪靠维护名单，属于'知道哪几家吵'的土办法，标准做法是 `logging.config.dictConfig` + 按库分组配置"。

### 3. 两个 handler：stderr + 滚动文件

```python
# 1. 控制台 → stderr（logging_config.py:49-52）
sh = logging.StreamHandler(sys.stderr)
# 2. 文件 → logs/app.log（滚动，:54-63）
LOG_DIR.mkdir(parents=True, exist_ok=True)
fh = RotatingFileHandler(
    LOG_DIR / "app.log",
    maxBytes=5 * 1024 * 1024,    # 5MB
    backupCount=3,               # 3 个备份
    encoding="utf-8",            # 中文日志不乱码
)
```

- **为什么 stderr 而不是 stdout**：模块注释说透了（`logging_config.py:4`）——MCP 工具服务器子进程的
  **stdout 是 JSON-RPC 协议通道**（`mcp_client.py:42-50` 用 `stdio_client` 从子进程 stdout 读协议），
  任何日志混进 stdout 都会把协议包搞坏。这是"日志不能随便 print 到 stdout"的硬理由；stderr 想怎么打怎么打。
  另外 `app.py:432` 把 uvicorn 自己的日志压到 `log_level="warning"`（access log 也不输出）——业务日志
  走 logging_config，HTTP 访问日志不落盘，日志文件很干净。
- **轮转**：单文件超 5MB → 改名 `app.log.1`、旧的依次后移、最多留 3 个备份。磁盘上限 ≈ 4×5MB = 20MB，
  日志量有天花板，不会无限膨胀。当前真实文件 512KB、5969 行（logs/app.log），**远没触发过轮转**，
  所以 `logs/` 里只有 `app.log` 没有 `.1/.2/.3`。
- `logs/` 整个目录被 `.gitignore:5` 忽略、`.env` 被 `.gitignore:1` 忽略——日志和密钥都不会进 git。

### 4. logger 命名与"模块导入即配置"

```python
def get_logger(name: str) -> logging.Logger:   # :24-26
    return logging.getLogger(name)

setup_logging()                                # :66-67 文件末尾直接调用
```

- 各模块写 `logger = get_logger(__name__)`，`__name__` 是 `src.agent`、`src.retrieval` 这种完整路径名，
  所以日志天然带"这是哪个模块打的"标签，grep 特别好用。
- **配置在 import 时触发**：谁第一个 `from src.logging_config import ...`，谁就顺手把整个配置跑一遍；
  `_configured` 保证只跑一次。副作用是**测试也会写生产日志文件**——真实 logs/app.log:19-20 就有
  pytest 跑出来的 `[任务队列] test-b 队列已满` 和 `RuntimeError: boom` 堆栈。
- 顺带一提：`src/task_queue.py:20` 用的是原生 `logging.getLogger(__name__)` 而不是 `get_logger`——
  也能正常工作，因为 handler 挂在 **root** 上，任何 logger 最终都 propagate 到 root（没人设 `propagate=False`，
  所以也**不存在重复日志问题**）。
- **一个细节坑**：`LOG_LEVEL` 是在首次 import 的瞬间读 env 的（:35-38）。能生效是因为 import 链上有顺序
  保证：`src/retrieval.py:13-14` 先 `load_dotenv()` 再 `:25` import logging_config，而标准入口
  （`app.py:21` import agent → agent.py:44 import retrieval）都会先走到 retrieval 这步，所以 `.env` 里的
  `LOG_LEVEL` 在常规启动路径上能被读到。反过来，如果谁绕过 retrieval 第一个 import logging_config，
  `.env` 的级别就丢了（进程级配置"首次 import 定终身"）。

---

## 三、使用面与排查示例：9 个模块在打日志

`grep -rn "get_logger" src/` 统计：9 个业务模块接入了统一日志（agent.py:54、order_agent.py:16、
after_sales_agent.py:16、retrieval.py:54、vector_store.py:23、memory.py:25、mcp_client.py:21、
llm_utils.py:18、mcp_servers/order_server.py:14），外加 task_queue.py 用原生 logging。HTTP 层 app.py 和
SSE 层 stream_chat.py **不用 get_logger**——业务错误走 HTTPException 翻译成状态码返回给前端，不留 ERROR 日志。

各模块日志风格一览（真实来源行号）：

| 模块（logger 名） | 代表性日志 | 源码位置 |
|---|---|---|
| `src.agent`（心脏） | `[检索词] 原文: ...` / `[Supervisor] → 售前 Agent` / `[审核] 通过` / `[审核] 拦截: ...` | agent.py:203, 463, 346, 342 |
| `src.llm_utils` | `LLM 异步调用失败，1s 后重试: ...`（重试/降级/流式中断全在这） | llm_utils.py:30, 48, 95 |
| `src.mcp_client` | `[MCP] product 已连接 (1 个工具)` / `[MCP] 所有连接已关闭` | mcp_client.py:63, 102 |
| `src.retrieval` | `[检索] [hybrid] 查询: ... → N 条结果` | retrieval.py:247 |
| `src.vector_store` | `[Qdrant] LocalMode（本地存储）: ...` | vector_store.py:48 |
| `src.memory` | `Layer 1 热缓存: Redis` / `[记忆] 提取偏好: ...` / `[记忆] 非法 user_id 已降级为 'guest': ...` | memory.py:41, 178, 201 |
| `src.task_queue` | `[任务队列] 任务执行失败`（带堆栈）/ `队列已满，任务丢弃` | task_queue.py:77, 96 |
| `src.mcp_servers.order_server` | `create_order 失败: ...`（子进程内异常也留痕） | order_server.py:70 |

### 排查流程示例（README.md:142 原话就是 grep）

```bash
# 某客户说"我明明是来退货的，怎么回我面料知识" → 看路由到哪了
grep "Supervisor" logs/app.log
23:13:06 | INFO    | src.agent | [Supervisor] → 售前 Agent          # 真实行（logs/app.log:551）
23:16:17 | INFO    | src.agent | [Supervisor] → 售后 Agent          # 这行才是售后
# 出错了先看有没有 ERROR / WARNING：
grep -E "WARNING|ERROR" logs/app.log
19:21:18 | ERROR   | src.mcp_client | [MCP] product 重启失败: MCP 子进程返回异常: '' (Expecting value: line 1 column 1 (char 0))   # 真实行（logs/app.log:16）
19:21:19 | ERROR   | src.task_queue | [任务队列] 任务执行失败        # 真实行（logs/app.log:20，下面跟堆栈）
```

把真实日志翻译成人话：
- `[Supervisor] → 售后 Agent` = Supervisor 用便宜 LLM 判完意图，把这次会话路由给了售后分支；
  如果你发现用户问退货却走了售前，问题就锁定在 **Supervisor 路由那一次 LLM 调用**。
- `[MCP] product 重启失败: ... (Expecting value: line 1 column 1 (char 0))` = product 工具服务器的
  子进程启动时 stdout 没吐合法 JSON-RPC（`Expecting value` 是 json 解析报错），说明子进程启动阶段崩了。
- `[记忆] 非法 user_id 已降级为 'guest': '../../etc/passwd'`（logs/app.log:18 的真实安全日志）= 有人拿
  `../../etc/passwd` 路径穿越 payload 当 user_id，被 `user_identity` 拦下并降级成 guest——**这种日志是安全事件的第一手证据**。
- 真实日志里还能看到用户消息原文被记下：`[检索词] 原文: 13857577360，钱塘路1102，10天`（logs/app.log:5784）——
  手机号+地址都在，第 7 节专讲。

---

## 四、LangSmith：全自动为主，手动标注已经删干净了

### 1. 开关三件套（`.env.example:16-19`）

```
# 可选：LangSmith 追踪
LANGSMITH_TRACING=false     # 总开关：false = 不开
LANGSMITH_API_KEY=          # 你的 smith.langchain.com key
LANGSMITH_PROJECT=          # 项目名（网页上按项目分组看）
```

关键事实：**代码里没有一行 Python 直接读 `LANGSMITH_*`**（全仓 grep 只命中 `.env.example`、
README、docs/DEPLOY.md，`src/` 里一个都没有）。这三个变量是 **LangChain 官方集成自动读的**：
`requirements.txt:10-12` 锁了 `langchain-openai==0.3.35` / `langchain-core==0.3.86` / `langsmith==0.4.37`，
`ChatOpenAI` 的每次 `invoke/ainvoke/stream/astream`（如 agent.py:136 审核调用、llm_utils.py:45 的统一入口）
都会走 LangChain callback，`langsmith` 客户端在调用瞬间检查 `LANGSMITH_TRACING`，为 true 就把 run
（含 prompt、completion、token 数、耗时）上报到 smith.langchain.com。**开与关在 SDK 内部，代码零改动。**

### 2. ⚠️ 文档与代码不一致（面试主动指出是加分项）

README.md:146 写着：`review_response`、`retrieve` 用 `@traceable` 手动标注——**但当前代码里
`grep -rn "traceable|langsmith" src/` 是零匹配，没有任何 @traceable**。查 git 历史能还原真相：

- 旧版（03b95cb 时代）确实标过：`agent.py` 有 `@traceable(run_type="chain", name="review_response")`
  装饰在审核函数上；`retrieval.py` 有 `@traceable(run_type="retriever", name="hybrid_retrieve")`
  装饰在 `HybridRetriever.retrieve` 上。
- 提交 **7106bec**（"企业级演进——PostgreSQL + Qdrant + 官方 MCP SDK + 全链路异步"）把
  `from langsmith import traceable` 连装饰器一起删了。当前 `review_response`（agent.py:124）和
  `context_retriever`（agent.py:221）都是裸 async 函数，只剩日志。
- 结论：**README 的追踪章节没跟上代码演进，是文档债**。面试被问"哪些函数手动标了 traceable"，
  正确答法是："设计上曾对 `review_response` 和 `retrieve` 手动标注过（README 还这么写），
  但 7106bec 异步化重构时删掉了，现在靠 LangChain 自动上报"。能指出这个不一致，比背 README 可信得多。

### 3. 手动标注本来想解决什么问题 + 成本

那为什么当初要手动标这两个函数？因为 **LangChain 只自动上报"LLM 调用"和"LangGraph 图执行"**，
而上报树里缺少"不带 LLM 但很关键"的环节：
- `retrieve`（混合检索）本身**不调 LLM**（Qdrant + BM25 + RRF 全本地），不手动标，trace 树里看不到
  "这次答错了是不是检索没召回"，只能从 `[检索] 命中 N 条` 日志里猜——这正是"非 LLM 节点值得标"的理由；
- `review_response` 是"规则拦截 + LLM 审查"，规则拦截命中时同样没有 LLM 调用可上报，不手动标就看不到
  "为什么这条回复被拦/被放行"。

成本与使用建议：开 trace 后**每次请求的完整 LLM 消息体都出网到第三方云**（详见第 7 节敏感信息），
且有 API 配额与费用；`docs/DEPLOY.md:58` 明确建议**演示期 `LANGSMITH_TRACING=false`**，`.env.example` 默认
也是 false——**默认关、按需开**。真需要看"无 LLM 节点"细节时，再考虑用 `@traceable` 手动补标注
（一个装饰器，零成本），或直接看日志也够。

---

## 五、分工与排查流程：什么时候看日志、什么时候开 trace

README.md:148 给了官方流程，翻译成排查话术：

| 现象 | 先做什么 | 再做什么 |
|---|---|---|
| 服务挂了 / 疯狂报错 / 没反应 | 看日志：`grep -E "WARNING\|ERROR" logs/app.log`（或 `docker compose logs -f app`） | 定位到模块级现场（哪次 LLM 重试、哪个 MCP 崩了） |
| 单次回复不对 / 答非所问 / 超时 | 看日志定位"卡在哪个节点"：`grep "Supervisor"`、`grep "检索"`、`grep "审核"` | **开 trace**：`.env` 设 `LANGSMITH_TRACING=true`，复现一次，去 smith.langchain.com 点开该请求的执行树，看那一步 LLM 的 prompt 和工具返回 |
| 想知道服务健不健康 | `curl http://127.0.0.1:8005/healthz` | 看 queue 计数是否爆（见第 6 节） |

一段"排查话术"（背下来能直接用）：

> 先看日志把问题**缩小到一个节点**——比如 `grep "Supervisor"` 发现用户问退货被路由到售前 Agent，
> 那问题大概率在路由那次 LLM 判断上；光有日志只能看到"它去了售前"，看不到"LLM 为什么判售前"。
> 这时开 trace（`LANGSMITH_TRACING=true`），复现同一条消息，LangSmith 网页上能看到 Supervisor 调用的
> 完整 prompt——是不是历史消息串场了、是不是改写后的 query 丢了关键信息。定位到原因后立刻关掉，避免请求体持续出网。

**为什么顺序不能反**：日志零成本、常开、按模块过滤；trace 有成本（出网 + 配额）且按请求留存。
先日志是"大海捞针先缩小到一平方厘米"，再 trace 是"对着一平方厘米用显微镜"——反过来会被 trace 树淹死。

---

## 六、/healthz 与队列指标：运行态观测层

### 1. 端点本体（app.py:303-310，全文就 8 行）

```python
@app.get("/healthz")
def healthz():
    """存活探测 + 轻量运行状态（Docker 健康检查 / 观测用）。"""
    from src.task_queue import get_extraction_queue
    return {
        "status": "ok",
        "queue": get_extraction_queue().metrics(),
    }
```

- 是**同步 def、不查数据库、不查 Qdrant**——只证明"进程活着 + 能取到队列计数"。响应长这样
  （队列刚初始化时）：

  ```json
  {"status": "ok", "queue": {"name": "mem-extract", "queued": 0, "submitted": 0, "processed": 0, "dropped": 0}}
  ```

- 无鉴权、静态页一样公开（app.py:76），健康检查器访问不需要 token；`/api/healthz` 兼容别名挂同一 handler（app.py:403）。

### 2. 队列指标从哪来（`src/task_queue.py:104-113`）

```python
def metrics(self) -> dict:
    with self._lock:
        return {
            "name": self._name,        # 队列名（mem-extract）
            "queued": self._q.qsize(),       # 此刻排队中的任务数
            "submitted": self._submitted,    # 累计提交总数
            "processed": self._processed,    # 累计处理完成数
            "dropped": self._dropped,        # 累计因队列满丢弃数
        }
```

四个计数器在 worker 线程和提交线程间用 `threading.Lock` 保护（task_queue.py:54），所以多线程读不会错。
语义自明：`dropped > 0` 说明**队列满过、有任务被丢**（记忆提取属"尽力而为可丢"，模块注释 task_queue.py:7 原话）；
`queued` 长期接近 200（maxsize，task_queue.py:146）说明后台任务积压。

### 3. 它在 Docker 里怎么用（docker-compose.yml:33-38）

```yaml
healthcheck:
  test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8005/healthz', timeout=3)"]
  interval: 30s
  timeout: 5s
  start_period: 240s     # 首次启动要下模型/建索引，给 4 分钟宽限
  retries: 5
```

每 30s 探测一次，连续 5 次失败容器标记 unhealthy，配合 `restart: unless-stopped` 自动重启。
**下游依赖（PG/Qdrant）的健康由各自容器自己的 healthcheck 守**（compose 里 postgres 用 `pg_isready`，
qdrant 只等 `service_started`），app 与依赖之间的就绪顺序由 `depends_on: condition` 保证——分工明确。

### 4. 诚实吐槽（面试主动说的取舍）

两点必须心里有数：
1. **/healthz 不探下游**：PG 挂了但 Python 进程还活着时，/healthz 照样返回 ok，容器不会被重启，
   直到真实请求报错。生产标准做法是 liveness（只探活）与 readiness（查下游依赖）分开；
   本项目把两者合并成一个轻量探活，属演示量级取舍。
2. **队列指标实际长期为 0**：task_queue.py 文档说它替代"每请求裸开 threading.Thread"、是给**记忆提取**
   用的——但当前代码里 `submit()` 的调用方只剩 `tests/test_task_queue.py`！真实路径（agent.py:639-640、
   stream_chat.py:184）用的是 **`asyncio.create_task(memory.extract_and_store(...))` 后台协程**，根本没走
   TaskQueue。全链路 async 化之后线程队列被架空，/healthz 的 queue 数字在生产流量下恒为 0（测试时会涨）——
   这是"演进后留下的文档/职责过时"，README 与模块注释都没同步，又一个"代码在跑但历史包袱还在"的点。

---

## 七、敏感信息风险点（结合代码诚实写）

这一节面试官很容易延伸追问，先把真实证据摆出来：

1. **用户消息原文会进日志（INFO 级，默认就开）**。`agent.py:203` 打 `[检索词] 原文: {last_msg}`，
   `agent.py:228` 打 `[检索] 查询: {query}`（rewrite_query 含用户原文拼改写结果）。真实日志里就有：
   `[检索词] 原文: 13857577360，钱塘路1102，10天`（logs/app.log:5784）——**手机号 + 收货地址明文落盘**。
   审核环节只打结论不打印全文（`[审核] 拦截: reason`，agent.py:342），但检索环节是整条原文。
2. **记忆提取的内容也进日志**：`memory.py:178` 打 `[记忆] 提取偏好: {result.splitlines()}`，而偏好提取
   prompt（memory.py:146-163）明确让 LLM 记录"客户主动给的电话、地址"——意味着联系方式经 LLM 提取后
   可能出现在日志行里（当前 logs/ 中该行不常见，但代码路径存在）。
3. **凭证类没有泄漏**：全仓搜索，日志调用里没有任何打印 headers/token/password 的语句；
   实测 `grep -cE "password|Bearer |JWT|authorization" logs/app.log` 为 0。密码只以 bcrypt 哈希存在
   users 表（第 02 步讲过 `to_public` 白名单保证哈希不出模块），日志侧是干净的。
4. **LangSmith 开了之后是另一个出口**：`LANGSMITH_TRACING=true` 时，ChatOpenAI 每次调用的**完整
   messages（用户输入、检索到的知识原文、订单信息、审核文案）都会上传到 smith.langchain.com 第三方云**。
   与本地 app.log 不同——app.log 在自己机器上，trace 是**出网**。所以两处风险要分开说：
   本地日志泄漏 = 谁拿到服务器文件谁看到 PII；trace 泄漏 = 第三方平台数据驻留 + 传输链路。

**生产环境注意项清单**（能说出来就是"懂工程"）：
- 用户原文落日志前做**截断/脱敏**（手机号、地址打码），或把检索词日志降到 DEBUG（DEBUG 不默认开，丢了可
  排查性换合规）；目前 INFO 即全量原文是演示取舍。
- `LANGSMITH_TRACING` **默认关**（.env.example:17 就是 false，docs/DEPLOY.md:58 建议演示期 false），
  上线前确认没开；真要开，评估第三方数据驻留是否符合客户数据合规要求，必要时上自托管 trace 后端。
- 日志文件做好轮转之外的**备份与清理策略**（当前 20MB 封顶自洁，但没归档）。
- /healthz 的队列指标在异步化后失真（第 6 节），监控别只信它一个信号。

---

## Q&A

**Q1：日志和 trace 都有，为什么不是二选一？**
粒度不同、覆盖不同：日志是进程级流水账，跨请求、常开、零成本，但**没有单请求上下文**（没有 request_id，
你只知道"某时刻路由到售前"，不知道是哪条消息触发的）；trace 是单请求执行树，能看 LLM prompt/token/耗时，
但**只覆盖"走了 LangChain/LangGraph"的部分**（且默认关、出网有成本）。交集是"LLM 调用"，差集才是价值：
日志独有的"模块级错误、重试、MCP 连接、安全降级"，trace 独有的"某请求每一步输入输出"（README.md:129
原话就是"互补"）。所以排查套路固定成"日志缩小范围 → trace 看细节"（第 5 节）。

**Q2：为什么轮转是 5MB×3，不是越大越好？**
5MB×3 意味着磁盘占用有 ~20MB 硬顶，且**日志体积小 = 单文件打开快、grep 快、随容器迁移轻**。
`maxBytes` 和 `backupCount` 就是两个旋钮（logging_config.py:58-59）：调大 = 保留现场更久但查找变慢；
调多备份 = 历史更长但要清理策略。真正的槽点是**格式串没有日期**——`datefmt="%H:%M:%S"`（logging_config.py:47）
只到时分秒，跨天/跨多次启动的日志（当前 app.log 里 19:21 和 18:10 两次启动混在一起）无法区分是哪天的，
轮转后 app.log.1 是"上一次"都只能靠猜。面试可以补一句"我会把 asctime 加 %Y-%m-%d"。

**Q3：LOG_LEVEL=WARNING 会把什么丢掉？**
丢掉所有 INFO（和 DEBUG）：Supervisor 路由去向（agent.py:463）、检索命中情况（agent.py:234）、审核通过
（agent.py:346）、MCP 连接事件（mcp_client.py:63）——这些正是排查"这条消息走了哪条路"的主力线索，
全没了只剩"出错了才说话"。DEBUG 更是细到工具入参出参（agent.py:326 `结果: {result[:120]}`）。
所以调 WARNING 适合"稳定期压噪音"，排查期要调回 DEBUG/INFO；而且级别是首次 import 定终身（第 2 节），
改 .env 要重启进程才生效。

**Q4：如果 LANGSMITH_TRACING 忘了关就上线，会有什么后果？**
所有 ChatOpenAI 调用（用户问题、改写后的检索词、检索命中的知识原文、审核文案、下单确认内容）持续上传到
smith.langchain.com：一是**用户 PII / 商业信息出网到第三方**，存在数据驻留与合规风险（客户发的手机号地址，
同款内容见 logs/app.log:5784）；二是每次调用附带收集上报开销，吃网络与 API 配额；三是 trace 数据会积累
完整对话档案，等于在第三方存了一份"客户档案副本"。所以 .env.example 默认 false、docs/DEPLOY.md:58 建议
演示期 false，上线 checklist 必须有一项"确认 LANGSMITH_TRACING=false 或走自托管"。

**Q5：日志打不打用户消息体？这合规吗？**
打，而且打的是**整条原文**（agent.py:203），真实日志里有完整手机号+地址（logs/app.log:5784）。
从"可排查性"看这是优点（复现检索问题必须有原文）；从合规看这是风险点——日志文件属于敏感数据载体，
要按 PII 同等对待（加密、限权、备份策略）。诚实结论：演示/本地量级这是合理的简化；生产至少该
**截断 + 脱敏**（手机号打码）或降到 DEBUG 级，并在 README/部署文档里写明这一取舍。面试官问"会不会把密码
打进日志"，答"密码/哈希/token 从不进日志——grep logs/app.log 里 password|Bearer 是 0 次，bcrypt 哈希只
存在数据库，且 to_public 白名单保证不出模块"。

**Q6：/healthz 探活为什么不查数据库？**
代码里 /healthz 只回 `{"status":"ok", queue:...}`（app.py:307-309），不查 PG/Qdrant。这是**故意的分层**：
a) 探活要**快且独立**——若探活去查下游，PG 抖动会误杀还健康的 app 容器（`restart: unless-stopped` 会反复重启）；
b) **下游健康由下游自己守**——PG 容器用 `pg_isready` 自检（docker-compose.yml:55），就绪顺序交给
`depends_on: condition: service_healthy`（docker-compose.yml:24-26）。诚实点："PG 挂了但 app 活着"时
/healthz 仍返回 ok，容器不会自动重启——生产可用 readiness 查依赖、liveness 只探活，把两个语义分开。

**Q7（追问）：那 `@traceable` 到底还在不在？README 不是说 review/retrieve 手动标了吗？**
不在——这是本项目文档与代码不一致的典型例子。README.md:146 的说法是历史版本（03b95cb）的事实：
当时 agent.py 的 `review_response` 挂着 `@traceable(run_type="chain", name="review_response")`、
retrieval.py 的 `HybridRetriever.retrieve` 挂着 `@traceable(run_type="retriever", name="hybrid_retrieve")`。
提交 7106bec（全链路异步化）把它们随 `from langsmith import traceable` 一起删了，README 没同步更新。
删掉后 LLM 调用仍靠 LangChain 自动上报，损失的只是"无 LLM 节点（检索、规则审核）进 trace 树"的可见性，
而那部分本来也能靠 `[检索] 命中 N 条`（agent.py:234）这类日志弥补。真到需要时，`@traceable` 一个装饰器
随时能加回来。能主动讲清"README 过时 + git 历史还原 + 删掉的动机"，是二面里比背文档高一个段位的回答。
