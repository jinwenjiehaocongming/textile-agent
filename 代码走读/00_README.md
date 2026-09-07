# 00 · 这套笔记怎么用

> 纺织 B2B 交易智能体——二面代码走读笔记。
> 形式：**一步一步 + 每步配问答**，从注册登录一路走到 Docker 部署，
> 覆盖全部核心代码，git / docker / 日志 / 测试都不落下。

---

## 一、阅读方法（先读这个）

1. **顺序读**：从 01 到 18，编号即学习顺序，每一步都建立在上一步之上。
2. **每步结构固定**：
   - `先建立心智模型` —— 这一节在讲什么、和别处什么关系（表格）
   - `逐段拆解` —— 挑真正的代码逐行翻译成人话，每个"黑话"都给解释
   - `类比` —— 用生活场景帮助记忆
   - `Q&A` —— 每步配 3~6 个问题 + 详细答案（含面试追问点）
3. **对照代码读**：笔记里所有代码都标注了文件与行号（如 `src/auth.py:55`），
   请打开项目原文对照着看——**笔记是地图，代码才是土地**。
4. **先骨架后血肉**：第一遍只读每节的"心智模型"和"Q&A"，第二遍再逐行抠。
5. 配套材料：
   - `面试/` 文件夹：现成的面试题整理（八股 + 话术），本套笔记负责"真的读懂代码"，
     两份互补：先走读本笔记 → 再背 `面试/04_项目面试题详解.md` 之类。
   - 仓库自带文档：`README.md`（概览）、`ARCHITECTURE.md`、`docs/UNDERSTANDING.md`（架构从零讲解）、
     `docs/LEARNING.md`（上线改造复盘）、`WEB_UI_STREAMING.md`（流式设计旧版）、`EVALUATION.md`。

## 二、全 18 步目录地图

| 编号 | 文件 | 覆盖 | 关键代码 |
|----|------|------|---------|
| 00 | 本文件 | 使用说明 | — |
| 01 | 项目总览 · 技术栈与文件地图 | 这项目是什么、目录、主流程预览 | README、全目录 |
| 02 | 注册登录 · JWT 鉴权全链路 | 账号体系、密码哈希、JWT、认证/授权 | `users.py` `auth.py` `app.py` |
| 03 | 读代码的基本功 | async/await、装饰器、Depends、占位符、yield | 语法速查 |
| 04 | 服务启动 · FastAPI 入口与 MCP 初始化 | lifespan、MCP 子进程、CORS、静态托管、/api 兼容 | `app.py` `mcp_client.py` |
| 05 | 多会话与聊天记录 | sessions 表、CRUD、消息落库、default 兼容 | `sessions.py` `db.py` |
| 06 | 第一条消息的旅程 · SSE 与事件流 | StreamingResponse、事件协议、Queue、token 流 | `stream_chat.py` `token_stream.py` `llm_utils.py` |
| 07 | Agent 主图 · Supervisor 路由与审核 | 图结构、节点、双层审核、checkpointer | `agent.py` `node_events.py` |
| 08 | 下单流程 · HITL 人工审批 | interrupt 挂起、审批恢复、订单落库 | `order_agent.py` `approval.py` |
| 09 | 售后流程 · 订单与退款 | 查单、退货规则、退款工单 | `after_sales_agent.py` `mcp_servers/refund_server.py` |
| 10 | 混合检索 RAG | Qdrant+BM25+RRF+Rerank、索引构建 | `retrieval.py` `vector_store.py` `scripts/build_index.py` |
| 11 | MCP 工具服务器与官方 SDK | FastMCP 三 server、ClientSession、生命周期 | `mcp_client.py` `mcp_servers/*` |
| 12 | 记忆系统与用户隔离 | 三层记忆、偏好提取、user_id 行级隔离 | `memory.py` `user_identity.py` |
| 13 | 前端 React 与 API 对接 | 会话门禁、SSE 消费、token 存储、玻璃 UI | `web/src/*` |
| 14 | 测试与评测体系 | pytest 覆盖、检索消融、端到端、LLM-as-Judge | `tests/` `scripts/eval_*.py` |
| 15 | 日志与 LangSmith 追踪 | logging 配置、滚动文件、trace | `logging_config.py` |
| 16 | Docker 部署与 CI | 镜像、compose、entrypoint、健康检查、流水线 | `Dockerfile` `docker-compose.yml` `ci.yml` |
| 17 | Git 演进复盘 | 从 SQLite/Chroma 到 PG/Qdrant 的每一步动机 | `git log` |
| 18 | 高频追问与坑清单 | 面试官会追问的点和诚实的工程取舍 | 汇总 |

## 三、一页速览：整个系统在干什么（背熟这个）

```
客户消息
  → ① 改写查询（rewrite）
  → ② 混合检索（Qdrant 向量 + BM25 关键词 + RRF 融合 + Rerank）
  → ③ Supervisor 意图路由（售前 / 下单 / 售后 / 闲聊）
  → ④ 对应 Agent 干活（查产品 / 写订单 / 查单退款）
  → ⑤ 下单到"人工审批"时 interrupt 挂起（HITL）
  → ⑥ 所有回复过双层审核（规则 + LLM）
  → ⑦ SSE 逐字推回前端
```

存储一句话：**业务数据 → PostgreSQL；知识 → Qdrant + BM25 索引；对话记忆 → PG + Qdrant 偏好；LLM → DeepSeek；工具 → 官方 MCP SDK 子进程。**
