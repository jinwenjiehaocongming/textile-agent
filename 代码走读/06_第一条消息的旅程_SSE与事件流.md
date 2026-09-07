# 06 · 第一条消息的旅程 · SSE 流式与事件管道

> 目标：看懂"你在聊天框发一句话，到 AI 逐字回复"，浏览器-服务器之间到底发生了什么。
> 覆盖 StreamingResponse、SSE 事件协议、asyncio.Queue 生产者-消费者管道、真 token 流。
> 这是本项目最亮的机制之一，也是面试"画图题"重灾区。

---

## 一、先建立心智模型

**为什么聊天接口长得和登录不一样？**

| | 登录/注册（普通接口） | 聊天（SSE 接口） |
|---|---|---|
| 交互 | 一来一回 | 一问、多推 |
| 返回 | `return {...}` 一个值，连接关 | `StreamingResponse`，**连接挂着** |
| 关键 | 一次给全 | 服务器**算一点、推一点**（yield） |
| 协议 | JSON | `text/event-stream`（SSE） |

AI 要想几秒甚至几十秒，且一个字一个字生成。等全部想完再一次给 → 用户盯着白屏以为
死机。SSE = **电话（不挂）** vs 对讲机（说完就挂）。前端用 `fetch` 拿到
`resp.body.getReader()` **循环读流**，来一段渲染一段（web/src/api.js）。

## 二、两条聊天接口：/chat 与 /chat/stream

- `POST /chat`（app.py:92）：老版/简单版——整包等结果，一次 return。
- `POST /chat/stream`（app.py:314）：SSE 真流式，前端用的这个（web/src/api.js:136）。

两条都做同一件事，stream 版多了"边跑边推"。后端入口（app.py:314）：

```python
@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, user: dict = Depends(get_current_user)):
    user_id = user["user_id"]
    session_id = await _resolve_session(user_id, req.session_id)  # 会话归属校验（05 步）
    memory = get_user(user_id)          # 拿该用户"记忆管家"（12 步）

    async def event_gen():              # ← 生成器：挤牙膏的地方
        yield "data: {\"type\": \"start\"}\n\n"        # 先推"连接就绪"
        async for evt in stream_chat(req.message, memory, agent_graph, get_cheap_llm(),
                                     user_id=user_id, session_id=session_id):
            yield f"data: {_json.dumps(evt, ensure_ascii=False)}\n\n"  # 转 SSE 格式

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", ...})
```

前三行你已经认识：`Depends(get_current_user)` = 门禁验 token；`_resolve_session` =
确认这个会话属于你（行级隔离）。**新概念只有一个 `StreamingResponse` + 生成器**
（语法见 03 步第五节）。

SSE 线上格式（浏览器按此解析）：

```
data: {"type": "node", "node": "retrieve", ...}

data: {"type": "token", "content": "你好"}
```

即 `data: ` + JSON + 两个换行，一条条推。

## 三、事件协议：6 类事件（stream_chat.py 顶部注释即契约）

```python
{"type": "start"}                          ① 连接就绪（前端清空输入进入等待）
{"type": "node", node, label, detail}      ② 图跑到哪个节点（可多个，过程可视化）
{"type": "token", "content": str}          ③ AI 逐字生成的字（可多个，打字机）
{"type": "done", "content": str}           ④ 完整回复（权威文本，收尾）
{"type": "pending", ...}                   ⑤ 订单挂起等审批（HITL，08 步）
{"type": "error", "content": str}          ⑥ 异常
```

顺序：start →（node / token 交错）→ done 收尾；中途可能 pending / error。
**前端侧栏"节点逐个亮 + 底部打字机"就是 ②③ 两个事件的功劳。**

## 四、stream_chat 核心：asyncio.Queue 生产者-消费者管道

问题：跑图的人和推给浏览器的人是**两个并行协程**，快慢不定，不能直接互相调用。
解法：中间放一条管道 `asyncio.Queue`，生产者只管塞、消费者只管取——解耦。

```python
dst_queue = asyncio.Queue(maxsize=500)     # 一条消息管道（含背压上限）

def emit(kind, payload):                   # 往管道塞（token 用，put_nowait 不阻塞）
    dst_queue.put_nowait((kind, payload))

async def _run_graph():                    # 生产者：后台跑图
    set_token_pusher(lambda text: emit("token", text))        # ① 注册"AI蹦字→塞管道"
    async for ev in graph.astream(state, config, stream_mode="updates"):
        await dst_queue.put(("node_event", ev))   # ② 每跑完一个节点→塞管道
    await dst_queue.put(("__done__", None))      # ③ 跑完→塞"结束哨兵"

runner = asyncio.create_task(_run_graph())      # ④ 后台协程跑图，不阻塞主流程

# ⑤ 消费者（主生成器）：从管道取事件 → 转 SSE 推给浏览器
while True:
    kind, payload = await dst_queue.get()
    if kind == "__done__": break                  # 哨兵：没货了，退出
    if kind == "token":
        yield {"type": "token", "content": payload}
    if kind == "node_event":                      # 节点事件再拆成 node 级 SSE
        for node, update in ev.items():
            yield {"type": "node", "node": node, "label": NODE_LABELS.get(node, node), ...}
```

**面试点：为什么节点状态和 token 共用同一条 Queue？**
1. **天然全局有序**：谁先发生谁先出队，绝不出"回答推完了、节点还在亮"的乱序；
   若拆两个队列还得自己同步。
2. 消费端统一：一个 `while get()` 处理所有事件类型。
3. 结束也走队列发哨兵（`__done__`/`__error__`），消费者见哨兵即知"不会再有货"。
4. `maxsize=500` = **背压**：防止生产太快撑爆内存；塞满时 `await put` 会等待
   （节点事件），`put_nowait` 会抛错（token 回调是同步触发，不能阻塞，见 Q4）。

## 五、真 token 流：AI 的字怎么"蹦"出来的

三个文件一条链，每个只干一件事：

```
① src/token_stream.py    ContextVar 里放一个 push 回调（线程内"传话筒"，
                          顺着调用链传播，不用每个函数显式传参）
② src/llm_utils.py       _safe_llm_async(..., stream_tokens=True) 时不走 ainvoke
                          （等全量），改走 astream（逐块来）：
                          async for chunk in llm.astream(messages):
                              if chunk.content: pusher(chunk.content)   # 蹦一块推一块
                              acc = chunk if acc is None else acc + chunk  # 同时累加
③ src/stream_chat.py     set_token_pusher(lambda text: emit("token", text))
                          把"喊的一嗓子"接到管道 → 主循环 yield 给浏览器
```

用大白话：**LLM 本身一个字一个字生成，astream 能拿到中间过程，拿到一小块立刻转发
给浏览器**，前端按序拼起来就是打字机效果。三个加分细节：

1. **只在"最终用户可见回复"调用点开流**（llm_utils.py 模块注释）：工具调用那几轮的
   chunk 内容为空，天然不推字——用户看到的打字永远是最终答案，不会被中间过程打扰。
2. 流中断**回落普通 invoke**（`except: return ainvoke(...)`）：宁可慢，不能丢。
3. `stream_mode="updates"`：LangGraph 每完成一个节点吐一次更新 → 节点事件来源。

## 六、收尾：done 事件 + 存档（stream_chat.py:150-190）

流结束后：
- `_extract_final_reply`：从图最终状态 messages 里**倒着找第一条非空 AI 消息**，
  作为权威完整文本发 `done`（前端收到它就知道流结束、以它为准收尾）；
- `memory.save_messages([HumanMessage, AIMessage], session_id)`：把这一问一答**落库**
  （这就是刷新页面历史还在的原因，05/12 步细讲）；
- `memory.save_last_query_type(qtype)`：持久化本轮意图类型——注释里写明这是关键修复：
  流式路径若不持久化 query_type，"下单确认/补全信息"这类多轮延续永远无法路由到
  下单 Agent（07 步看 query_type 的用法）；
- `asyncio.create_task(memory.extract_and_store(...))`：后台异步提炼用户偏好，不阻塞。

## 七、HITL 挂起在流里怎么体现（预告）

`_run_graph` 跑图时，若下单节点 `interrupt` 挂起，`astream` 的更新里会带
`__interrupt__` 字段。stream_chat 检测到 → 生成 **pending 事件**（把"待审批提示"推给
用户）并 break，不再发 done。审批动作则由 `/approval/approve|reject` 用
`Command(resume=...)` **唤醒同一张被挂起的图**继续跑（08 步完整讲）。

---

## Q&A

**Q1：SSE 与普通接口的本质区别？对应哪两行代码？**
普通接口：请求→算完→`return {...}` 一次给全→连接关闭。
SSE：`StreamingResponse(event_gen(), media_type="text/event-stream")`——传入一个带
`yield` 的生成器，连接挂起，每 yield 一次推一段。区别就是 `return 值` vs
`StreamingResponse(生成器)`。

**Q2：为什么中间要放 asyncio.Queue？谁塞谁取？**
跑图协程（生产者）与推流生成器（消费者）是两个并行的人，速度不一致，直接互相调用
会阻塞。Queue 是传送带：塞的是 `_run_graph`（节点更新、token、哨兵），取的是主循环
的 `while True: await dst_queue.get()`。塞的不管取的进度，取的不管塞的速度。

**Q3：打字机效果是代码里哪一行实现的？**
源头 `src/llm_utils.py` 的 `_invoke_streaming_async`：`async for chunk in
llm.astream(messages)` 循环里 `if content: pusher(content)`——每生成一小块字就推一次。
pusher → stream_chat 的 `emit("token", ...)` → Queue → yield SSE token 事件 → 前端拼接。

**Q4：为什么 token 用 `put_nowait` 而节点事件用 `await put`？**
token 的 pusher 由 LLM astream 的**同步回调**触发（协程上下文里同步执行），不能
`await`（语法/语义都不行），所以 `put_nowait`；万一队列满（500）会抛错，由外层
except 兜底。节点事件在 `_run_graph` 这个 async 函数里，可以 `await put` 等待队列腾位置
——这正是背压设计：宁可让生产者等，不丢事件不爆内存。

**Q5：为什么只在最终回答时开流？工具调用轮会不会推一堆中间字？**
工具调用轮的 LLM chunk `content` 为空（只有 tool_calls 增量），`if content:` 直接跳过，
天然不推。所以用户在打字机里看到的永远是最终文字回复，不会被"改写、检索、路由"
这些中间 LLM 调用的输出污染——省流量也省认知负担。

**Q6：`done` 事件里为什么是"权威文本"？前端为什么要它？**
token 事件是碎片，可能因网络丢/断不完整；`done` 携带的是服务器最终状态的完整回复
（从图消息里倒序取最后一条非空 AI 消息）。前端逻辑：收到 token 流就实时渲染，
收到 `done` 就以它为准**覆盖收尾**（web App.jsx 注释：`真 token 流已渲染 →
用服务端权威最终文本收尾`；无 token 时本地打字机补偿）。双保险防"显示不完整"。
