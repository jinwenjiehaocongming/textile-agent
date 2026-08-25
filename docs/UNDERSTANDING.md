# 读懂这个项目：改造后全讲解（从零看懂版）

> 目的：让你重新掌握这个项目。每处改动都按
> **原来 → 现在 → 为什么 → 面试怎么讲 → 怎么自测** 拆开讲。
> 看完本文 + 跑一遍「自查清单」，你就能从零给任何人讲清楚它。
> 配套：`PRODUCTION_MIGRATION_CHECKLIST.md`（改造明细）、`ARCHITECTURE.md`（最初架构）。

---

## 0. 一句话总览

一个**纺织 B2B 客服**：客户问价/知识 → 系统检索面料知识 + 查产品库 → 售前/下单/售后三个 Agent 分流应答 → 所有回复过安全审核。

改造后加了四个东西：**多人隔离**（原来所有人共用一个记忆）、**下单必须人工审批**（原来 AI 直接写订单）、**执行过程实时可见**（前端能看到"现在在检索/在调用工具"）、**自动评分评测**（LLM 当裁判给回答打分）。

---

## 1. 现在的完整流程（一张图）

```
客户消息（带请求头 X-User-Id 标识是谁）
  ↓
① 改写查询  →  ② 知识检索（向量+BM25+Rerank，命中5条知识）
  ↓
③ Supervisor 路由：售前 / 下单 / 售后
  ├─ 售前 Agent ⇄ 工具（search_product 查价）
  ├─ 下单 Agent ⇄ 工具（查产品→确认单→ create_order）
  │        └── ⛔ HITL：create_order 前 interrupt() 挂起，
  │              销售经理在 /approval/approve 审批通过后才会真的写库
  └─ 售后 Agent ⇄ 工具（查订单→建退款工单）
  ↓
④ 审核（规则拦截 + LLM 安全审查）→ 回复给客户
  ↑
（整条链路的每个节点，SSE 实时推给前端显示"执行步骤"）
```

三条竖线是这次改造的骨架：
- **电梯左侧**：`user_id` 贯穿所有请求（多用户隔离）
- **审批分支**：下单不再一步到底（HITL）
- **底部横线**：节点过程外露（流式可视化）

---

## 2. 七处改动逐一讲解

### 改动①　多用户隔离（P0-1）

| | 内容 |
|---|---|
| 原来 | `app.py` 写死 `USER_ID = "123456"`，**所有人共用一份聊天记忆** |
| 现在 | 每个请求从请求头 `X-User-Id` 取身份；校验规则 1-64 位字母数字下划线连字符，非法返回 400；缺省降级 `guest` |
| 为什么 | 多用户必须互不串历史/偏好/订单 |
| 涉及文件 | `src/user_identity.py`（校验规则，单一事实来源）、`app.py`（3 个接口按请求取记忆）、`web/src/api.js`（前端自动带身份头）、`src/memory.py`（get_user 兜底校验） |
| 面试怎么讲 | "原来用户 ID 写死导致所有人记忆串在一起；我抽了一个校验模块做单一来源，Web 层非法就 400，内存层再兜底，前端用 localStorage 持久化身份——两个用户并发对话，历史互不干扰。" |
| 怎么自测 | `curl -H "X-User-Id: a" .../history` 和 `-H "X-User-Id: b"` 结果互不影响；`-H "X-User-Id: ../.."` 返回 400 |

### 改动②　SQLite 并发兜底（P0-3）

| | 内容 |
|---|---|
| 原来 | 每个函数手写 `sqlite3.connect()` 手动 close；`create_order` 异常时连接**不关**（泄漏）；并发写会报 `database is locked` |
| 现在 | 统一走 `src/mcp_servers/sqlite_utils.py`：`WAL` 日志模式 + `busy_timeout=5000` + 上下文管理 `finally` 必关 |
| 为什么 | 多用户并发是早晚的事；WAL 允许读写不互斥 |
| 涉及文件 | `sqlite_utils.py`（新）、三个 MCP server、`src/memory.py` |
| 附带修复 | 订单号原来是"日期+秒"生成，**同一秒两单会撞唯一约束** → 加了微秒+随机后缀 |
| 面试怎么讲 | "三个工具 server 和记忆层统一到一个 SQLite 工具层：WAL 模式、busy_timeout、连接必关。我压测过 20 线程 400 并发写，0 失败 0 撞单。" |
| 怎么自测 | `python -m pytest tests/test_servers.py::test_concurrent_create_order` |

### 改动③　记忆提取改队列（P0-5）

| | 内容 |
|---|---|
| 原来 | 「提取用户偏好」这个后台任务每请求 `threading.Thread(...).start()`——线程数无上限，会打爆进程 |
| 现在 | `src/task_queue.py`：有界队列（200）+ 固定 2 个 worker 线程 + 计量（submitted/processed/dropped） |
| 为什么 | 请求延迟和后台任务解耦；线程受控；队列满时丢弃并告警（偏好提取是尽力而为，丢了不伤主流程） |
| 附带产出 | `GET /healthz` 接口返回队列状态（也是 Docker 健康检查的探针） |
| 面试怎么讲 | "原来用裸线程做异步提取，并发一高线程就失控。我换成了有界队列+固定 worker，满队列丢弃+告警；生产要跨进程扩缩容就换 Redis Stream，接口不变。" |

### 改动④　MCP 工具层线程安全（P0-2）

| | 内容 |
|---|---|
| 原来 | 全局单例 MCP Client 共享子进程 stdin/stdout，**并发时响应会串线**（A 的请求 B 收到结果）；子进程僵死会永久阻塞请求 |
| 现在 | `src/mcp_client.py`：① RLock 串行化请求（工具是毫秒级 SQLite，串行代价可忽略）② 响应超时（读线程+join，默认 30s）③ 子进程崩溃自动重启 + 重试一次 |
| 面试怎么讲 | "三个坑：串线、永久挂起、崩溃无自愈。锁串行 + 超时 + 自动重试，压测 20 线程 200 次调用无串线；kill 掉子进程后下一次调用自动恢复。" |

### 改动⑤　HITL 下单人工审批 ⭐面试最亮的点

| | 内容 |
|---|---|
| 原来 | 客户说"确认"，AI 直接调 `create_order` 写库 |
| 现在 | 下单 Agent 准备调 `create_order` 时，先 `interrupt()` 把图**挂起**，把订单草案登记到 `src/approval.py`；销售经理在 `/approval/approve`（或 reject）审批，通过后才真正写库 |
| 为什么 | 金融/订单类操作不该由 LLM 一步到位——这是 agent 领域最标准的 **Human-in-the-Loop** 模式 |
| 涉及文件 | `src/approval.py`（新）、`src/order_agent.py`（拦截 create_order）、`src/agent.py`（图编译带 checkpointer）、`app.py`（审批 3 个端点 + 挂起态守卫） |
| 两个关键技术点 | ① LangGraph `interrupt()` 需要 **checkpointer**（内存版 MemorySaver，thread_id=user_id）才能记住"挂在哪"；② 恢复用 `graph.invoke(Command(resume=...), config)` |
| 面试怎么讲 | "下单原来是 AI 直接写库，我加了人工审批：LLM 生成订单草案 → interrupt 挂起图 → 销售经理审批 → 通过才落库。客户在待审批期间再发消息会被守卫拦住提示'正在审批'，不会重复下单。" |
| 怎么自测 | 用浏览器发"订P0075 T400黑色200米12.7元，电话138，地址杭州，交期下周，确认下单" → 回复"已提交人工审批" → `curl /approval/pending` 能看到 → `curl -X POST /approval/approve -d '{"thread_id":"<你的用户id>"}'` → 订单落库 |

### 改动⑥　节点事件真流式 ⭐可视化亮点

| | 内容 |
|---|---|
| 原来 | 前端把结果"假装"打字机效果；用户看不到过程 |
| 现在 | 图执行时每个节点（改写查询/知识检索/意图路由/智能应答/工具执行/安全审核）经 SSE 实时推给前端，顶部一条"执行步骤"丝带逐条点亮（当前步骤脉冲、完成打✓） |
| 技术要点 | `graph.stream(stream_mode="updates")` 逐节点产出事件，放后台线程跑，经 asyncio.Queue 喂回 SSE |
| 踩过的坑 | 0.6.x 的 async `astream` + 同步 `interrupt()` 会抛错且 **checkpoint 不落盘**（导致审批无法恢复）→ 换成同步 stream + 后台线程；补了"流结束哨兵"防止卡死 |
| 面试怎么讲 | "我把图执行的每个节点通过 SSE 推给前端，客户和演示时都能看到 Agent 现在的状态——正在检索、正在调工具、正在审核。实现上用 updates 流模式，interrupt 场景用同步流放线程避免坑。" |

### 改动⑦　LLM-as-Judge 评测

| | 内容 |
|---|---|
| 原来 | 端到端评测靠**规则断言**（看回答里有没有关键词） |
| 现在 | 新增 `scripts/eval_judge.py`：LLM 当裁判，给回答打 4 维分（相关性/完整性/事实一致性/安全合规 1-5 分），通过标准：总评≥4 且事实≥4 且安全≥4 |
| 实际结果 | 首轮 10/11；裁判揪出"知识问答回答过于依赖表格，纯文本下答非所问"→ 我改了 prompt（知识类问题先答知识再查价 + 正文不依赖表格）→ 复测 11/11，维度均分 relevance 5.0 / overall 4.82 |
| 面试怎么讲 | 这就是最好的素材：**"规则断言测不出质量问题，我加了 LLM-as-Judge 四维评分，而且它真的抓到了 bug——答非所问——我修完 prompt 后从 10/11 提到 11/11。"** 评测驱动改进的闭环。 |

### 改动⑧　真 token 流式（最后加的）

| | 内容 |
|---|---|
| 原来 | 回复整包到达 → 前端本地打字机"假装"流式 |
| 现在 | 最终回复的 LLM token 经 SSE **逐字推送**，前端实时渲染（真流式） |
| 涉及文件 | `src/token_stream.py`（新，token 推送通道）、`src/llm_utils.py`（`_safe_llm(stream_tokens=True)` 内部改用 `llm.stream()`）、`src/stream_chat.py`（节点事件+token 统一管道）、前端 `App.jsx`（onToken 实时追加） |
| 技术要点 | 为什么之前不做：旧网关把整段生成完才吐第一个字（首包 15-38s），token 流纯负收益；**换官方 DeepSeek API 后首包 1-3s，真流式才有价值**。实现上用 ContextVar 在跑图线程里注入 pusher，工具调用轮的 chunk 内容为空天然不推字 |
| 面试怎么讲 | "流式分两层：过程层（节点事件实时可见）+ 内容层（回复 token 逐字推送）。token 层能否做取决于网关首包延迟——opencode 网关首包 15-38s 做了也没意义，官方 API 首包 1-3s 才有体验价值。" |

---

## 3. 文件地图（每个文件一句话）

```
app.py                    FastAPI 入口：/chat /chat/stream /history /healthz + 审批端点
src/
├── agent.py              主图：改写→检索→Supervisor→三分支→审核；编译带 checkpointer
├── order_agent.py        下单 Agent；create_order 前拦截 → 人工审批
├── after_sales_agent.py  售后 Agent（退款工单）
├── retrieval.py          混合检索（向量+BM25+RRF+Rerank）——没怎么动
├── memory.py             三层记忆（Redis/SQLite/Chroma）
├── mcp_client.py         工具客户端（线程安全 + 崩溃自愈）
├── mcp_servers/
│   ├── product_server.py 产品查询工具
│   ├── order_server.py   订单查询/创建工具（订单号防撞）
│   ├── refund_server.py  退款工具
│   └── sqlite_utils.py   【新】SQLite 共享层（WAL/busy_timeout/必关连接）
├── user_identity.py      【新】user_id 校验（单一来源）
├── approval.py           【新】待审批注册表 + 挂起文案
├── task_queue.py         【新】有界任务队列（替代裸线程）
├── node_events.py        【新】节点名→中文描述（流式可视化）
├── render_tools.py       表格展示协议（前端渲染表格用）
├── stream_chat.py        SSE：节点事件 + token 流 + 最终回复 + 挂起事件
├── token_stream.py       【新】真 token 流式推送通道
└── eval_cases.py         【新】评测共享用例
scripts/
├── eval_agent.py         规则断言评测（11题）
├── eval_judge.py         【新】LLM-as-Judge 四维评分
└── eval_retrieval.py     检索消融评测（85题）
web/src/
├── App.jsx               执行步骤丝带 + 打字机 + 表格渲染
└── api.js                SSE 解析（start/node/done/pending/error）
Dockerfile / docker-compose.yml / .github/workflows/ci.yml   【新】部署 + CI
docs/UNDERSTANDING.md     【新】本文
```

---

## 4. 怎么跑 / 怎么部署

```bash
# 本地（开发）
cd ~/Desktop/study_1
.venv/bin/python -m uvicorn app:app --port 8005        # 后端
cd web && npm run dev                                   # 前端 http://localhost:5173

# Docker（首次构建会下载约 600MB 的本地推理模型，需几分钟）
docker compose up --build
# 跳过模型下载、用本地缓存挂载：
docker compose up --build --build-arg DOWNLOAD_MODELS=0 -v ~/.cache/huggingface:/root/.cache/huggingface

# CI：push 到 GitHub 自动跑（.github/workflows/ci.yml：装依赖→构建前端→pytest）
```

> ⚠️ 本地没有 Docker 环境，`docker compose up` 未在本机实测；配置文件已通过 YAML 语法校验。

---

## 5. 30 分钟自查清单（证明你重新掌握了它）

**5 分钟·读代码**
1. `src/user_identity.py` — 6 行看完校验规则
2. `src/approval.py` — 看懂 register/list/approve 流程
3. `app.py` 的审批端点 + `/chat` 的挂起守卫（各 20 行）

**10 分钟·跑命令**
```bash
.venv/bin/python -m pytest tests/ -q                # 应全绿
# 起服务后：
curl -s http://127.0.0.1:8005/healthz               # 看队列计量
curl -s http://127.0.0.1:8005/approval/pending       # 看待审批
curl -N -X POST http://127.0.0.1:8005/chat/stream -H "Content-Type: application/json" \
  -H "X-User-Id: me" -d '{"message":"T400黑色多少钱"}'   # 看节点事件流
```

**10 分钟·自己讲一遍**（对着镜子或朋友）
- 为什么加 `X-User-Id`？不加密钥行不行？
- 订单为什么会被"挂起"？审批通过后发生了什么？
- 前端那个"执行步骤"是怎么来的？
- 规则评测和 Judge 评测有什么区别？

**5 分钟·看日志**
```bash
grep -E "Supervisor|审核|审批" logs/app.log | tail -20   # 路由与审核轨迹
```

---

## 6. 已知边界（面试被追问时的诚实答案）

1. **checkpointer 是进程内 MemorySaver**：服务重启后挂起的审批会丢。生产换 Postgres 后端持久化（代码只需换一行 compile 参数）。
2. **审批端点未加鉴权**：demo 可用，生产必须接管理员登录（清单 P3 第 13 项）。
3. **LLM 走 opencode 网关**，token 级流式不稳定 → 所以"流式"是节点事件 + 前端本地打字机，不是 token 流。
4. **embedding/rerank 模型在 Web 进程里**：多 worker 部署要小心（清单 P2 第 12 项建议下沉独立推理服务）。
5. 评测的裁判和作答是同族模型，存在自评偏差（业界通病，可用 GPT 等异构模型交叉打分缓解）。