# 纺织客服系统 · 生产级改造清单

> 目标：从「单机单用户 demo」改造成「可多人并发、可水平扩展、可观测、可运维」的生产级服务。
> 分 P0 → P3 四个优先级推进。P0 是"多用户会崩/会串数据"的硬伤，必须先做。

---

## 现状诊断：为什么现在不支持多人共用

| 问题 | 位置 | 后果 |
|---|---|---|
| 用户 ID 写死 `"123456"` | `app.py:30` | 所有人共用同一份记忆 |
| MCP Client 全局单例 + 同步读子进程 stdout | `mcp_client.py:185-213`、`_recv` | 多线程并发时 JSON-RPC 响应串线，工具结果错乱 |
| `rerank_model` / `retriever` 模块级全局单例 | `retrieval.py:34,78` | CrossEncoder/SentenceTransformer 并发 predict 非线程安全 |
| SQLite 每次操作开新连接，无 WAL / busy_timeout / 连接池 | `memory.py`、三个 `mcp_servers/*.py` | 并发下 `database is locked` |
| ChromaDB 每用户每次调用新建 `PersistentClient` + embedding 模型 | `memory.py:130-140` | 极慢，多 client 同路径锁冲突 |
| `extract_and_store` 每请求 `threading.Thread` | `app.py:149` | 线程无上限，打爆进程 |
| 前端 `innerHTML` 直接渲染 | `app.py:107` | XSS |
| LLM/MCP/检索全是同步阻塞调用 | `app.py` `def chat`、`agent.py` | 阻塞事件循环，吞吐上不去 |
| 无鉴权 / 无会话隔离 / 无限流 | `app.py` | 任何人都能调，无法区分用户 |
| 端口三处不一致 | `app.py:6,168,170` | 8000/8003/8005 混乱 |

---

## P0 · 正确性与并发安全（不改也能先上线的底线）

### 1. 多用户隔离（session / user_id 从请求来）✅ 已完成
- [x] 删除 `app.py` 的硬编码 `USER_ID`，改为从请求头 `X-User-Id` 注入（新增 `src/user_identity.py` 单一校验源：1-64 位 `[A-Za-z0-9_-]`）。
- [x] `/chat`、`/history`、`/chat/stream` 均按请求中的 `user_id` 取 `get_user(user_id)`，不再用全局 `memory`。
- [x] 校验 `user_id` 合法：非法显式值 → `400`；缺省 → 降级 `guest`；`get_user` 内另有 `sanitize_user_id` 兜底（防目录穿越）。
- [x] 前端 `web/src/api.js`：localStorage 持久化 user_id，请求自动带 `X-User-Id`。
- **验收**：✅ 已通过（两个 user_id 并发对话，历史/偏好互不串；`../..` → 400；测试 `tests/test_user_identity.py` + `test_memory.py::test_user_isolation`）。

### 2. MCP Client 线程安全 ✅ 已完成
- [x] 「全局单例 + 同步读 stdout」→ 全局 RLock 串行化 in-flight 请求（工具是毫秒级本地 SQLite，串行代价可忽略；并发吞吐属后续 HTTP 传输演进）。
- [x] `_recv` 改「读线程 + join(timeout)」：子进程僵死不再永久阻塞，超时抛 `ConnectionError`（默认 30s）。
- [x] 子进程崩溃自愈：`call_tool` 检测到死进程/超时后自动重启该 Server（重做 initialize/tools/list）并重试一次；`_send` 对 BrokenPipe 统一转 `ConnectionError`。
- **验收**：✅ 已通过（单测 `tests/test_mcp_client.py`：20 线程 × 200 并发调用无串线；kill -9 后自动重启重试成功；死进程 5s 内返回错误不挂起。运行中实测：kill 掉 product 子进程后下一次对话自动恢复）。

### 3. SQLite 并发兜底（迁移前的止血）✅ 已完成
- [x] 新增共享层 `src/mcp_servers/sqlite_utils.py`：`connect`（`busy_timeout=5000` + 进程级一次性 `PRAGMA journal_mode=WAL`）、`conn_ctx`（finally 必关）、`query_all/query_one/execute/executescript`。
- [x] 三个 MCP server（product/order/refund）+ `src/memory.py` 全部改走共享层，修掉 `create_order`/`create_refund`/`search_product` 的**异常时连接泄漏**。
- [x] 顺带修复并发暴露的 bug：`order_no` 秒级生成会撞 UNIQUE → 改为「秒+微秒+随机 4 位」（`ORD-20260824-165808931234`）。
- **验收**：✅ 已通过（单测 `test_servers.py::test_concurrent_create_order` 8 线程×10 单；额外压测 20 线程×400 并发写 → 0 失败、0 撞单、0.13s；真实库 orders.db/products.db 已确认处于 WAL 模式）。

### 4. ChromaDB 单例化 + 合并多用户
- [ ] `_chroma_collection` 缓存成进程级单例，不要每次新建 client + embedding 模型。
- [ ] 目标（配合 P1）：从「每用户一个目录」改成「一个 collection + `user_id` 元数据过滤」。
- **验收**：`retrieve_preferences` / `extract_and_store` 不再反复加载 100MB 模型。

### 5. 异步记忆提取改队列 ✅ 已完成
- [x] 新增 `src/task_queue.py`：有界任务队列（默认 200）＋固定 worker 池（默认 2）＋计量（submitted/processed/dropped/queued，供 `/healthz` 观测）。
- [x] 移除 `app.py` / `agent.py` / `stream_chat.py` 的裸 `threading.Thread`，统一 `get_extraction_queue().submit(...)`。
- [x] 新增 `GET /healthz`：存活探测 + 队列计量（后续 Docker 健康检查复用）。
- **验收**：✅ 已通过（单测 `tests/test_task_queue.py` 4 项：顺序执行/满队列丢弃/任务异常不杀 worker/单例；运行中实测 submitted=1 → processed=1，请求延迟与提取解耦）。

---

## P1 · 存储升级（SQLite → PostgreSQL，Chroma → pgvector）

### 6. 关系库迁移
- [ ] 引入 SQLAlchemy 2.0（async）+ `asyncpg`（或 psycopg3）+ 连接池。
- [ ] 用 **Alembic** 管理迁移：`products` / `orders` / `refunds` / `conversations` / `profile` 五张表全部纳入迁移脚本。
- [ ] 补 `scripts/init_db.py`：建表 + 从 `products.json` 播种 281 条产品（现在生产代码里**没有建表/播种逻辑**）。
- [ ] 解决 orders 表 schema 漂移（`phone/address/delivery_date` 是后 `ALTER` 的）——迁移里一次性规范化。
- **验收**：全新环境一条命令初始化；`alembic upgrade head` 可重放；旧 SQLite 数据脚本化导入 PG。

### 7. 向量库迁移
- [ ] 知识库 `index/chroma_db` → **pgvector**（优先，少一套基础设施）或 Milvus/Qdrant/Pinecone。
- [ ] 用户画像 `data/users/<id>/chroma` → 统一向量表 + `user_id` 元数据过滤（与 P0-4 合并）。
- [ ] BM25 自写 pickle → PG `tsvector` 全文检索，或 OpenSearch/ES（如检索规模大）。
- [ ] embedding/reranker 模型下沉为独立推理服务（GPU worker），Web 进程不再加载模型。
- **验收**：`build_index.py` 产出写入 pgvector；`retrieval.py` 的 `HybridRetriever` 后端可切换；检索评测（85 题）指标不回退。

### 8. 缓存与队列规范化
- [x] Redis 加连接池（`redis.asyncio`），配置化 host/port/db/password。
      实现：`REDIS_URL`（含密码/库号）+ `REDIS_CACHE_TTL`/`CHAT_CACHE_MAXLEN`/
      `REDIS_KEY_PREFIX`/`REDIS_SOCKET_TIMEOUT`；连接池 `max_connections=20` +
      超时 1.5s + `health_check_interval`；惰性连接（import 期不再 ping）；
      熔断冷却（`REDIS_FAIL_COOLDOWN`）到点自动重试；降级后端换成**有界 LRU**。
- [x] 修复 `load_recent` 的「Redis 命中即返回、不回补 PG」合并 bug。
      命中条件改为**缓存条数 ≥ n**；回源按 `max(n, CHAT_CACHE_MAXLEN)` 取满窗口，
      用 `DEL + RPUSH` 原子重建（不再追加，杜绝重复/乱序）。
- [x] 顺带修掉的三个一致性/容错 bug（同一次修复）：
      ① `sessions.delete_session` / `clear_history` 现在会 `invalidate_cache()`
      （旧实现删库不清缓存 → 删掉的对话在 TTL 内"复活"；`clear_history` 更只清了
      `default` 一个 key）；② `save_messages` 改为**先落库再写缓存**且缓存异常
      fail-open（旧实现缓存一抖连消息都丢）；③ 进程内降级缓存从裸 dict 换成有界 LRU。
- [x] 基础设施接通：`redis==5.2.1` 进 `requirements.txt`（旧版没装 → 生产实际跑的是
      每进程一份的 dict）、compose 增加 redis 服务（`--maxmemory 256mb
      --maxmemory-policy allkeys-lru`，纯缓存不挂卷）+ `REDIS_URL=redis://redis:6379/0`、
      `.env.example` 补齐 `REDIS_*`、`/healthz` 暴露 `"cache": "redis" | "local"`。
- **验收**：Redis 清空/部分回写后，历史仍能从 PG 补齐 —— ✅ `tests/test_memory_cache.py`
  （9 条，修复前 7 条红）；四种模式全量 111 passed：Redis 正常 / 不可达（fail-open）/
  `REDIS_ENABLED=0` / 未安装 redis 包；连续多跑不再出现缓存污染型 flaky。

### 8b. 凭证体系：短期 access + 可轮换 refresh（2026-09，路线②）
- [x] access token 15 分钟无状态（`ACCESS_TOKEN_TTL_MINUTES`），claims 加 `typ/jti/sid/iss/aud`；
      `decode_token` 保持**纯函数**（热路径零 IO），`iss/aud/typ` 采用"存在才校验"以便滚动升级。
- [x] refresh token 存 Redis（`src/auth_sessions.py`）：只存 SHA-256 哈希、`{sid}.{secret}` 形态；
      **轮换**（RFC 9700 对 public client 的 MUST）+ **重用检测**（旧 token 重放 → 整会话作废，
      并写 `audit_log.refresh_reuse_detected`）+ **竞态宽限**（默认 30s，对应 Okta 的 rotation leeway）；
      判定/换发/续期是一段 **Lua**，避免并发刷新竞态。
- [x] 双封顶：空闲期 14 天（滑动）+ 绝对上限 30 天，防"活跃用户被无限续命"。
- [x] 服务端会话管理：`POST /auth/logout`（登出必须调服务端，否则被拷走的 refresh 一直有效）、
      `GET /auth/sessions`、`DELETE /auth/sessions/{sid}`（校验归属防越权）。
- [x] **fail-closed**：Redis 不可用 → 登录/刷新 503（绝不放行）；已签发 access 仍有效（≤1 个 TTL）。
      配套：`study1:auth:*` 必须开 AOF、不能与纯缓存共用 `allkeys-lru`（会挤掉活跃会话）。
- [x] 前端：`authFetch` 401 → 刷新 → 重放一次；**单飞（single-flight）**避免并发 401 打出多次刷新
      （否则第二次会拿已轮换作废的 refresh → 服务端判重用 → 用户被莫名登出）；
      `auth:expired` 事件统一回登录页。测试：`web/test/api.singleflight.test.mjs`（`npm test`）。
- **验收**：`tests/test_auth_refresh.py` 13 条全绿（轮换/重用/宽限/双封顶/登出/踢设备/越权/fail-closed）；
  CI 增加 redis service + 前端测试步骤；端到端 curl 跑通 登录→刷新→并发宽限→超窗重用→登出 全流程。

---

## P2 · 架构与部署（异步、容器化、可观测）

### 9. Web 层异步化
- [ ] `/chat` 改为 `async def`，LLM 调用用 `ChatOpenAI.ainvoke`，避免阻塞事件循环。
- [ ] 长回复改 **SSE 流式**返回（现在要等整个 graph 跑完才返回）。
- [ ] 加请求级超时 + 取消（LangGraph 支持 `config` 递归取消）。

### 10. 配置管理
- [ ] 用 `pydantic-settings` 集中管理配置，`.env` 只放密钥，去掉硬编码（端口、模型名、路径）。
- [ ] 密钥走环境变量 / 云 KMS，不提交仓库。

### 11. 可观测性
- [ ] 结构化 JSON 日志 + 全局 `request_id` 贯穿（`logging_config.py` 改造）。
- [ ] 指标埋点：QPS、P50/P99 延迟、LLM token、工具调用次数（Prometheus）。
- [ ] 错误追踪（Sentry），保留 LangSmith trace。

### 12. 部署（部分完成）
- [x] Dockerfile + docker-compose（app 单体 + 本地 HF 模型缓存卷；健康检查 `/healthz`，重启策略 `unless-stopped`）。
- [x] CI：`.github/workflows/ci.yml`（push/PR → 缓存 HF 模型 → 装依赖 → 构建前端 → pytest）。
- [x] 优雅关闭基础：MCP 子进程 `shutdown`、任务队列 `shutdown` 已在 CLI 退出路径接入；uvicorn 侧待补（on_shutdown 钩子）。
- [ ] 多 worker 部署（全局单例 retriever/模型需按 worker 初始化或下沉独立服务）。
- [ ] 反向代理（nginx/caddy）+ HTTPS。数据持久卷/备份策略。
- ⚠️ 本机无 Docker 环境，`docker compose up` 未实测（compose/CI YAML 已做语法校验），首发构建会下载约 600MB 模型。

---

## P2+ · 节点事件真流式（Agent 过程可视化）✅ 已完成

> 图执行过程实时可见：改写查询 → 知识检索 → 意图路由 → 智能应答(ReAct) → 工具执行 → 安全审核，前端顶部「执行步骤」丝带逐条点亮。

- [x] 新增 `src/node_events.py`：节点名 → 中文标签 + 细节描述（Supervisor 路由去向、检索命中数、Agent 工具调用名、工具执行名——按 tool_call_id 回查）。
- [x] `src/stream_chat.py` 重写：**同步 `graph.stream(stream_mode="updates")`** 放 `asyncio.to_thread`，节点事件经 `asyncio.Queue` 喂回 SSE（0.6.x 实测 async `astream` + 同步 interrupt 会抛错且 checkpoint 不落盘；同步 stream 则正确产出 `__interrupt__` 事件 + 落盘）。
- [x] SSE 事件扩展：`{"type":"node","node","label","detail"}`；HITL 挂起仍发 `pending`（内含 draft 供表格渲染）。
- [x] 前端 `api.js` 增加 `onNode` 分发；`App.jsx` 新增「执行步骤」丝带（当前步骤脉冲高亮、完成打 ✓、横向滚动）。
- **踩坑记录**：初版 async `astream` + 同步 interrupt 两处失效（RuntimeError + checkpoint 未写）→ 换同步 stream + 后台线程；另补了「流结束哨兵 __done__」（初版消费循环在最后卡死，90s 超时才暴露）。
- **验收**：✅ 已通过（`tests/test_node_events.py` 6 项纯函数测试；运行中实测：普通对话输出 9 个节点事件 + done；下单对话输出改写/检索/路由节点后正常挂起 pending，且 checkpoint 可被 `/approval/approve` 恢复写库）。

---

## P2+ · HITL 订单人工审批（agent 深度）✅ 已完成

> 超出原清单的新增能力：下单不再由 LLM 直接写库，而是「AI 生成确认单 → LangGraph interrupt 挂起 → 销售人工审批 → 通过才写库」。

- [x] `src/order_agent.py`：拦截 `create_order` 工具调用，组装 draft 后 `interrupt()` 挂起；审批通过才真正调 MCP 写库。
- [x] `src/agent.py`：图编译带 `MemorySaver` checkpointer；新增 `thread_config(user_id)`（thread_id = user_id，HITL 定位会话）。
- [x] `src/approval.py`：待审批注册表 + `pending_reply_text`（客户可见挂起文案）+ `find_pending_draft`。
- [x] `app.py`：`get_state` 挂起态守卫（待审批期间客户再发消息 → 提示待审批，不重跑图）；新增管理端点 `GET /approval/pending`、`POST /approval/approve`、`POST /approval/reject`。
- [x] `stream_chat.py`（与节点流式合并演进）：同步 `graph.stream` 放后台线程经队列喂回 SSE；interrupt 经 `__interrupt__` 事件正确处理（checkpoint 落盘，可审批 resume）；SSE 增加 `pending` 事件（前端已适配）。
- [x] 待审批订单/挂起态存 MemorySaver（进程内）；**生产换持久化 checkpointer（Postgres/Redis）**，重启用 `<thread_id>` 恢复。
- ⚠️ 审批端点未加鉴权（P3 待办）。
- **验收**：✅ 已通过（单测 `tests/test_approval.py` 6 项；运行中端到端：下单→挂起→pending 列表→approve→`ORD-...待付款` 落库 / reject→取消不入库 / 挂起态重复消息守卫 / SSE `pending` 事件）。

---

## P3 · 安全与合规

### 13. 安全
- [x] 鉴权（账号密码登录 + JWT），`/chat` `/history` 保护 —— 2026-09 升级为
      **短期 access + 可轮换 refresh**（详见 8b）。企业微信 OAuth 仍待二期接入
      （`src/auth.py` 的"换证"入口已预留：身份源换掉即可，其余代码不动）。
- [ ] 前端 `addBubble` 去掉 `innerHTML`，改 `textContent` / 白名单渲染（`app.py:107`）。
- [x] **跨租户越权（IDOR）修复（2026-09）**：订单级工具（query_order / query_order_status /
      create_refund）之前**不校验调用者归属**（身份只在提示词里）—— 普通客户可读别人的订单 PII、
      可在别人订单上建退款工单（并把订单推进「退款中」）、可通过提示词注入把订单记到别人名下。
      现在：调用侧用服务端身份**覆盖**注入（`src/order_access.py::with_trusted_identity`，
      三处调度点统一走它），工具侧按 `order_no + customer_id` 查询、缺身份 fail-closed、
      "不存在"与"不属于你"同一句话。回归用例 `tests/test_cross_tenant_access.py`（7 条）。
- [ ] **生产关掉 `/docs`、`/redoc`、`/openapi.json`**：现在公开暴露全部接口清单
      （含 admin 端点），等于把攻击面地图递出去。建议 `FastAPI(docs_url=None, redoc_url=None,
      openapi_url=None)`（或在反代层仅内网放行）。
- [ ] `/healthz` 现在会回 `cache`/`auth_store`/队列指标：确认只在内网或 LB 可达，
      别暴露到公网（信息泄露）。
- [x] 速率限制（Redis 令牌桶，2026-09）：`/auth/login` 同 IP 20 次/分钟、同 账号+IP
      失败 5 次/15 分钟即锁定、`/auth/register` 同 IP 10 个/小时、`/auth/refresh`
      单会话 60 次/分钟；429 + `Retry-After`。判定+扣减是一段 Lua（原子），
      Redis 不可用时**本层 fail-open**（限流是降风险，不是授权判定）。
      `/chat`、`/chat/stream` 也已按 **user_id** 限流（默认 30 条/分钟；SSE 端点在
      **建立流之前**判定，否则 429 会退化成流里的一个事件）。
      诚实交代两条：① 目前只有"次数/分钟"，还没有"并发数上限"和"每日 token 配额"
      （前者要信号量，后者要先埋点统计 token）；后台偏好提取那次 LLM 调用也只受入口
      限流间接约束，没进 `task_queue` 的闸；
      ② `/auth/*` 限流取 TCP 对端 IP、**不信任 X-Forwarded-For**（防伪造绕过），
      反向代理后需改成"只信任已知代理"。
- [ ] 输入长度限制、防 prompt 注入（用户输入与 system prompt 隔离）。
- [ ] 输出编码 + 内容审核（现有 `review_response` 是基础，补 PII 脱敏）。
- [x] 审计日志：谁、何时、调了什么工具、改了什么数据（`audit_log`）。2026-09 补齐
      登录/登出/踢会话/凭证重用检测（`refresh_reuse_detected`）/登录失败与锁定
      （`login_failed`/`login_locked`）/改密码/封号留痕。
- [x] **秒级吊销**（2026-09）：`users.token_version` + 60 秒读穿缓存 → 改密码、
      封号、全端下线时**已签发的 access token 立刻失效**（撤销窗口从 15 分钟压到 ≤60s）。
      接口：`POST /auth/change-password`（验旧密→改哈希→全端下线→当前设备换发新凭证；
      前端弹窗见 `web/src/components/ChangePasswordModal.jsx`，顶栏钥匙图标打开）、
      `POST /auth/logout-all`、`POST /admin/users/{uid}/status`（启用/禁用，禁用即全端下线）。
      开关：`AUTH_REVOCATION_CHECK=0` 可关掉校验，回到"热路径零查询"（撤销窗口退回一个 TTL）。
      **代价要如实说**：这一步把"每 15 分钟一次 IO"变成"每用户每 60 秒一次 IO"，
      绝大多数请求命中进程内缓存；Redis/DB 双故障时 fail-open（靠 access TTL 兜底），
      但**已知版本仍继续生效**——否则打挂 Redis 就能让封号失效。

### 13b. HITL 待审批订单持久化（2026-09 方案 B）✅ 已完成
- [x] 新增 `pending_approvals` 表（状态机 pending|approved|rejected|expired +
      `draft`/`args` JSONB + `expires_at` + `decided_by`/`reason`/`order_no` 审计字段）。
      **背景是真事故**：此前"谁在等审批"只在进程内存（`approval.py` 的 dict +
      LangGraph `MemorySaver`），而"审批通过→真正写订单"只存在于图 resume 路径上 ——
      一次发版/重启就等于：客户已收到"已提交人工审批"，管理员列表却空了，订单永远
      不生成，且**全链路无痕迹**（无异常、无日志、无审计行）。
- [x] `src/approval.py` 改为 DB 状态机：登记幂等（同一 thread 只留一条 pending；
      resume 重放复用刚判定的行，不产生幽灵待审批）、`claim()` **CAS 抢占**
      （并发审批只有一人成功）、`expire_overdue()` 惰性超时、`remove_pending` 按 id
      精确清理。
- [x] 审批端点改为"**CAS 抢占 → 优先图 resume → 兜底直接写单**"：
      服务重启过、没有 checkpoint 时，用登记在表里的 `args` 直接调 MCP `create_order`，
      订单照生成；**成功标准 = 真拿到订单号**（旧实现把"图没抛异常"当成功，
      在"注册表在、checkpoint 没了"时会返回 ok=true 而订单没写 —— 静默假成功）。
- [x] 写单幂等：`orders.client_request_id` 唯一索引 + MCP `create_order` 幂等，
      兜底路径重试也不会重复下单。
- [x] 挂起态守卫与管理员列表改读 PG（`/chat`、`/chat/stream`）：重启后守卫依然生效，
      客户再发消息不会被"从头再跑一遍"掩盖掉丢失。
- **验收**：`tests/test_approval.py` 17 条全绿，含"模拟重启（图必然 resume 失败）后
  仍能审批并真实生成订单"、"拿不到订单号必须报错（假成功回归）"、"并发审批只成功一次"、
  "超时不可批"、"工单幂等"；端到端实测：**真 kill 进程 → 新进程 → 列表仍在 → 审批 →
  订单落库（`resumed=false`，走兜底路径）→ 重复审批被拒**。

### 13c. 管理员数据分析 Agent（2026-09）✅ 已完成
- [x] **前提：数据可信**。先补齐三件事：① 金额列 `REAL`(float4) → `numeric`（实测 14.2 存成
      14.199999809265137）；② 修掉 3 行"列错位"历史订单（`SUM(total)` 从 414 亿 → 119.8 万，
      原值快照写进 audit_log 可回滚）；③ 造数脚本（含**自查**：脚本自己查库打印注入信号，
      不硬编码声称——初版我写死"前 20% 客户贡献多数 GMV"，实测只有 27%）。
- [x] **语义层** `src/analytics/schema_hints.py`：表关系（本库**没有外键**，JOIN 依据只能手写）、
      状态枚举、8 条业务口径（GMV 排除已取消、退款金额只能由被退款订单 total 近似、
      询价转化率可能 >1、**默认不加时间过滤**）、8 条坑（时间列两套类型、"退款看率不看数"）。
- [x] **只读四层防护** `src/analytics/sql.py`：① 独立只读角色（`scripts/create_analytics_role.py`）
      ② 事务只读 + statement_timeout ③ 单语句校验 + 关键字黑名单 + 禁系统表/注释 + 强制 LIMIT
      ④ 行数/单元格封顶；全链路只读，`SqlRejected` 在执行前拒绝。
- [x] **MCP 工具** `analytics_server`（analytics_dict / list_tables / describe_table / run_sql），
      与 product/order/refund 并列；工具**按名字显式授予**，客服 Agent 拿不到分析工具。
- [x] **分析图**（LangGraph）`planner → coder → executor → critic(自纠错≤2) → reporter → charter`；
      charter 是**纯程序节点**：LLM 只选图型/列名，**图表数值由程序从真实结果填**。
- [x] **反编造**：结论里的数字逐个回查结果集，查不到的写进「数据局限声明」（日期/编号/时间
      窗口豁免，保证告警有信噪比）；执行失败**无条件** retry，重试耗尽则如实降级进 notes。
- [x] **端点** `POST /analytics/stream`（SSE，admin-only + 独立限流桶 + 每次写 `audit_log`）
      与 `GET /analytics/examples`；`max_steps` 支持快速/标准两档。
- [x] **前端** 管理端「数据分析」视图：命令面板 + 快捷问题 + 流水线步骤流 + SQL 可折叠 +
      结果表 + ECharts（**懒加载**，主包不背 568KB）+ 结论卡 + 局限声明 + 中止按钮。
- [x] **评测** `src/analytics/eval_cases.py` + `scripts/eval_analytics.py`：7 条用例，
      期望值由**参考 SQL 现算**（数据一变自动跟着变），拦三类回归：口径退化、
      脏数据回退（禁用数字）、只读被绕过。
- **验收**：后端 208 passed / 前端 3 个 node 测试文件；真实 HTTP SSE 端到端（33s 快速模式）；
      **全链路 SSR 渲染**：真实问题 → 只读 SQL → 图表 spec → ECharts 出真 SVG；
      **评测 7/7 全部通过**（平均 29.2s，见 eval_results/eval_analytics.json）。
- **诚实交代**：标准模式一次分析约 2 分钟（延迟由 LLM 输出长度决定，模型侧 ~30 字符/秒）；
      `product_id` 这类列名模型偶尔会编（`p.product_no`），靠 critic 自纠错兜住。

### 13d. 数据完整性治理（2026-09：加外键）✅ 已完成
- [x] **先立不变式**：`users.ensure_user_row()` —— "凡是被接受的身份，users 里必须有行"
      （影子账号：password_hash 为空，永远无法密码登录）。实测当时有 **63 个 user_id**
      在业务数据里出现却没有账号行（`dev_customer` 41 行、`guest` 9 行、`123456`、手机号、
      `eval_user`…）。接入点：写消息/画像/会话/订单/待审批 与 `/dev/login`。
- [x] **清理存量孤儿 313 行**（`scripts/cleanup_orphans.py`，默认 dry-run，先备份到
      `data/orphans_backup_*.json` 再删）：对话 236、画像 25、待审批 2、订单 50。
- [x] **加 7 条外键**（`src/db.py::_apply_foreign_keys`，`NOT VALID` → `VALIDATE` 两步，
      生产不停机范式）：refunds→orders(CASCADE)、orders.product_id→products(RESTRICT)、
      orders.customer_id→users(**RESTRICT：删用户不能带走订单**)、sessions/profile/
      conversations/pending_approvals → users(CASCADE)。
- [x] **刻意不加的两条**：`audit_log.actor`（审计必须能记 system/unknown/已删除账号）；
      `conversations.session_id → sessions.id`（`default` 是**跨用户的历史遗留桶**，
      外键表达不了这个语义 —— 外键要编码真实不变式，不能编码我们希望的不变式）。
- [x] **补 8 个业务索引**：`orders` 之前除主键/唯一键没有任何索引（"我的订单"全表扫描），
      且外键要求在引用列上有索引。
- [x] **语义层收益**：表关系从"手写字符串"改为**运行时读外键**（`schema_hints.load_fk_text()`），
      手写部分只保留外键表达不了的语义关系与业务口径 —— 手写的东西越少越不易漂移。
- [x] **完整性测试** `tests/test_schema_integrity.py`（8 条）：外键存在且已校验、外键真的会拦人、
      RESTRICT/CASCADE 语义正确、public 每张表都在语义层登记、RELATIONS 指向的列真实存在、
      关系能从外键自动读出、库内无孤儿。
- **验收**：后端 **216 passed**（其中 4 条原有测试因新约束需要"先建父行"，已按真实语义修正）；
      开发库 7 条外键 `convalidated=true`；主链路（注册/对话/订单/审批/分析）实测全通。

### 13e. 管理端工作台（2026-09）✅ 已完成

补上管理侧原本缺失的接口（改之前：管理员只能审批下单 + 看用户列表）。

**数据库变更（老库需要执行；`ensure_schema()` 启动时自动幂等执行）**
```sql
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS decided_at TEXT;   -- 审核时间（ISO）
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS decided_by TEXT;   -- 审核人 user_id
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS note       TEXT;   -- 审核备注/驳回理由
```
新库由 `CREATE TABLE refunds` 一次建好。**注意顺序**：ALTER 必须放在 CREATE 之后，
否则空库初始化会 `relation "refunds" does not exist`（开发库因表已存在会掩盖它）。

**新接口（全部 `require_admin`，写操作写 `audit_log`）**
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/orders` | 全站订单：`status` / `keyword` 筛选 + `limit`(≤200) / `offset` 分页，返回 `total` |
| GET | `/admin/orders/summary` | 工作台指标：待审批 / 待发货 / 未付款 / 退款待审 / 累计订单 / 本月 GMV / 近 7 天趋势 |
| POST | `/admin/orders/{order_no}/status` | 状态流转（状态机校验；非法 400、单不存在 404） |
| GET | `/admin/refunds` | 退款工单（`status` 筛选，默认全部） |
| POST | `/admin/refunds/{id}/decide` | 退款审核（CAS：仅 `待审核` 可改，并发下只有一个成功） |

**状态机**（`src/admin_orders.py`）：`待付款→{已付款,已取消}`、`已付款→{已发货,已取消}`、
`已发货→{已收货}`、`已收货/已取消` 为终态。非法流转 400 **且不改库、不写审计**。

**上线注意**
- [ ] 老库执行上面三条 ALTER（或重启服务让 `ensure_schema()` 跑一遍）。
- [ ] 确认 `analytics_ro` 等只读角色**没有**这些写接口的权限（它们走的是业务连接，不受影响）。
- [ ] 退款审核目前只改工单状态 + 审计，**未对接支付/账务**（真实系统需要出账补偿链路）。
- [ ] 无批量发货、无导出（已知缺口）。

---

### 13f. 退单联动（2026-09 第二轮）✅ 已完成

**问题**：退款工单与订单状态互不相干 —— 19 张「已通过」的工单对应订单仍为 已发货/已收货
（¥197,500 退款查不到），分析口径里的"净成交额"无数据可减。

**数据库变更（老库需执行；`ensure_schema()` 启动时自动幂等执行）**
```sql
ALTER TABLE orders  ADD COLUMN IF NOT EXISTS refunded_at TEXT;          -- 退款到账时间
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS order_status_before TEXT;  -- 发起退款时订单的状态
```
⚠️ 两条 ALTER 都必须在对应 `CREATE TABLE` **之后**（空库初始化时表还不存在）。

**新增订单状态**：`退款中`（有未决工单）、`已退款`（审核通过，终态）。
状态机：`待付款/已付款/已发货/已收货 → 退款中`，`退款中 → 已退款`（仅由工单决定）；
驳回退回 `order_status_before`。

**上线步骤**
1. 部署代码（`ensure_schema()` 自动补列）；
2. `python scripts/backfill_refund_linkage.py` 看计划（dry-run）；
3. `python scripts/backfill_refund_linkage.py --apply` 回填存量（单事务 + 写审计）；
4. 核对口径自洽：`GMV − 已退款金额 = 净成交额`，且 `SELECT count(*) FROM orders WHERE status='退款中'`
   应等于待审核工单涉及的订单数（`check_refund_invariants()` 为空）。

**口径（已写入语义层，分析 Agent 会照此回答）**
- GMV = `SUM(total) WHERE status <> '已取消'`（**含** 退款中/已退款）
- 净成交额 = `SUM(total) WHERE status NOT IN ('已取消','已退款')`
- 退款率 = 已退款订单数 / 订单数；退款申请率（投诉口径）= 有工单的订单数 / 订单数
- 退款中敞口 = `SUM(total) WHERE status='退款中'`

**上线注意**
- [ ] 退款审核仍**未对接支付/账务**：只改状态与审计，真实系统需要出账 + 回执 + 对账链路。
- [ ] 无批量审核/批量发货；退款无金额列（金额口径取被退款订单的 `total`）。

---

### 14. 合规
- [ ] 客户电话/地址等 PII 加密存储 + 访问控制。
- [ ] 数据留存周期 + 删除权（`clear_history` 现在只删 conversations，没删 profile/向量）。
- [ ] 与订单/财务相关的操作加幂等（防止重复下单）。

---

## 落地顺序建议

1. **先 P0-1、P0-3、P0-5**：最小的改动量让"多用户不崩、不串数据、响应不被记忆提取拖慢"。
2. 再 **P1-6/P1-7**：换 PG + pgvector，去掉 SQLite/Chroma 单机瓶颈。
3. 然后 **P2-9/P2-12**：异步 + 容器化 + 可观测，准备上线。
4. 最后 **P3**：上线前安全加固。

---

## 需要我继续做什么？

可以从任一条开始实现，例如：
- 先做 **P0-1 多用户隔离**（改 `app.py` 从请求注入 user_id）——改动最小、见效最快；
- 或做 **P1-6 数据库迁移**（SQLAlchemy + Alembic + init_db 脚本）；
- 或先出一份 **Docker + docker-compose** 让整套跑起来。

告诉我从哪条开始。
