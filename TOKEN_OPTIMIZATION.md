# Token 优化 — 面试高频考点

## 你已经做的

| 优化 | 省多少 | 在哪 |
|------|:--:|------|
| 闲聊跳过检索 | 每次闲聊省 1 次改写 LLM + 1 次 embedding + 3 次 Rerank | `should_skip_retrieval` |
| CrossEncoder 替代 LLM Rerank | Rerank 从 10 次 API → 1 次本地 | `retrieval.py` |
| 便宜模型做非核心任务 | 改写/审核/Supervisor 原本每条 2000 token | `cheap_llm`（但千问欠费了） |
| Top 5 而不是 Top 10 | Agent 每次少吃 5 条 chunk 的 token | `context_retriever` |

## 还能做的

### 1. 对话窗口限制（最重要）

你现在把全部历史发给 LLM。客户聊了 100 轮后，每次输入 token 爆炸式增长。

```python
# 现在
messages = state["messages"]  # 全量，越来越长

# 优化：只发最近 N 轮 + 早期摘要
recent = messages[-20:]  # 最近 20 轮，完整保留
old_summary = llm.summarize(messages[:-20])  # 更早的压缩成一段摘要
final = [summary_msg] + recent  # 发给 LLM
```

效果：100 轮对话的输入 token 从 50000 降到 8000。

### 2. System Prompt 瘦身

你现在 system prompt 约 200 字 + 检索到的知识 chunks（5 × 500 = 2500 字）≈ 2700 字。

```python
# 优化：知识 chunks 不是越多越好
# Top 5 → Top 3，相关性已经足够
results = retriever.retrieve(query, top_k=3, use_rerank=True)
```

### 3. 工具描述精简

你现在 `search_product` 的 JSON Schema description 约 80 字。每次 LLM 调用的 tools 字段都带着。多个工具就是几百 token。

```python
# 优化：description 写到 "查询产品价格和库存" 就够了
# LLM 不需要知道 "用于客户询问价格、库存、MOQ、交期，按名称/颜色搜索"
```

### 4. 缓存 LLM 响应

同一个问题 30 秒内再问 → 不调 LLM，直接返回缓存。

```python
cache = {}  # {query_hash: (response, timestamp)}
def cached_llm(query):
    key = hash(query)
    if key in cache and time() - cache[key][1] < 30:
        return cache[key][0]
    resp = llm.invoke(query)
    cache[key] = (resp, time())
    return resp
```

### 5. Prompt Cache / KV Cache

DeepSeek 和 GPT 都支持——重复的 system prompt 部分只计费一次。你不需要改代码，LLM 自动节省。你的日志里 `cached_tokens: 1024` 就是已经生效了。

### 总结

| 优先级 | 优化项 | 省 token | 实现难度 |
|:--:|------|:--:|:--:|
| ⭐⭐⭐ | 对话窗口限制 | 80% | 中 |
| ⭐⭐ | Top 5 → Top 3 | 20% | 1 行 |
| ⭐⭐ | 工具描述精简 | 5% | 1 行 |
| ⭐ | 响应缓存 | 视情况 | 简单 |
| ✅ | Prompt Cache | 自动 | 免费 |
