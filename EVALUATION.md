# 评估报告

## 1. 检索评估（85 题，4 组消融，142 块语料）

评估 RAG 管线质量：ChromaDB 向量检索 + BM25 关键词 + RRF 融合 + CrossEncoder Rerank。
对比 4 种配置，验证「混合检索 + Rerank」的价值。

| 方法 | Hit@3 | MRR |
|------|:--:|:--:|
| ① 纯向量 | 100% | 0.945 |
| ② 纯 BM25 | 95.3% | 0.835 |
| ③ 混合 (RRF) | 100% | 0.941 |
| ④ 混合 + Rerank | 100% | 0.951 |

> 运行: `python scripts/eval_retrieval.py`
> 报告: `eval_results/eval_retrieval.json`

## 2. 端到端评估（11 题）

覆盖售前、下单、售后、闲聊、安全拦截，规则断言判断通过与否。

| 指标 | 得分 |
|------|:--:|
| 题目数 | 11 |
| 通过率 | 100% (11/11) |

> 运行: `python scripts/eval_agent.py`
> 报告: `eval_results/eval_agent.json`

## 总览

```
检索管线:  Hit@3 100%  MRR 0.951（混合+Rerank）
端到端:    100% 通过率（11/11）
```
