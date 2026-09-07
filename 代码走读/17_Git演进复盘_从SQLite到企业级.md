# 17 · Git 演进复盘：从 SQLite 到企业级

> 目标：用 `git log` 把项目**从第一天到收官**的每一步动机串成故事，回答面试官最爱问的
> "你为什么从 A 换到 B"——SQLite→PostgreSQL、Chroma→Qdrant、线程桥→asyncio、
> 自研 MCP→官方 SDK、X-User-Id→JWT 账号体系、单会话→多会话。
> 读法：先看"时间轴总表"，再按 ①~⑦ 阶段讲故事，最后过 Q&A。**每一条结论都能在
> git log / 提交信息 / 仓库文档里找到出处**，不编历史。
> 本文配合 `git log` 现跑：仓库共 **34 个提交**，时间从 2026-07-27 到 2026-09-06。

---

## 一、先建立心智模型：三条主线 + 一张时间轴

整条演进可以压缩成三条主线，面试讲故事时"三条线齐头并进"最有条理：

| 主线 | 起点（最早提交） | 终点（HEAD） | 一句话本质 |
|---|---|---|---|
| 存储/检索 | 同步 `sqlite3` + ChromaDB（每用户一个目录） | PostgreSQL(asyncpg) + Qdrant 服务 | 文件 → 服务：从"自己能跑"到"多人并发跑" |
| 执行模型 | 同步 `invoke` + `threading.Thread` | 全链路 asyncio 单事件循环 | 线程乱飞 → 事件循环统一调度 |
| 身份/会话 | 写死 `USER_ID="123456"` | JWT 账号 + sessions 多会话 | 无身份 → 可伪造身份 → 真账号 |

时间轴里程碑（按日期，全部来自 `git log --format="%h %ad %s" --date=short`）：

| 日期 | 提交 | 主题 | 对应阶段 |
|---|---|---|---|
| 07-27 | `1860bfb` | 纺织B2B智能客服系统（首个提交，33 文件/+7586） | ① 起步 |
| 08-16 | `03b95cb` | MCP 工具层迁移 + 统一日志 + **测试套件** + 评估完善 | ② 铺垫 |
| 08-17 | `3f323d0` | 知识库扩充 + 自动入库流水线 + 评测重写 | ② 铺垫 |
| 08-25 | `e888baa` | 生产化与 agent 深度改造（多用户隔离/HITL/流式/Judge/部署CI/前端） | ②④⑥ 生产化第一波 |
| 08-26 | `7106bec` | **企业级演进：PostgreSQL + Qdrant + 官方 MCP SDK + 全链路异步** | ② 底座换血 |
| 08-27/28 | `34a41ee`→`9d58585` | CI 适配 PG、README 同步、托管 web/dist、端到端 11→25 题 | ②③ 收尾 |
| 09-05 | `8251b9b` | 管理端点 JWT 鉴权 + 审批审计 + `/dev/login` | ④ 鉴权 |
| 09-06 | `f097906` `28ee9e2` `12223e3` | 账号体系+玻璃UI+多会话 / 容器一键初始化+DEPLOY / **LEARNING.md** | ④⑤⑥⑦ 收官 |

> 记忆锚点：**8-25 之前是"单机 Demo 增强"，8-25 是"多人可用"，8-26 是"底座换血"，9 月是"给人用 + 上线"。**

---

## 二、先说清楚一件事：git 历史 ≠ 完整历史

看 `git log --reverse` 你会发现一个**重要事实**：仓库的第一个提交 `1860bfb`
（07-27 17:51）就已经叫"多 Agent+RAG+评估体系"——`src/agent.py`(668 行)、
售前/下单/售后三 Agent、评测脚本全都齐了。**真正的"单 Agent 聊天原型"阶段没有提交**，
只有 `simple_graph.py`(161 行单图示例)、`practice.py`、`test.py` 这些练习残留
藏在第一个提交里，一分钟后就被 `ad835e8`(07-27 17:52) 删掉了（-1149 行）。

另一层错位：`docs/LEARNING.md` 标题是"**从本地 Demo 到上线部署**的完整改造复盘"，
但它复盘的是**最后一波**（9 月账号体系 → UI → 多会话 → 部署），它的"起点"（第 0 节）
写的存储已经是"PostgreSQL + Qdrant"——那其实是 **8-26 `7106bec` 之后**的状态，
不是项目最早形态。所以本文叙事 = LEARNING.md 的骨架 + git log 的实证，
两处对不上的地方单独列在第五节，面试照实说反而加分。

---

## 三、七个阶段，一段段讲

### 阶段①　起步（07-27）：先跑通，什么简单用什么

**动机**：把"多 Agent 会聊纺织"这件事先跑起来。当时的存储形态（`git show 1860bfb` 实证）：

```python
# src/order_agent.py（1860bfb 版）：直接 sqlite3
import sqlite3
ORDERS_DB = ... / "data" / "orders.db"     # 下单直接连库文件

# src/memory.py（1860bfb 版）：sqlite3 + threading + chromadb
import sqlite3, json, threading
import chromadb                                # 向量库用 ChromaDB
self.db_path = self.user_dir / "chat.db"       # 每个用户一个 chat.db
chroma_path = str(self.user_dir / "chroma")    # 每个用户一个 chroma 目录

# src/agent.py（1860bfb 版）：全程同步
llm_with_tools.invoke([system] + safe)         # LLM 同步调用
app.invoke(state)                              # 图同步执行
threading.Thread(...)                          # 异步靠裸线程
```

特征一句话：**同步代码 + 文件型数据库（SQLite）+ 嵌入式向量库（ChromaDB）**。
requirements.txt 也只有 11 行：fastapi / langgraph / langchain / chromadb /
sentence-transformers…没有 asyncpg、没有 SQLAlchemy、没有官方 MCP SDK。

> 关键文件：`src/order_agent.py`、`src/memory.py`、`data/products.db`
> 关键提交：`1860bfb`（入库）、`ad835e8`（当天清理练习文件）
>
> **「面试怎么讲」**："第一天我只求跑通——能聊、能查价、能下单入库。所以选最省事的
> 同步 sqlite3 + 嵌入式 ChromaDB。代价是后来全部返工成服务化，但'先跑通'让业务逻辑
> 先被验证了。"

### 阶段②　企业级演进（08-16 → 08-26）：底座从"文件"换成"服务"

**动机**：多人要用 → 单文件并发写就 `database is locked`；多实例部署需要数据可共享；
同步阻塞扛不住慢 LLM 的长连接。演进其实分两步走：

**第一小步（08-16 `03b95cb`，自研 MCP 层）**：把三个 Agent 里手写的工具函数
抽成独立进程的 **product / order / refund 三个 MCP Server**（JSON-RPC over stdio），
并**自己用标准库写了一个 MCP Client**（`src/mcp_client.py`，214 行）。配套新增
`logging_config.py`、第一批 `tests/`。动机是"工具能独立测、能热插拔"。
同批写了 982 行的 `MCP_MIGRATION_GUIDE.md` 学习笔记（08-17 `0f3a30c` 就把它
移出跟踪了，gitignore 掉）。

**第二小步（08-25 `e888baa`，SQLite 并发止血）**：换库之前先给旧库打补丁——
`src/mcp_servers/sqlite_utils.py` 统一 WAL + `busy_timeout=5000` + 连接必关
（UNDERSTANDING.md 改动②，作者自测 20 线程 400 并发写 0 失败）。

**第三步（08-26 `7106bec`，一次提交换掉四样东西，+3988/-1929 行）**：

| 换什么 | 从 | 到 | 落地文件 |
|---|---|---|---|
| 业务库 | 手写 sqlite3 | SQLAlchemy 2.0 async + asyncpg 连接池 | `src/db.py`(新增 152 行) |
| 向量库 | ChromaDB（每用户一目录） | Qdrant：LocalMode / `QDRANT_URL` 双模式一份代码；单 collection + payload 多租户过滤 | `src/vector_store.py`(新增) |
| 异步 | `invoke`/裸线程 | `ainvoke`/astream、节点 async、`stream_chat` **移除线程桥（单事件循环）**、lifespan 异步初始化 MCP、记忆提取改 `asyncio.create_task` | 全局 + `stream_chat.py` |
| MCP | 自研 subprocess 客户端 + 手写 `_validate_args` | 官方 `mcp` SDK：`ClientSession + stdio_client`；三 Server 重写为 **FastMCP**（内建参数校验） | `mcp_client.py`、`mcp_servers/*` |
| Python | 3.9 | 3.12（MCP SDK 要求 ≥3.10） | requirements.txt |

一次提交同时落地，提交信息里写了完整的**验证证据**：pytest 66 passed；
85 题检索消融混合+Rerank MRR **0.951 → 0.959（不降反升）**；11 题端到端 11/11。
这就是"先本地 Demo → 8-25 止血 → 8-26 换血"的完整动线。

> 关键文件：`src/db.py`、`src/vector_store.py`、`scripts/migrate_sqlite_to_pg.py`、`src/mcp_client.py`
> 关键提交：`03b95cb`、`e888baa`、`7106bec`（+ 8/27 CI 适配 `34a41ee`、README 同步 `cddb3b7`）
>
> **「面试怎么讲」**："存储升级我分了三步：先自研 MCP 把工具解耦、再给旧 SQLite 打并发
> 补丁止血、最后一天之内把 PG + Qdrant + 全异步 + 官方 MCP SDK 一起换掉——用一个
> 大提交锁定切换点，换完立刻用既有评测回归：检索 MRR 反而从 0.951 升到 0.959。"

### 阶段③　评估与测试体系：比代码更早存在的"裁判"

反转来了：**评估体系比 pytest 早**。`1860bfb` 首提交就带着 `scripts/eval_agent.py` /
`eval_retrieval.py` / `eval_v2.py` 和 `eval_results/*.json`——它是这个项目的"质检员"，
从一开始就在。演进节奏：

| 日期 | 提交 | 变化 |
|---|---|---|
| 07-27 | `1860bfb` | 检索评测（eval_retrieval）+ Agent 评测（eval_agent）首版 |
| 08-16 | `03b95cb` | 评测重构（eval_agent 替代 eval_v2）；**`tests/` pytest 套件首次出现**（conftest + 6 个测试文件） |
| 08-25 | `e888baa` | 新增 `scripts/eval_judge.py`（LLM-as-Judge 四维打分）+ `eval_results/eval_judge.json`；tests 扩到多用户/HITL/节点事件 |
| 08-28 | `9d58585` | 端到端用例 11 → **25 题**（售前7/下单4/售后4/闲聊4/安全6），修复 4 个"断言过严"用例后 25/25 |
| 09-05 | `aa2c678` | **校准评测口径**：LLM-as-Judge 11/11（实测），端到端规则 25/25 不变 |

为什么评测体系反而是最老的公民？看 UNDERSTANDING.md 改动⑦ 就有答案：
**规则断言测不出"质量问题"**，所以作者在 8-25 引入 LLM 当裁判打 4 维分
（相关性/完整性/事实一致性/安全合规），而且**裁判真的抓到了 bug**——"知识问答过于
依赖表格、纯文本下答非所问"，改 prompt 后 10/11 → 11/11。这就是"评测驱动改进"的闭环。

> 关键文件：`scripts/eval_*.py`、`tests/`、`eval_results/`
> 关键提交：`1860bfb` → `03b95cb` → `e888baa` → `9d58585` → `aa2c678`
>
> **「面试怎么讲」**："评测是我的'先有秤再买菜'：换向量库之前就有 85 题检索消融，
> 所以 8-26 换 Qdrant 当天敢断言 MRR 不降反升。LLM-as-Judge 不是装饰品——它真的
> 抓出过一次答非所问，我改完 prompt 从 10/11 修到 11/11。"

### 阶段④　鉴权三级跳：从"没人管"到"换证思想"

| 版本 | 时间/提交 | 身份长什么样 | 安全漏洞 |
|---|---|---|---|
| v0 | 最早（无提交） | `app.py` 写死 `USER_ID="123456"`，所有人共用记忆 | 没有身份概念 |
| v1 | 08-25 `e888baa` | 请求头 **`X-User-Id`**：`src/user_identity.py` 统一校验（1-64 位 `[A-Za-z0-9_-]`，防目录穿越），缺省降级 `guest` | **谁都能伪造任意 user_id**（提交里注释原话：对应企业微信 external_userid 场景） |
| v2 | 09-05 `8251b9b` | `/dev/login` mock（仅 `DEV_MODE=1`）+ 管理端点 JWT 化：`src/auth.py` 认证/授权分层、审批写 `audit_log` | /dev/login 仍是假身份，但门卫上岗了 |
| v3 | 09-06 `f097906` | **真账号体系**：`src/users.py`（bcrypt round 12、username 小写归一化、开放注册只给 customer）、chat/history/stream 全部强制 token，**移除 guest/X-User-Id 回退** | 基本闭环（JWT 无法吊销是诚实遗留，见第 02 步笔记） |

安全动机非常直白（LEARNING.md 第 1 节决策表原话）："无 token 访问一律 401——
否则登录系统形同虚设，**任何人可冒充任意客户**"。取舍上的巧思是 `auth.py` 的
**"换证"思想**：token 一律由 auth.py 签发、签发入口可替换——密码登录、DEV_MODE 的
mock `/dev/login`、将来企业微信 OAuth 回调，前端只认 `{token, role}`。

> 关键文件：`src/user_identity.py`(v1)、`src/auth.py`(v2)、`src/users.py`(v3)、`scripts/create_admin.py`(v3)
> 关键提交：`e888baa` → `8251b9b` → `f097906`
>
> **「面试怎么讲」**："身份演进走三级：写死 user_id → X-User-Id 请求头（谁都能伪造）→
> /dev/login mock（只留开发模式）→ 真 JWT 账号。每升一级都是因为上一级有个明确的洞：
> 伪造身份、越权读别人订单。最后'签发入口可替换'的设计让将来接企业微信 OAuth 只动一处。"

### 阶段⑤　会话演进：从"一个人一条流水"到 sessions 表

多会话之前长什么样？`e888baa`(8-25) 的 `app.py` 里写得很直白（git show 该版本可查）：

```python
# app.py（e888baa 版）——线程即用户、用户即会话
thread_id: str              # thread_id 即 user_id
def _resume_approval(thread_id, ...):   # thread_config 以 user_id 命名会话
```

即 **thread_id == user_id**：一个用户一条会话流水线，历史是"每个用户一条流水"
（LEARNING.md 第 0 节原话）。这时的 LangGraph checkpointer 用 `user_id` 当 thread 名，
HITL 挂起/恢复都靠它。

09-06 `f097906` 引入真正的多会话：
- `sessions` 表 + `conversations.session_id`（幂等 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`，
  老数据统一迁到 `'default'`）；
- `src/sessions.py`：会话 CRUD + **所有权校验**（`WHERE id=? AND user_id=?`，跨用户一律 404）
  + 自动标题（新会话首条用户消息前 18 字）——见 `sessions.py` 模块注释（现文件 1-20 行）；
- 审批回写：挂起时在 approval 注册表记 `session_id`，管理员审批后结果存回客户当时所在会话。

> 关键文件：`src/sessions.py`、`src/memory.py`、`src/approval.py`
> 关键提交：`e888baa`（thread=user）→ `f097906`（sessions 表）
>
> **「面试怎么讲」**："早期'用户=会话'，多开几个话题历史就全串在一起。我加了 sessions
> 表，消息按 (user_id, session_id) 行级隔离，老数据幂等迁到 default 会话兜底；
> 连 HITL 审批都记录了 session_id，审批结果能回写到客户当时那个对话框。"

### 阶段⑥　Docker / CI / 上线：脚手架先于部署

| 提交 | 内容 |
|---|---|
| 08-25 `e888baa` | 第一次出现 `Dockerfile`(32行)/`docker-compose.yml`/`.github/workflows/ci.yml`/`.dockerignore`，同时写了 `PRODUCTION_MIGRATION_CHECKLIST.md` 当作战地图 |
| 08-25~28 | CI 连环修：锁依赖版本(`1be82d6`)→ 模型缓存判断(`d76a312`)→ pytest 入 requirements(`ae79cfc`)→ LLM 懒加载 + `.env.example`(`b1b5283`)→ CI 起 PG 容器适配迁移(`34a41ee`)→ 修复无 key 环境测试收集(`e51cff0`) |
| 09-06 `28ee9e2` | **容器一键初始化收官**：`docker_entrypoint.sh`（等 PG/Qdrant → 建表/管理员 → 灌知识索引 → 启动）；Dockerfile 改**多阶段**（node 先构建前端）；模型下载移到 `COPY . .` 之前；281 条产品导出成 `sql/001_products.sql` 由 postgres initdb 自动灌入；写 `docs/DEPLOY.md` |

上线踩坑的"真金白银"都在 LEARNING.md 第 9 节，面试随便挑一条都是故事：
zip 包 397MB（漏排缓存）、容器"空转三连坑"（不初始化 / sys.path 找不到 src /
产品表空——`data/*.db` 被当本地数据误排）、HF 模型下载三连坑（offline 自杀 /
Xet 绕镜像 / hf-mirror 连不上）、`crypto.randomUUID` 只在安全上下文可用
（公网 IP 明文 http 白屏，`e39a3e8` 加 `getRandomValues` 兜底）、Qdrant LocalMode
双进程锁。

> 关键文件：`Dockerfile`、`docker-compose.yml`、`.github/workflows/ci.yml`、`scripts/docker_entrypoint.sh`、`sql/001_products.sql`
> 关键提交：`e888baa` → `b1b5283` → `28ee9e2` → `e39a3e8`
>
> **「面试怎么讲」**："我先把 CI 和迁移清单写上，再谈部署——所以每个换库提交都敢
> 带'全绿'证据。上线那天容器'healthz 绿但功能全无'的三连坑（不初始化/import 不到
> src/产品表空）我一个个用入口脚本、sys.path、SQL 种子修掉；这些比顺利跑通更值钱。"

### 阶段⑦　文档复盘：知识是怎么一层层长出来的

| 文档 | 出生提交 | 角色 |
|---|---|---|
| `ARCHITECTURE.md` / `README.md` / `EVALUATION.md` | 07-27 `1860bfb` | 原始架构图 + 评测口径（当时写的是 **ChromaDB**！） |
| `MCP_MIGRATION_GUIDE.md`（982 行） | 08-16 `03b95cb` | MCP 迁移学习笔记 → 08-17 `0f3a30c` 移出跟踪 gitignore |
| `docs/UNDERSTANDING.md` | 08-25 `e888baa` | **"原来→现在→为什么→面试怎么讲→怎么自测"** 八处改动逐条讲（现文件结构） |
| `PRODUCTION_MIGRATION_CHECKLIST.md` | 08-25 `e888baa` | 改造战地图：现状诊断 → P0 并发安全 → P1 存储升级 → P2 架构部署 → P3 安全合规 |
| `WEB_UI_STREAMING.md` | 08-25 `e888baa` | 流式设计旧版 |
| `docs/DEPLOY.md` | 09-06 `28ee9e2` | 部署手册 |
| `docs/LEARNING.md` | 09-06 `12223e3` | **收官复盘**：从"缺什么"到 12 节踩坑全集 |

还有两个"非正式文档"佐证学习过程：`问题.md`（现 4 行）记了两条真问题——
LLM 懒加载是 `b1b5283` 的 CI 崩溃教训、"模块级 `__getattr__` 兼容层是渐变改造的
典型手法"；`面试/` 目录曾在 `7106bec` 里被提交过 8 篇中文笔记，`8251b9b` 明确
"个人面试笔记移出跟踪并加入 .gitignore（不公开）"。

> **「面试怎么讲」**："我养成了'改造必留档'的习惯：动手前写 PRODUCTION_MIGRATION_
> CHECKLIST 战地图，动手后把每处改动按'原来→现在→为什么'写进 UNDERSTANDING，
> 全部收尾后写 LEARNING 复盘。个人学习笔记（面试/、MCP 迁移指南）gitignore 掉，
> 仓库里只留给别人看的。"

---

## 四、总表：一句话动机版

| 阶段 | 时间 | 代表提交 | 一句话动机 |
|---|---|---|---|
| ① 起步：同步 SQLite + ChromaDB | 07-27 | `1860bfb` | 先跑通，验证业务，不折腾基础设施 |
| ② 企业级：PG + Qdrant + 全异步 + 官方 MCP SDK | 08-16→08-26 | `03b95cb`→`e888baa`→`7106bec` | 多人/多实例要用，文件型存储与同步阻塞扛不住 |
| ③ 评估与测试 | 07-27→09-05 | `1860bfb`→`e888baa`→`aa2c678` | 先有秤再买菜：每次大改都有回归数字兜底 |
| ④ 鉴权：X-User-Id→/dev/login→JWT 账号 | 08-25→09-06 | `e888baa`→`8251b9b`→`f097906` | 伪造身份漏洞 → 门卫上岗 → 真账号闭环 |
| ⑤ 会话：thread=user → sessions 表 | 09-06 | `f097906` | 一人多话题，历史不能串 |
| ⑥ Docker/CI/上线 | 08-25→09-06 | `e888baa`→`28ee9e2` | 从"本地全绿"到"服务器一键起" |
| ⑦ 文档复盘 | 全程 | `1860bfb`…`12223e3` | 改造必留档，踩坑变谈资 |

## 五、文档与 git 对不上的地方（照实说，别藏）

走读时我发现的"历史/文档不一致"，面试被追问时主动交代反而显专业：

1. **LEARNING.md 的"起点"不是项目最早形态**：它第 0 节写的存储已是 PG+Qdrant，
   那是 8-26 `7106bec` 之后的状态；SQLite/Chroma 时代（7/27–8/26）它没覆盖。
2. **ARCHITECTURE.md 停更在 ChromaDB 时代**：`git log -- ARCHITECTURE.md` 只有两条
   （`1860bfb`、`03b95cb`），8/26 换 Qdrant 后没人回改——现文件第 21/78 行仍写
   "ChromaDB(向量)"，而 README 在 8/27 `cddb3b7` 已同步成 Qdrant。文档互相滞后。
3. **PRODUCTION_MIGRATION_CHECKLIST.md 冻结在改造前**：它自 8-25 `e888baa` 创建后再
   没被任何提交改过（git log 仅 1 条），第 6/7 项"SQLite→PG / 向量库迁移"至今没打 ✅，
   但 8-26 `7106bec` 已实际完成且提交信息写了全绿验证。
4. **ChromaDB 没清干净**：换库当天 `7106bec` 的 diff 里 `index/chroma_db/chroma.sqlite3`
   竟然还在更新，且到 HEAD 仍被 git 跟踪（`git ls-files | grep chroma` 有 5 条）；
   `src/logging_config.py:44` 的日志降噪名单里至今留着 `"chromadb"`。
5. **早期文档已被删除**：首提交里的 `MCP.md` / `FAILURE_HANDLING.md` /
   `MEMORY_ARCHITECTURE.md` / `TOKEN_OPTIMIZATION.md` / `TODO.md` 现在都不在仓库；
   `MCP_MIGRATION_GUIDE.md` 只是本地 gitignore 残留；`面试/` 已移出跟踪。
6. **"单 Agent 原型"只在文档里，不在 git 里**：git 最早提交已含三 Agent；
   原型期证据只有当天被删的 `simple_graph.py` / `practice.py` / `test.py` 文件名。

---

## Q&A

**Q1：为什么 SQLite → PostgreSQL？**
并发与多实例是主因。SQLite 是"单文件、单写者"：多人同时下单会 `database is locked`
（作者 8-25 只能靠 WAL + busy_timeout 止血）；部署成多个容器实例时，每个实例写自己
文件里的库 = 数据分叉。PG 是独立服务：多连接池（SQLAlchemy 2.0 async + asyncpg）、
行级隔离（conversations 带 user_id）、还能上审计与备份（`pg_dump`，DEPLOY.md 速查）。
代价是引入迁移：`scripts/migrate_sqlite_to_pg.py` 把 products.db / orders.db /
每用户 chat.db 搬到 PG 三张表并补 user_id 列（该脚本模块注释自称"一次性迁移"）。

**Q2：为什么 Chroma → Qdrant（本地 → 服务化演进路线）？**
Chroma 是嵌入式库，当时的用法是**每个用户一个 Chroma 目录**——用户一多，索引各自为
政、无法共享；而且 LocalMode 同一目录只允许一个进程持有（LEARNING.md 9.6 双进程锁坑）。
Qdrant 是独立服务（`QDRANT_URL`），容器里跑一份大家连：一份代码支持
"LocalMode 本地开发 / 独立服务生产"双模式（`src/vector_store.py`），知识库 + 用户偏好
合并成**单 collection + payload 多租户过滤**（7106bec 提交信息原话），比"每人一个库"
正确一个量级。验证也过硬：85 题消融 MRR 0.951 → 0.959。

**Q3：为什么线程桥 → asyncio 单事件循环？**
早期"异步"靠 `threading.Thread`（SSE 桥、偏好提取）——线程数无上限会打爆进程
（UNDERSTANDING.md 改动③：每请求起一个裸线程），且线程间共享状态要加锁、心智负担大。
全异步后：FastAPI → asyncpg → LangGraph `ainvoke`/astream → LLM astream 全链路
`await`，一个事件循环在 IO 等待间隙服务别的请求，慢 LLM 不再占死资源。中间态是先
用"同步 stream + 后台线程 + asyncio.Queue"绕开 0.6.x `astream`+`interrupt()` 的坑
（UNDERSTANDING.md 改动⑥），8-26 才正式移除线程桥（7106bec 提交信息："stream_chat
移除线程桥（单事件循环）"）。

**Q4：为什么自研 MCP → 官方 SDK？**
08-16 没有（或不想引入）官方 SDK，作者用标准库按 JSON-RPC over stdio 手写 Client
（214 行），还得自己维护参数校验、超时、子进程崩溃重启（UNDERSTANDING.md 改动④：
RLock 串行 + 30s 超时 + 自动重启，防"响应串线/永久挂起"）。官方 `mcp` SDK 出来后：
`ClientSession + stdio_client` 把协议细节全包了，Server 用 **FastMCP** 重写、
内建参数校验替代手写 `_validate_args`（7106bec 提交信息原话）。取舍逻辑是**标准到来
之前先用最小实现跑通协议，标准成熟就换**——不算白做，那些坑让作者更懂协议本身。

**Q5：为什么 /dev/login（mock）→ 正式账号体系？**
`/dev/login` 只是开发期演示双身份的快捷方式（DEV_MODE=1 才存在），它无法回答
"谁是你、密码对不对"。真账号的价值：① 密码哈希（bcrypt round 12）与"注册即签发
token"构成可审计的身份闭环；② 开放注册只给 customer、admin 由幂等种子脚本建，
堵住越权注册；③ 无 token 一律 401，删掉 guest/X-User-Id 回退——否则"任何人可冒充
任意客户"（LEARNING.md 决策表原话）。取舍：JWT 无状态无法主动吊销，作者在
LEARNING.md 第 12 节如实列为遗留边界。

**Q6：为什么单会话（thread_id==user_id）→ sessions 表多会话？**
早期一人一线程，一旦用户想同时聊"面料推荐"和"退货"两个话题，历史全串一条流水，
LangGraph 记忆上下文互相污染。sessions 表让消息按 (user_id, session_id) 隔离，
老数据幂等迁到 `'default'` 兜底（现 `sessions.py` 模块注释：default 是历史遗留、
不出现在列表）。连带把 HITL 审批也绑上 session_id，审批结果能回写客户当时所在会话。

**Q7：迁移脚本怎么保证幂等？重跑会怎样？（自己发现的好问题）**
三层：① 建表 `ensure_schema()` 幂等（IF NOT EXISTS 一族）；② 业务表
products/orders/refunds 全部 `ON CONFLICT (id) DO UPDATE`——重跑只更新不重复
（migrate_sqlite_to_pg.py:38/53/65）；③ 显式插入 id 后必须
`setval(pg_get_serial_sequence(...), MAX(id))` 重置 serial，否则新下单直接撞主键
（同文件 111-117 行，注释明说"关键"）。**但** conversations/profile 的迁移没有
ON CONFLICT（对话没有自然业务主键），重跑会重复插对话——所以脚本 docstring 自称
"一次性迁移"，业务表幂等、记忆表不幂等，这个细节面试答出来很加分。

**Q8：如果重来一次，哪些取舍会不同？（基于 LEARNING.md 自认的坑）**
① **别把数据文件当产品数据的唯一来源**：281 条产品藏在 `data/*.db` 里，部署打包时
被当"本地数据"误排，产品表直接空——重来一次会一开始就用 `sql/001_products.sql`
种子脚本管理（LEARNING.md 9.3）。② **LLM/模型别 import 即实例化**：CI 无 .env 直接
崩（`b1b5283`），懒加载是血的教训。③ **HITL 的审批注册表 + LangGraph checkpoint
别放进程内存**：重启丢挂起单，作者自己在第 12 节说"换 PostgresSaver/Redis 只动
compile 一处"。④ **created_at 别用 ISO 字符串**：SQLite 时代遗留，PG 里不能直接用
时间函数（第 02 步笔记 Q6 同款吐槽）。⑤ **动效宁可克制**：UI 玻璃化 v6 海浪动画被
用户打回 v5（LEARNING.md 第 3 节），重来会直接做 v5。
