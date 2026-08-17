# 纺织 B2B 智能客服 Agent

基于 LangGraph 的多 Agent 纺织企业客服系统，支持产品查询、面料知识问答、下单、售后全流程。

## 特性

- **多 Agent 架构** — Supervisor 三分支路由（售前 / 下单 / 售后），状态机 + LLM 意图分类
- **混合检索 RAG** — ChromaDB 向量 + BM25 关键词 + RRF 融合 + CrossEncoder Rerank
- **结构化产品查询** — SQLite 281 条产品，关键词匹配 + 计分排序
- **完整下单流程** — 查产品 → 展示确认单 → 客户确认 → 写入订单
- **售后处理** — 查订单 → 对照退货规则 → 生成退款工单
- **双层审核** — 规则快速拦截 + LLM 安全审查
- **三层记忆** — Redis 热缓存 + SQLite 持久化 + ChromaDB 偏好提取
- **评估体系** — 检索消融评测 85 题 + 端到端评测 11 题

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 .env（DeepSeek API Key）
echo 'DEEPSEEK_API_KEY=sk-xxx' > .env
echo 'DEEPSEEK_BASE_URL=https://api.deepseek.com/v1' >> .env

# 3. 构建知识库索引
python scripts/build_index.py

# 4. 终端运行
python src/agent.py

# 5. Web 界面
python app.py
# 打开 http://127.0.0.1:8003
```

## 架构

```
入口 → 改写查询 → 知识检索 → Supervisor → 售前 Agent → 审核 → END
                                         → 下单 Agent → 审核 → END
                                         → 售后 Agent → 审核 → END

数据库: products.db (281条面料) + orders.db (订单+退款)
知识库: knowledge.txt (48条结构化chunk)
Embedding: BAAI/bge-base-zh-v1.5 (本地, 免费)
Rerank: BAAI/bge-reranker-base (本地 CrossEncoder)
```

## 评估

| 指标 | 得分 |
|------|:--:|
| 检索 Hit@3 | 100% |
| 检索 MRR | 0.951 |
| 端到端通过率 | 100% (11/11) |

详见 [EVALUATION.md](EVALUATION.md)

## 日志与追踪

日志（logging）与追踪（LangSmith）互补，一个回答「发生了什么」，一个回答「这次请求怎么走的」。

| 机制 | 回答的问题 | 查看方式 |
|------|-----------|---------|
| 日志 | 程序状态、错误、警告（进程级） | `logs/app.log` + stderr |
| 追踪 | 每次请求的执行树、token、耗时（请求级） | LangSmith 网页 |

### 日志

统一配置在 `src/logging_config.py`，各模块用 `get_logger(__name__)` 获取，级别 `DEBUG < INFO < WARNING < ERROR`。

- 输出到 stderr + `logs/app.log`（滚动，5MB × 3 备份）
- 默认 INFO，调详细程度：`LOG_LEVEL=DEBUG python src/agent.py`（或 `.env` 加 `LOG_LEVEL=DEBUG`）
- 排查示例：`grep "Supervisor" logs/app.log` 看路由、`grep -E "WARNING|ERROR" logs/app.log` 看异常

### 追踪（LangSmith）

`.env` 配置 `LANGSMITH_TRACING=true` + `LANGSMITH_API_KEY` + `LANGSMITH_PROJECT`，LLM / 工具调用自动 trace；`review_response`、`retrieve` 用 `@traceable` 手动标注。打开 https://smith.langchain.com 看 trace 树。

排查流程：先用日志定位「哪个环节有问题」，再用 LangSmith 点开该环节看输入输出细节。

## 项目结构

```
src/
├── agent.py              主图 + 售前 Agent
├── order_agent.py         下单 Agent
├── after_sales_agent.py   售后 Agent
├── retrieval.py           混合检索器
├── memory.py             用户记忆系统
├── logging_config.py      统一日志配置
├── mcp_client.py          MCP 客户端 (JSON-RPC over stdio)
└── mcp_servers/           工具服务层 (product/order/refund)
scripts/
├── build_index.py         构建索引
├── eval_retrieval.py      检索消融评测 (85题)
└── eval_agent.py          端到端评测 (11题)
index/                    索引文件 (auto-gen)
data/
├── knowledge.txt          纺织知识库
├── products.db            产品数据库
├── orders.db              订单数据库
└── users/                 用户记忆 (SQLite + ChromaDB)
```
