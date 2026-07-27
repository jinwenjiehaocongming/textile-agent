# 纺织 B2B 智能客服 — 系统架构图

## 整体架构

```
┌─────────────────────────────────────────────────────────┐
│                    用户入口                              │
│              终端 / 企业微信（未来）                      │
└────────────────────┬────────────────────────────────────┘
                     │  "羽绒服用什么面料"
                     ▼
┌─────────────────────────────────────────────────────────┐
│                 LangGraph Agent 图                       │
│                                                         │
│  ┌─────────────┐                                        │
│  │ ① 改写查询  │  cheap_llm (qwen-turbo 免费)           │
│  │             │  "羽绒服面料推荐"                       │
│  └──────┬──────┘                                        │
│         ▼                                               │
│  ┌─────────────┐                                        │
│  │ ② 知识检索  │  ChromaDB(向量) + BM25(关键词)          │
│  │             │  ↓ RRF 融合(0.5/0.5)                   │
│  │             │  ↓ CrossEncoder Rerank(去10取5)         │
│  └──────┬──────┘                                        │
│         ▼                                               │
│  ┌─────────────┐                                        │
│  │ ③Supervisor │  判断意图：售前 / 下单                   │
│  └──┬──────┬───┘                                        │
│     │      │                                            │
│     │      └────── 下单 ──────────┐                     │
│     ▼                             ▼                     │
│  ┌──────────────┐    ┌──────────────────────┐          │
│  │ ④a 售前Agent │    │ ④b 下单Agent (独立)   │          │
│  │  llm = deep- │    │  llm = deepseek-chat  │          │
│  │  seek-chat   │    │  工具：               │          │
│  │              │    │  · search_product     │          │
│  │  工具：       │    │  · create_order       │          │
│  │  · search_   │    │                      │          │
│  │    product   │    │  流程：               │          │
│  │  · query_    │    │  查产品→确认单→下单    │          │
│  │    order_    │    │                      │          │
│  │    status    │    │                      │          │
│  └──────┬───────┘    └──────────┬───────────┘          │
│         │                       │                       │
│         │  有工具调用 → 循环      │                       │
│         ▼                       │                       │
│  ┌──────────────┐               │                       │
│  │ ⑤ 工具执行   │               │                       │
│  │  search_prod │               │                       │
│  │  query_order │               │                       │
│  └──────┬───────┘               │                       │
│         │                       │                       │
│         └──── 无工具调用 ────────┼────┐                  │
│                                 ▼    ▼                  │
│                       ┌─────────────────┐               │
│                       │ ⑥ 审核 (必选)    │               │
│                       │ ① 规则: 成本价等  │               │
│                       │ ② LLM: 安全+事实  │               │
│                       │  cheap_llm        │               │
│                       └────────┬────────┘               │
│                                ▼                        │
│                          用户看到回复                     │
└─────────────────────────────────────────────────────────┘
```

## 数据流

```
用户 "T400黑色多少钱"
  │
  ├─→ ① query_reformulator
  │   输入: 历史 + "T400黑色多少钱"
  │   输出: rewrite_query = "T400黑色价格 | T400黑色现货"
  │   模型: qwen-turbo (免费)
  │
  ├─→ ② context_retriever
  │   输入: rewrite_query
  │   过程: ChromaDB向量检索(embedding: text-embedding-v4)
  │         + BM25关键词检索(bigram+IDF²)
  │         → RRF融合(k=60, w=0.5/0.5)
  │         → CrossEncoder Rerank(10取5)
  │   输出: knowledge_chunks (5条)
  │
  ├─→ ③ supervisor_node
  │   判断: should_create_order() → "place_order" or "chat"
  │
  ├─→ ④a agent_node (售前)
  │   system_prompt = "你是宏润纺织客服" + knowledge_chunks
  │   工具: [search_product, query_order_status]
  │   LLM: deepseek-chat
  │   ReAct循环: agent ⇄ tool_executor (最多N轮 → 审核)
  │
  ├─→ ⑤ tool_executor
  │   search_product → SQLite products.db
  │   query_order_status → SQLite orders.db
  │
  └─→ ⑥ review_node
      规则: 成本价/底价 → 拦截
      LLM: 安全 + 事实校验(知识库交叉验证)
```

## 下单路径

```
Supervisor 判为 "place_order"
  │
  ├─→ ④b order_agent_node
  │   独立 ReAct 循环:
  │     Round 1: search_product → 查到产品详情
  │     Round 2: 展示确认单 → 等客户确认
  │     Round 3: 客户确认 → create_order → 写入 orders.db
  │
  └─→ ⑥ review_node (与售前共用)
```

## 存储

```
data/
├── products.db        SQLite, 281条产品, 10字段
├── orders.db          SQLite, orders + refunds 表
├── knowledge.txt      纺织知识库, 48条结构化chunk
└── users/ (待建)      用户档案 + 对话历史

index/
├── chroma_db/         向量索引 (text-embedding-v4)
└── bm25_index.pkl     BM25稀疏索引

~/.cache/huggingface/  CrossEncoder模型缓存 (1.1GB)
```

## 外部依赖

```
DeepSeek API    → Agent对话 (deepseek-chat) + 下单Agent
DashScope API   → Embedding (text-embedding-v4)
                 → 改写 + 审核 (qwen-turbo, 免费)
本地            → CrossEncoder Rerank (离线, 毫秒级)
```

## 成本估算

```
每次对话:
  改写      qwen-turbo    免费
  检索      embedding     免费额度
  Rerank    本地CPU       0元
  审核      qwen-turbo    免费
  Agent     deepseek      ~¥0.002 (输入+输出约2000 token)

下单流程额外:
  下单Agent deepseek      ~¥0.005 (多次工具调用)

月成本 (日均100轮): 约 ¥20-30
```
