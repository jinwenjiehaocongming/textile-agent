# 故障处理手册

## 1. LLM API 挂了（DeepSeek / 千问）

当前：直接抛异常，Agent 崩溃。

优化：
- 重试 2 次（间隔 1s、2s）
- 还挂 → 切备选 API（千问→DeepSeek 互相备）
- 还挂 → 静态话术兜底："系统繁忙，稍后重试"

成本：代码加一个 retry decorator，0 额外费用。

## 2. Embedding API 挂了（ChromaDB 查询用）

当前：当前已用本地 bge-base-zh-v1.5 模型，不依赖 API ✅

## 3. ChromaDB 挂了

当前：直接报错。

优化：
- catch 异常 → knowledge_chunks 返回空列表
- Agent 只用自身知识 + 产品查询工具回答
- 回复降级：只报价咨询，不说面料知识（"关于工艺参数建议咨询人工"）

成本：加 try-catch，0 额外费用。

## 4. SQLite 挂了（products.db / orders.db）

当前：直接报错。

优化：
- 产品查询：catch 异常 → 返回 "产品查询暂时不可用，请稍后重试"
- 订单：catch 异常 → 返回 "订单服务暂时不可用"
- Agent 收到降级回复继续兜底话术

成本：加 try-catch。

## 5. CrossEncoder Rerank 挂了

当前：模型加载失败直接报错。

优化：
- 模型加载失败 → 跳过 Rerank，直接用 RRF 融合结果（你本来就有 RRF）
- 效果：Precision 从 70% 降到 ~60%，但系统不崩

成本：加 try-catch，fallback 到纯 RRF。

## 6. DeepSeek API 限流

当前：直接崩。

优化：
- 请求排队：Redis 队列缓冲，控制并发
- 返回 429 → 退避 2s 后重试
- 高峰期：关 Rerank、审核只做规则层

成本：需要 Redis。

## 7. 用户量暴涨（1000 人同时问）

当前：一个 Agent 实例，排队阻塞。

优化：
- FastAPI 本身支持并发请求
- 每个请求独立 state，互不阻塞
- 瓶颈在 DeepSeek API 的 QPS 限制
- 多买几个 API Key 轮询分发

成本：多个 API Key。

## 8. 下单后数据库写入失败

当前：create_order 写了 commit()，失败抛异常。

优化：
- 写入失败 → rollback → 返回 "订单生成失败，请稍后重试"
- 记录失败日志（发告警给运维）
- 客户不会以为下单成功了其实没写进去

成本：加 try-catch + rollback。

## 9. 内存/DB 满了（长期运营）

当前：无清理机制。

优化：
- 对话历史定期归档（超过 30 天的移到冷存储）
- ChromaDB 偏好去重（同一用户记忆上限 50 条）
- SQLite 定期 VACUUM 回收空间

成本：定时任务。

## 优先级

| 优先级 | 优化点 | 原因 |
|--------|--------|------|
| ⭐⭐⭐ | LLM 重试+降级 | 最核心依赖 |
| ⭐⭐⭐ | 产品订单查询 try-catch | 直接面向客户 |
| ⭐⭐ | Rerank 降级 | 挂了只是精度降 |
| ⭐⭐ | ChromaDB 降级 | 很少挂 |
| ⭐ | 限流/并发 | 小规模不用在意 |
| ⭐ | 磁盘清理 | 很久以后的事 |
