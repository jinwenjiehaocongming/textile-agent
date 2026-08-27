# 评估报告

## 1. 检索评估（85 题，4 组消融，142 块语料）

评估 RAG 管线质量：Qdrant 向量检索 + BM25 关键词 + RRF 融合 + CrossEncoder Rerank。
对比 4 种配置，验证「混合检索 + Rerank」的价值。

| 方法 | Hit@3 | MRR |
|------|:--:|:--:|
| ① 纯向量 | 82.4% | 0.776 |
| ② 纯 BM25 | 95.3% | 0.835 |
| ③ 混合 (RRF) | 96.5% | 0.849 |
| ④ 混合 + Rerank | 100% | 0.959 |

> 运行: `python scripts/eval_retrieval.py`
> 报告: `eval_results/eval_retrieval.json`

## 2. 端到端评估（25 题，规则断言）

覆盖售前、下单、售后、闲聊、安全拦截五类场景，规则断言判断通过与否。
（下单用例已演进为 HITL 感知：评测中自动走「审批通过」分支再断言订单号。）

| 指标 | 得分 |
|------|:--:|
| 题目数 | 25 |
| 通过率 | 100% (25/25) |

> 运行: `python scripts/eval_agent.py`
> 报告: `eval_results/eval_agent.json`

## 3. LLM-as-Judge 评估（四维评分）

LLM 裁判对回答打 4 维分（相关性/完整性/事实一致性/安全合规，各 1-5 分），
通过标准：`overall ≥ 4 且 factual ≥ 4 且 safety ≥ 4`。

| 指标 | 得分 |
|------|:--:|
| Judge 通过率 | 100% |
| relevance 均分 | 5.00 |
| completeness 均分 | 4.55 |
| factual 均分 | 5.00 |
| safety 均分 | 5.00 |
| overall 均分 | 4.82 |

**评测驱动改进闭环（重要）**：
- 首轮 Judge 10/11——裁判抓住规则断言漏掉的质量问题：「知识问答」回答只写
  「以上为……现货」过度依赖表格，纯文本场景下答非所问。
- 据此改进 prompt：① 知识/推荐类问题先用检索知识给出结论，不急着调工具报价；
  ② 正文不得依赖表格，必须自带完整信息。
- 复测 25/25，relevance 5.0 / overall 4.82。

> 运行: `python scripts/eval_judge.py`
> 报告: `eval_results/eval_judge.json`
> 已知偏差：裁判与作答同族模型，存在自评偏差（可用异构 LLM 交叉打分缓解）。

## 总览

```
检索管线:       Hit@3 100%  MRR 0.959（混合+Rerank）
端到端规则:     100% (25/25)
LLM-as-Judge:  100%  overall 4.82
```