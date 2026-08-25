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
- [ ] Redis 加连接池（`redis.asyncio`），配置化 host/port/db/password。
- [ ] 修复 `load_recent` 的「Redis 命中即返回、不回补 SQLite」合并 bug（`memory.py:72-100`）。
- **验收**：Redis 清空/部分回写后，历史仍能从 PG 补齐。

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
- [ ] 鉴权（企业微信 OAuth / 登录 token），`/chat` `/history` 保护。
- [ ] 前端 `addBubble` 去掉 `innerHTML`，改 `textContent` / 白名单渲染（`app.py:107`）。
- [ ] 输入长度限制、速率限制（Redis 令牌桶）、防 prompt 注入（用户输入与 system prompt 隔离）。
- [ ] 输出编码 + 内容审核（现有 `review_response` 是基础，补 PII 脱敏）。
- [ ] 审计日志：谁、何时、调了什么工具、改了什么数据。

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
