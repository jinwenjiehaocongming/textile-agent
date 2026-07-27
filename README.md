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
- **评估体系** — 检索评估 50 题 + 端到端评估 30 题

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
| 检索 Recall@3 | 100% |
| 检索 Precision@3 | 85.71% |
| 端到端通过率 | 90% (27/30) |
| 端到端平均分 | 4.1/5 |

详见 [EVALUATION.md](EVALUATION.md)

## 项目结构

```
src/
├── agent.py              主图 + 售前 Agent
├── order_agent.py         下单 Agent
├── after_sales_agent.py   售后 Agent
├── retrieval.py           混合检索器
└── memory.py             用户记忆系统
scripts/
├── build_index.py         构建索引
├── eval_retrieval.py      检索评估 (50题)
├── eval_v2.py            端到端评估 (30题)
└── test_multi_turn.py    LLM 多轮对话测试
index/                    索引文件 (auto-gen)
data/
├── knowledge.txt          纺织知识库
├── products.db            产品数据库
└── orders.db              订单数据库
```
