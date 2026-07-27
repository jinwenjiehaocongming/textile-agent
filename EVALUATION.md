# 评估报告

## 1. 检索评估（50 题）

评估 RAG 管线质量：ChromaDB 向量检索 + BM25 关键词 + RRF 融合 + CrossEncoder Rerank。

| 指标 | 得分 |
|------|:--:|
| Recall@3 | 100% |
| Precision@3 | 85.71% |
| MRR | 1.00 |

> 运行: `python scripts/eval_retrieval.py`
> 报告: `eval_results/eval_retrieval.json`

## 2. 端到端评估（30 题）

20 单轮 + 10 多轮，LLM 裁判打分 1-5 分。覆盖售前、下单、售后、闲聊、安全拦截。

| 指标 | 得分 |
|------|:--:|
| 题目数 | 30（单轮20 + 多轮10） |
| 通过率（≥4分） | 90% (27/30) |
| 平均分 | 4.1 / 5 |

> 运行: `python scripts/eval_v2.py`
> 报告: `eval_results/eval_agent_v2.json`

## 总览

```
检索管线:  Recall@3 100%  Precision@3 85.71%
端到端:    90% 通过率  4.1/5 平均分
```
