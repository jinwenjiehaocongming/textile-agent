# 三层记忆系统

## 为什么是三层

Agent 同时面临三个矛盾需求：
- **快** — 每次调 LLM 都要传历史，不能每次都读数据库
- **持久** — 用户下线回来不能丢记忆，不能只靠进程内存
- **智能** — 客户半年没来，Agent 还得记得他是做羽绒服的

单层解决不了，所以三层各管各的：

```
用户发消息
  ↓
① 热缓存 (Redis / 内存 dict)
  → 毫秒级读取最近 50 轮对话
  → 塞进 state["messages"] 发给 LLM
  ↓ 对话结束

② 持久化 (PostgreSQL / SQLite)
  → 全部消息写入，永久不丢
  → 下次登录从这里加载到 Redis

③ 向量记忆 (ChromaDB)
  → 异步提取关键信息
  → "做羽绒服的""只要黑色" → 向量入库
  → 下次登录检索 → 注入 system prompt
```

## Layer 1 — 热缓存（你现在是 state["messages"]）

```python
# 现在（单机内存在 state 里）
state["messages"]  # Python dict，重启就没了

# 生产（Redis）
redis.lrange(f"chat:{user_id}", -50, -1)  # 毫秒级返回最近 50 条
```

为什么用 Redis 而不是 Python dict：
- 多台服务器共享同一份缓存
- 进程重启数据不丢
- 自带 TTL 自动过期（1 小时没说话自动清）
- 读写都在微秒级

## Layer 2 — 持久化（你现在是 SQLite）

```python
# memory.py: save_messages()
INSERT INTO conversations (role, content, created_at) VALUES (...)
```

面试时：SQLite → PostgreSQL 换一下名字。PG 支持全文搜索、主从复制、千万级数据不卡。

## Layer 3 — 向量记忆（你现在是 ChromaDB）

这才是面试官最想听的——**你记住了什么，而不是记了多少**。

```python
# memory.py: extract_and_store()
# 每轮对话后异步跑：
LLM 扫聊天 → "这个客户是做羽绒服的" → embed → 存 ChromaDB

# 下次登录：
检索 "偏好" → ["做羽绒服的", "只要黑色"] → 注入 system prompt
→ Agent: "根据您的业务方向，推荐做羽绒服的380T尼丝纺..."
```

## 三层协作流程

```
客户上线
  → ChromaDB 检索偏好 → "做羽绒服外贸，偏好黑色<15块"
  → SQLite 加载最近 20 轮对话
  → Redis 缓存这些数据（下次直接命中，不读库）

客户发 "T400黑色多少钱"
  → Agent 从 system prompt 知道客户是做羽绒服的
  → 从 Redis 拿到对话历史
  → 回复

客户发 "帮我下单"
  → 下单 Agent 从历史中提取电话地址（在 Redis 里）
  → 创建订单

客户下线
  → 新消息写入 SQLite（永久）
  → 后台异步提取偏好 → ChromaDB
  → Redis TTL 倒计时 1h → 过期清空
```

## 面试话术

"三层记忆：Redis 做热缓存保证速度，PostgreSQL 做持久化保证不丢，ChromaDB 做结构化记忆保证智能。

区别：前两层存的是 '客户说了什么'，第三层存的是 '客户是什么样的人'。

第一层是 state["messages"] 的独立进程版——把对话历史从 LangGraph 内部搬到外部 Redis，保证多机共享和重启不丢。"
