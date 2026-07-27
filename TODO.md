# study_1 优化清单

> 按投入产出比排序，做完一个划掉一个

---

## 优先级 P0（半天，面试大加分）

### 1. print → 结构化日志

**改哪里**：`src/agent.py` 所有 `print()` 调用

**现在**：
```python
print(f"\n📝 [检索词] 原文: {last_msg}")
print(f"   → 改写: {reformulated}")
print(f"\n🔍 [检索] 查询: {query}")
print(f"   → 命中 {len(chunks)} 条")
print(f"\n🤖 [Agent] msgs={len(messages)}, chunks={len(chunks)}")
print(f"   🔧 调用: {[(tc['name'], tc['args']) for tc in response.tool_calls]}")
print(f"   💬 回答长度: {len(response.content)} 字")
print(f"\n⚙️ [工具] {name}({args})")
print(f"   ✅ 结果: {result}")
print(f"\n🛡️ [审核] 通过/拦截: ...")
print(f"\n🧭 [Supervisor] → 售前/下单/售后 Agent")
```

**改成**：
```python
import json, time

def log_event(level: str, event: str, **fields):
    print(json.dumps({
        "level": level,
        "event": event,
        "timestamp": time.time(),
        **fields,
    }, ensure_ascii=False))

# 使用示例
log_event("INFO", "query_reformulated", original=last_msg[:100], rewritten=reformulated)
log_event("INFO", "retrieval_done", query=query[:100], hits=len(chunks))
log_event("INFO", "agent_response", msg_count=len(messages), chunk_count=len(chunks), has_tool_calls=bool(response.tool_calls))
log_event("INFO", "tool_call", tool=name, args=args)
log_event("INFO", "supervisor_route", target=target, method=method)
log_event("WARN", "review_blocked", reason=verdict["reason"])
```

**同时修改**：`src/order_agent.py` 和 `src/after_sales_agent.py` 里的 print

**面试说辞**：
> "我把所有 print 换成了 JSON 结构化日志，每条日志带事件名和关键字段，方便之后接入 ELK/Loki 做查询和告警。"

---

### 2. LLM 调用加重试 + 指数退避

**改哪里**：`src/agent.py` 的 `query_reformulator`、`supervisor_node`、`review_response` 三个函数

**加一个工具函数**（放 `src/agent.py` 顶部）：
```python
import time, random

def llm_with_retry(llm, messages, max_retries=2, base_delay=0.5):
    """带指数退避的 LLM 调用"""
    for attempt in range(max_retries):
        try:
            return llm.invoke(messages)
        except Exception as e:
            if attempt == max_retries - 1:
                raise  # 最后一次还失败 → 抛出去
            delay = min(8.0, base_delay * (2 ** attempt) + random.random() * 0.25)
            log_event("WARN", "llm_retry", attempt=attempt, delay=round(delay, 2), error=str(e)[:100])
            time.sleep(delay)
```

**替换三个地方的调用**：
```python
# 改前
resp = cheap_llm.invoke([HumanMessage(content=prompt)])

# 改后
resp = llm_with_retry(cheap_llm, [HumanMessage(content=prompt)])
```

**面试说辞**：
> "LLM 调用加了指数退避重试——网络抖动偶发失败时自动重试 2 次，间隔递增加随机抖动避免同步重试风暴。最多重试 2 次后还失败才抛异常。"

---

### 3. 关键函数加 try-catch 降级

**改哪里**：

#### 3a. `search_product()` （`src/agent.py` 第 106 行）

```python
def search_product(query: str) -> str:
    keywords = [kw.strip().lower() for kw in query.replace("，", " ").replace(",", " ").split() if kw.strip()]
    if not keywords:
        return "请输入产品名、颜色或品类关键词。"

    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        # ... 原有查询逻辑 ...
        conn.close()
        # ... 原有计分排序逻辑 ...
        return "\n".join(lines)
    except Exception as e:
        log_event("ERROR", "search_product_failed", error=str(e)[:200])
        return "产品查询暂时不可用，请稍后重试或联系人工客服。"
```

#### 3b. `_insert_order()` （`src/order_agent.py` 第 137 行）

```python
def _insert_order(...) -> str:
    now = datetime.now()
    order_no = f"ORD-{now.strftime('%Y%m%d')}-{now.strftime('%H%M%S')}"
    try:
        conn = sqlite3.connect(str(ORDERS_DB))
        conn.execute("INSERT INTO orders ...", (...))
        conn.commit()
        conn.close()
        return f"✅ 订单已生成！\n订单号：{order_no}\n..."
    except Exception as e:
        log_event("ERROR", "insert_order_failed", order_no=order_no, error=str(e)[:200])
        return "订单生成失败，请稍后重试。您的信息已记录，我们的销售同事会尽快联系您。"
```

#### 3c. `_create_refund()` （`src/after_sales_agent.py` 第 70 行）

同上，加 try-catch + 降级提示。

**面试说辞**：
> "关键路径加了异常降级——产品查询挂了返回'系统暂时不可用'而不是让整个 Agent 崩溃，订单写入失败保证 rollback + 用户收到提示而不是以为下单成功了。这个在我的故障处理文档里规划过，刚实现了。"

---

## 优先级 P1（半天到一天，锦上添花）

### 4. trace_id 串联请求

**改哪里**：`src/agent.py` 的 `build_graph()` 入口

```python
import uuid
from contextvars import ContextVar

trace_id: ContextVar[str] = ContextVar("trace_id", default="")

# 在 main() 函数中，每次用户输入生成一个 trace_id
tid = str(uuid.uuid4())[:8]
trace_id.set(tid)
```

所有 `log_event()` 调用自动带 `trace_id`，一次请求的日志能串起来。

**面试说辞**：
> "我用 contextvars 给每个请求生成了 trace_id，从改写→检索→Agent→审核的所有日志都带同一个 trace_id，排查问题时能完整还原一次请求的全链路。"

---

### 5. search_product 代码去重

**问题**：`src/agent.py` 和 `src/order_agent.py` 里各有一份 `search_product`

**做法**：新建 `src/tools.py`，把共享的工具函数抽进去：

```python
# src/tools.py
"""共享工具函数"""
import sqlite3
from pathlib import Path

PRODUCTS_DB = Path(__file__).parent.parent / "data" / "products.db"

def search_product(query: str) -> str:
    # 只此一份，agent.py 和 order_agent.py 都 import 这个
    ...
```

两个文件删掉自己的 `search_product`，改为 `from src.tools import search_product`。

**面试说辞**：
> "之前 search_product 在 agent.py 和 order_agent.py 里写了两份，我把它抽成共享的 tools.py 了，消除重复代码。"

---

### 6. 流式输出

**改哪里**：`src/agent.py` 的 `agent_node()`、`src/order_agent.py`、`src/after_sales_agent.py`

```python
# 改前
response = llm.invoke(messages)

# 改后
chunks = []
for chunk in llm.stream(messages):
    chunks.append(chunk)
    yield chunk  # SSE 推给前端
response = chunks  # 完整内容
```

`app.py` 也要配合改——FastAPI 用 `StreamingResponse`。

---

## 不改的（演示项目不需要）

| 为什么不改 | 
|---|
| ❌ 三态熔断器 — 单模型没备选，熔断了没地方切 |
| ❌ 模型路由 — 只有一个 DeepSeek，先不加 |
| ❌ Docker/K8s — 部署相关，面试口头讲就行 |
| ❌ 语义缓存 — 281 条产品太少，缓存收益不明显 |
| ❌ Redis — SQLite 够用，不需要引入新依赖 |

---

## 做完后的面试加分点

```
[ ] 结构化日志 → "我做了可观测性" ✅
[ ] LLM 重试 + 退避 → "我做了容错" ✅
[ ] 关键函数 try-catch → "我做了降级" ✅
[ ] trace_id 串联 → "全链路追踪" ✅
[ ] 代码去重 → "代码质量" ✅
```
