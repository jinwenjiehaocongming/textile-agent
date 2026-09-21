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
- 复测 11/11，relevance 5.0 / overall 4.82。

> 运行: `python scripts/eval_judge.py`
> 报告: `eval_results/eval_judge.json`
> 已知偏差：裁判与作答同族模型，存在自评偏差（可用异构 LLM 交叉打分缓解）。

## 4. 数据分析 Agent 评估（9 题，参考 SQL 现算真值 + 多轮稳定性）

一句话中文问题 → 规划 → 只读 SQL → 自纠错 → 结论（数字反编造）→ 图表。
期望值不手写：每条用例带 `reference_sql`，评测时先跑真实库拿 ground truth，
再检查回答里的分类值/数值/禁用数字/安全行为（`scripts/eval_analytics.py`）。

| 阶段 | 通过率 | 说明 |
|------|:--:|------|
| 单次（基线） | 9/9 | 早期"当前 9/9"来自单次抽样，掩盖了波动 |
| **多轮实测（修复前，5 轮）** | **47/54 = 87.0%** | 稳定易错：`inquiry_without_order` 5/5 挂 |
| **多轮实测（修复后，3 轮）** | **26/27 = 96.3%** | 定向复查 3/3；剩余 1 次为 API 抖动下的模型漏算比率 |

**多轮评测暴露并修复的问题（2026-09）**：
1. **`inquiry_without_order` 5/5 全挂（稳定缺陷）**：`products.id` 就是文本产品号
   （`'P0001'`），`orders.product_id` 同格式，直接等值关联即可；但语义层没写，模型
   要么幻觉 `product_no/product_code` 列，要么用 `lpad(id,4,'0')` 拼出 `'PP0001'`
   永不匹配。修复：`schema_hints` PITFALLS 第 11 条写明列不存在 + 禁止拼接。
   修复后 3/3 通过，且耗时从 60-100s 降到 ~40s（不再空转 3 次自纠错）。
2. **`readonly_guard` 误报（判定器 bug）**：模型如实写"不要把 24 条表述为已删除完成"，
   `_claims_completion` 因对冲词表缺"不要"而误判谎称。修复：`_HEDGE` 补
   `不要/别/勿/切勿`，并补上漏检的被动式"已被删除"。修复后 3/3。
3. **`refund_rate_by_color` 恒过假阳性**：原参考 SQL 用**工单口径**，语义层用
   **已退款口径**——模型 8/8 次按语义层答、排名全错，却因 `numbers_check` 的
   ×100 变体 + 15% 容差（`0.1154×100≈11.54` 撞上结论里整数"12"）恒 2/2 判过。
   修复：① 参考 SQL 口径与语义层对齐（已退款口径；工单口径由
   `refund_requests_by_product` 专门覆盖）；② `numbers_check` 收紧——百分比变体
   必须带 `%`，且要求与其分类值 ±60 字符就近。修复后真值与模型数字逐项对上。
4. **API 抖动误伤**：连接错误时整条用例被判 0 分失败（实测 `gmv_total` 一次）。
   修复：对 `APIConnectionError/APITimeoutError` 整条用例重试 1 次。
5. 真实波动仍存在：一次（1/27）模型在 API 持续抖动下只算了退款**笔数**没算**比率**，
   数值检查如实判失败（不是判定器问题）；`critic` 对"要比率却给计数"有时放行，
   可进一步在 CRITIC prompt 里要求"问率必给率"。

> 运行: `python scripts/eval_analytics.py`（`--steps 1` 快速 / `2` 标准；`--no-llm` 只验参考 SQL）
> 报告: `eval_results/eval_analytics.json`；多轮产物 `eval_results/eval_analytics_{baseline,run*,fix_run*,fix_target_*}.json`

## 总览

```
检索管线:       Hit@3 100%  MRR 0.959（混合+Rerank）
端到端规则:     100% (25/25)
LLM-as-Judge:  100%  overall 4.82
数据分析:      9 题多轮 96.3%（修复前 87.0%：易错点已修，见上）
```