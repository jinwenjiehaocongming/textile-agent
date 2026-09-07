# 08 · 下单流程 · HITL 人工审批（interrupt 挂起 → 审批唤醒 → 订单落库）

> 目标：看懂「客户说"确认"→ 订单却先挂起 → 销售经理点通过 → 订单号才真正生成」。
> 核心机制：LangGraph `interrupt` 挂起 + `Command(resume=...)` 唤醒（HITL, Human-In-The-Loop），
> 配套 approval.py 待审批注册表、/chat 与 /chat/stream 两处"挂起态守卫"、三个审批管理端点。
> 06 步预告的 pending 事件在这里兑现。

---

## 一、先建立心智模型：谁在等谁

| 角色 | 文件 | 动作 | 一句话 |
|---|---|---|---|
| 客户 | — | 聊产品 → 报出收货信息 → 说"确认" | 以为订单成了 |
| 下单 Agent | `src/order_agent.py` | 收集字段 → 展示确认单 → 撞上 `interrupt` | **把图当场暂停，等审批** |
| 审批注册表 | `src/approval.py` | 内存 dict：谁、什么单、何时提交 | "待审批公示栏" |
| 销售经理（admin） | `app.py` 三个端点 | 看列表 → approve / reject | **拿着客户的 thread_id 唤醒客户的图** |
| checkpointer | `MemorySaver`（agent.py） | 把挂起那一步的图状态落盘 | 图的"存档点"，唤醒从这儿续跑 |

**HITL = 人到流程中间盖章。** 类比银行大额转账：你在柜台填完单（下单 Agent 收集完字段），
柜员不直接打款，而是把单子递给经理（register_pending），经理盖章（approve/reject）后
柜台才执行转账（create_order 真正写库）。经理不盖章，这笔业务就永远"挂"在系统里。

一句话先记住 interrupt 的语义（第四节逐行讲）：
**首次跑 = 抛异常暂停**（interrupt 值存进 checkpoint，ainvoke 返回 `__interrupt__`）；
**唤醒跑 = 同一 thread 上重放**，`interrupt()` 改"返回" resume 值，函数继续走——
下单函数会整体重跑一遍，靠 LLM 温度 0 保证重放决策一致（order_agent.py:133-134 注释自述）。

---

## 二、前置：客户怎么被送进下单 Agent（一句话，细节见 07 步）

Supervisor（agent.py:388）输出 `query_type`，`supervisor_router`（agent.py:503-509）据此
路由到 `order_agent` 节点。认"下单意图"有三条路：LLM 分类（提示词明确"确认下单/帮我安排/
就要这个"归 order，agent.py:378）；规则延续 Layer 0.5（prev=place_order 且客户回 ≤4 字
短确认词 → 无条件延续，agent.py:399-408，注释写明是修复"确认被误判成闲聊 → 下单挂半路"
的补丁）；Layer 0.6（上一轮在收集信息 → 客户补一句 → 留在下单上下文）。图节点把 user_id
当 customer_id 传进下单函数（agent.py:483-491）：

```python
reply = await order_agent(state["messages"], customer_id=state.get("user_id", "guest"))  # agent.py:485
```

## 三、下单 Agent 职责链：收集字段 → 确认单 → 触发挂起

### 3.1 提示词规定的工作流（order_agent.py:90-123，逐条对齐）

| 步骤 | 提示词要求（原文摘录） | 行号 |
|---|---|---|
| 收集 | "货号 + 产品名 + 颜色 + 数量 + 单价 + 电话 + 地址 + 交期"；"信息不齐 → 问客户补齐，不能编造"；"产品价格没确认过 → 先调 search_product 查，不能猜" | order_agent.py:96-98 |
| 确认单 | 输出固定格式确认单；**"确认单里绝对不能出现订单号"**；结尾固定为"请确认以上信息是否正确？回复'确认'即可下单。"（这行是后面路由识别"正在等确认"的暗号） | order_agent.py:100-112 |
| 下单 | "客户说'确认' → 调用 create_order 工具，严禁编造订单号；订单号只能从 create_order 工具返回" | order_agent.py:115-117 |
| 身份 | "create_order 的 customer_id 参数必须填系统提供的值：{customer_id}"——防止 LLM 填"客户"或编一个 id | order_agent.py:119-121 |

也就是说：**真正由 LLM 自由发挥的空间被提示词锁死**，字段一个都不能少、订单号不许编。
工具集也收得很窄（order_agent.py:185-187）：只绑 `search_product` + `create_order` + 表格渲染工具。

### 3.2 执行循环里 create_order 被拦截（order_agent.py:228-235）

```python
if name == "create_order":
    # HITL：人工审批拦截（interrupt 挂起，审批通过才写库）
    result = await _approve_then_create(args, customer_id)
    # 完成信号：工具真实返回成功（LLM 文本不可信，历史订单号会误伤）
    if "✅ 订单已生成" in str(result):
        order_completed = True
    tool_msgs.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
```

注意顺序：**"审批"发生在"调工具"之前**——先挂起，唤醒批通过后才真
`call_tool("create_order", ...)`：LLM 一次工具调用被拆成两段执行，中间隔着人工。

`order_completed` 标志随返回消息挂到 `additional_kwargs`（order_agent.py:203-209），
agent.py:489-490 据此把 query_type 重置回 chat——注释点破这是 2026-09 的修复：
**绝不能从 LLM 回复文本里找订单号判断完成**（回复复述历史订单号会把下单误判成已完成）。

## 四、挂起那一刻：_approve_then_create 逐行拆（order_agent.py:126-168）

```python
quantity = args.get("quantity", 0)
unit_price = args.get("unit_price", 0)
try:
    total = round(float(quantity) * float(unit_price), 2)   # ① 预算是给"确认单"展示用的
except (TypeError, ValueError):
    total = ""
```

① LLM 工具参数可能是字符串，先转 float 算个**展示用** total；算不出来就空着
（真正写库的 total 后面在 order server 里重算，见第八节——展示值可以错，库里的不能错）。
```python
draft = {
    "product_name": args.get("product_name", ""), "product_id": args.get("product_id", ""),
    "color": ..., "quantity": quantity, "unit_price": unit_price, "total": total,
    "phone": ..., "address": ..., "delivery_date": ...,
}                                            # ② 确认单草稿 = 审批人要审的全部信息

register_pending(customer_id, customer_id, draft)   # ③ 先登记，再挂起（approval.py:158）
decision = interrupt({"type": "order_approval", "draft": draft})   # ④ 挂起！图在此暂停
remove_pending(customer_id)                      # ⑤ 唤醒后第一件事：撤下公示栏
```

- ③ **register 必须先于 interrupt**：审批端点的存在性检查（app.py:263）与列表展示都靠它，
  先登记再挂起，保证挂起瞬间公示栏里已有这单。
- ④ `interrupt` 的 payload 是 `{"type": "order_approval", "draft": draft}`——`type` 标记
  "这是订单审批"，`draft` 是要审的内容。首次执行到这一行**抛异常暂停**，后面的审批
  分支根本没机会跑。
- ⑤ 唤醒后 `interrupt()` 这次**返回值**，执行继续，立刻 remove_pending 撤下公示栏。

```python
approved = bool(decision and decision.get("approved"))
reason = (decision or {}).get("reason", "")
if approved:
    return await get_mcp().call_tool("create_order", args)          # ⑥ 真写库，返回"✅ 订单已生成！订单号：ORD-..."
extra = f"（原因：{reason}）" if reason else ""
return f"订单未通过人工审批，已取消{extra}。如有疑问请联系销售经理。"   # ⑦ 拒绝文案（带原因）
```

⑥⑦ 的返回值成为 ToolMessage 的内容，成为这一轮对话的"最终回复"。
**审批决定是 approve 还是 reject，完全由 resume 值里的 `approved` 字段决定。**

> 类比：interrupt 就像函数里的"暂停键"——按下去，函数停在这一行，现场（局部变量/参数）被
> 存档；管理员处理完，用同一把钥匙（thread_id）回来按"继续"，函数从头重放一遍，
> 走到暂停键时不再暂停，而是把管理员的决定当作这行代码的返回值继续往下走。

## 五、待审批注册表：approval.py（内存 dict + 锁）

```python
_lock = threading.Lock()
_pending: dict[str, dict] = {}          # approval.py:18-19
```

**以 thread_id（= user_id）为键**；模块注释（approval.py:8-10）自认"进程内存储（与
MemorySaver 一致）：生产环境换 Redis/DB 即可，接口保持不变"。

| 函数 | 干什么 | 行号 |
|---|---|---|
| `register_pending(thread_id, user_id, draft)` | 登记；**重放时幂等覆盖**（注释点破：唤醒重跑会再调一次） | approval.py:22-30 |
| `set_pending_session(thread_id, session_id)` | 补记"挂起发生在哪个会话"（chat 层检测到 interrupt 后调用，供审批结果回写用） | approval.py:33-37 |
| `list_pending()` | 管理员列表用，按 `created_at` 升序 | approval.py:40-46 |
| `get_pending(thread_id)` | 单查 | approval.py:49-51 |
| `remove_pending(thread_id)` | 审批处理完移除 | approval.py:54-57 |
| `find_pending_draft(interrupts)` | **从 interrupts 里解出 draft** | approval.py:60-66 |
| `pending_reply_text(draft)` | 把确认单格式化成"已提交人工审批"文案 | approval.py:69-83 |

`find_pending_draft` 是全文复用最多的函数（/chat 守卫、stream 守卫、结果处理都要它），解析逻辑：

```python
for it in interrupts or []:
    val = it.value if hasattr(it, "value") else it    # Interrupt 对象取 .value，dict 直接用
    if isinstance(val, dict) and val.get("type") == "order_approval":
        return val.get("draft") or {}
```

兼容两种形态：`aget_state` 的 `snap.interrupts` 里是 Interrupt 对象（取 `.value`），
`ainvoke` 返回的 `__interrupt__` 里是裸 dict——test_approval.py:33-45 用 Fake 验证此兼容。

`pending_reply_text`（approval.py:69-83）生成客户可见的中间回复：

```
📋 您的订单已提交人工审批
产品：T400 | 货号：P0075 | 颜色：黑色
数量：200米 | 单价：¥12.5/米 | 总价：¥2500.0
电话：138xxx
⏳ 销售同事将尽快人工确认，审批通过后订单号将自动生成，请稍候。
```

（电话/地址/交期**有才拼**——approval.py:76-81 逐个 if 判断，不给列表刷空行。）

## 六、挂起态守卫：等审批时不能再跑图

**为什么**：订单挂起 = 图停在半路等 resume；此刻客户再发消息若照常 `ainvoke` 跑图，会向
同一 thread 塞新输入（该 thread 需显式 resume 才能续跑），造成状态冲突。所以两个聊天
入口都在跑图**之前**先查 checkpoint（`aget_state` 只读存档不执行图）：有订单待审批就
**不跑图**，把"已提交审批"再回一遍。

### 6.1 /chat（普通接口，app.py:101-105）

```python
snap = await agent_graph.aget_state(config)     # 只读 checkpoint，不执行图
draft = find_pending_draft(getattr(snap, "interrupts", None)) if snap else None
if draft:
    return {"reply": pending_reply_text(draft), "pending": True, "draft": draft}
```

### 6.2 /chat/stream（SSE 流式，stream_chat.py:77-83）

```python
snap0 = await graph.aget_state(config)
if snap0 and snap0.next:                        # 注意：比 /chat 多查 snap.next
    draft = find_pending_draft(snap0.interrupts)
    if draft:
        yield {"type": "pending", "content": pending_reply_text(draft)}
        return                                  # 推一个 pending 事件就收工
```

`snap.next` = 图下一步还要跑哪些节点——**非空 = 有未完成运行**（比 /chat 只看 interrupts
更保守，任何中断态都会被挡）。stream 版直接 yield `pending` 事件（06 步协议第⑤类事件
在此兑现）并 return，**不发 done**。

### 6.3 挂起那一刻，流里发生了什么（stream_chat.py:119-138）

图跑到 order_agent 节点撞 interrupt 后，`astream` 的节点更新里带 `__interrupt__` 字段：

```python
if "__interrupt__" in ev:
    draft = find_pending_draft(ev["__interrupt__"])
    if draft:
        interrupted = True
        reply = pending_reply_text(draft)
        await memory.save_messages([HumanMessage(content=req_message), AIMessage(content=reply)], session_id)
        await memory.save_last_query_type("chat")     # 重置路由状态，防止下轮又自动进下单
        set_pending_session(user_id, session_id)      # 记住挂起发生在哪个会话
        yield {"type": "pending", "content": reply, "data": {"type": "order", "data": draft}}
    break                                             # 停止消费管道，不再等后续节点
```

细节：把"客户的话 + 已提交审批的提示"落库（刷新历史可见）；query_type 重置为 chat
（防下一句被 Layer 0.5 自动延续成下单）；`set_pending_session` 把会话 id 补进注册表
（第五节那个空 session_id 就是留给这一刻的）。最后 `break` 出循环并置 `interrupted=True`，
连 done 都不发（stream_chat.py:154-155）——这场对话"停"在 pending 上，06 步预告兑现。

## 七、审批端点：管理员拿着 thread_id 唤醒客户的图

三个端点全部 `Depends(require_admin)`（app.py:250-300）——401 没登录 / 403 非 admin，
客户即使拿到 URL 也调不动（为什么客户不能批自己的单，见 Q4）。

### 7.1 列表端点（app.py:250-253）

```python
@app.get("/approval/pending")
def approval_pending(admin: dict = Depends(require_admin)):
    return {"pending": list_pending()}
```

一行：`list_pending()` 已把 dict 展开成 `{thread_id, user_id, draft, created_at, session_id}`
列表（approval.py:43-45）。

### 7.2 approve / reject → 同一个 _resume_approval（app.py:289-300）

```python
# 两端点只差 approved 布尔；审批者身份作 actor 传去审计
return await _resume_approval(body.thread_id, approved=approved,
                              reason=body.reason, actor=admin["user_id"])
```

### 7.3 _resume_approval 逐段（app.py:261-286）——"唤醒"到底执行了什么

```python
if not thread_id or not get_pending(thread_id):          # ① 公示栏没这单 → 拒绝操作
    return {"ok": False, "error": f"没有待审批的订单: {thread_id!r}"}

config = thread_config(thread_id)                        # ② 用 thread_id 定位图
result = await agent_graph.ainvoke(
    Command(resume={"approved": approved, "reason": reason}), config=config   # ③ 唤醒！
)
remove_pending(thread_id)                                # ④ 撤下公示栏
```

- ② `thread_config(user_id)`（agent.py:522-524）= `{"configurable": {"thread_id": user_id}}`。
  **为什么 thread_id 就是 user_id？** 注释一句话点破："HITL interrupt 依赖它定位会话"。
  客户挂起与审批唤醒用的是**同一把钥匙**，checkpointer 才能把两次调用认成"同一张图的
  同一次运行"——审批方拿注册表暴露的 thread_id（=客户 user_id）就能唤醒客户的图，
  这也是注册表 + admin 双重门禁缺一不可的原因（Q4）。
- ③ `Command(resume=...)` 是 LangGraph 的"续跑指令"：checkpointer 找到该 thread 停在哪
  一步重放；重放到 `_approve_then_create` 时 `interrupt()` 返回 `{"approved","reason"}` →
  第四节的分支执行（approved 走 create_order 写库 / reject 走取消文案）。`order_agent`
  节点整体重跑一遍（温度 0 → 同样决策），撞到 create_order 不再挂起（resume 语境下
  interrupt 只"返回值"）；节点内 remove_pending（order_agent.py:160）先把单撤了——
  app.py:270 的 remove 只是兜底。

唤醒返回后：

```python
final_msgs = (result or {}).get("messages") or []
ai_text = ""
for m in reversed(final_msgs):                 # 倒序找最后一条有内容的回复（就是 ✅ 订单已生成 文本）
    if getattr(m, "content", ""):
        ai_text = m.content
        break
if ai_text:
    info = get_pending(thread_id) or {}        # ⚠️ 注意：此时上面第④步已 remove，
    sid = info.get("session_id") or "default"  #    get_pending 恒为 None → sid 恒回退 'default'
    await get_user(thread_id).save_messages([AIMessage(content=ai_text)], sid)

await _audit(actor, "approve" if approved else "reject", thread_id, reason)   # 审计留痕
return {"ok": True, "approved": approved, "reply": ai_text}
```

- 唤醒结果（订单号 / 取消文案）作为一条 AIMessage 存进客户会话历史——下次打开聊天记录
  能看到"✅ 订单已生成！订单号：ORD-..."。
- `_audit`（app.py:235-247）写 audit_log：谁（actor）、何时、批了哪单（thread_id）、理由；
  审计失败只打印不影响主流程（审计是增强不是关键路径，02 步已提）。
- **⚠️ 诚实标注（顺序 bug）**：第④步先 remove_pending、到这里才 get_pending（app.py:279）
  ——而唤醒重放时节点又 register 过（session_id 空串）又被 remove，所以 get_pending 恒为
  None、`sid` 恒回退 `"default"`。单会话（default）场景无感；**多会话下审批结果会被写进
  default 会话**，而不是客户下单时所在的那个会话（Q7 细说）。

> 与 CLI 对照（agent.py:616-632）：命令行版没有注册表和 HTTP 端点，挂起后**当场**在终端
> 打印 draft、`input("审批 (y=通过 / n=拒绝[原因])")`，再同样
> `ainvoke(Command(resume=...), config=thread_config(user_id))` 唤醒——同一套 resume
> 机制，只是审批人从 HTTP 管理员换成了终端使用者。

## 八、订单落库与数据流

### 8.1 create_order 工具（order_server.py:34-90）

```python
order_no = (f"ORD-{now.strftime('%Y%m%d')}-"
            f"{now.strftime('%H%M%S')}{now.microsecond:06d}{random.randint(1000, 9999)}")  # 49-50
total = round(quantity * unit_price, 2)        # 51：total 在 server 端重算，不信任 LLM
```

- **订单号 = 日期 + 时分秒 + 微秒6位 + 随机4位**：注释点明是为了"并发同一秒多单不撞
  UNIQUE"（orders.order_no 有 UNIQUE 约束，db.py:96）。订单号因此**只可能来自这里**，
  这也是"严禁编造订单号"的底气。
- **total 谁算的**：`create_order` 签名里根本没有 total（order_server.py:35-45），库里那笔
  钱永远是 server `round(quantity*unit_price, 2)` 算的（:51）；第四节下单 Agent 算的 total
  只是**确认单展示用**——可信的那条在写库这一侧。

```python
await execute("""INSERT INTO orders (..., status, created_at, phone, address, delivery_date)
   VALUES (..., '待付款', :created_at, ...)""", {...})    # 55-68：状态默认 '待付款'
```

orders 表（db.py:94-111）：`order_no UNIQUE`、`customer_id`、`product_id/name`、
color/quantity/unit_price/total、status（默认'待付款'）、created_at、
**paid_at/shipped_at**、phone/address/delivery_date。注意 paid_at/shipped_at 两列存在但
**全项目没有任何 UPDATE orders**——订单停在"待付款"，付款/发货是留给生产的状态位
（诚实边界）。客户查自己订单走 `/orders`（app.py:377-386）：`WHERE customer_id = :uid`
行级隔离。

### 8.2 一次完整下单的时序（数据视角）

```
客户"确认" → order_agent → register_pending → interrupt 暂停（checkpoint 落盘，
/chat 回"已提交审批"文案 + save_messages + set_pending_session）
→ 经理 GET /approval/pending 看到 draft → POST /approval/approve{thread_id}
→ Command(resume) 重放 → interrupt() 返回值 → create_order 写 orders(待付款, 真实 total)
→ "✅ 订单已生成！订单号：ORD-..." → remove_pending → save_messages(ai_text) → _audit
```

---

## Q&A

**Q1：为什么用 LangGraph interrupt 挂起，而不是自己存一个"草稿状态位"？**
interrupt 的价值是 **checkpointer 把"执行到一半的函数现场"自动落盘**：图停在哪一步、
停时状态是什么，LangGraph 都替你存好（MemorySaver，agent.py:556-558），恢复只要
`Command(resume=...)` + 同一个 thread_id。自己写状态位 = 手工序列化"走到第几步、收集了
哪些字段、工具调用链长什么样"再写恢复器还原——等于重造 checkpointer 且易漏状态。
更妙的是**恢复路径和首次路径是同一份代码**：`interrupt()` 一行在两种语境下行为不同
（抛异常暂停 vs 返回值），业务零分支。追问点：interrupt 挂在**节点函数内部**，唤醒会
重跑整个节点（含 LLM 调用）——所以下单 LLM 必须 temperature=0，否则重放时 LLM 可能
"改主意"不调 create_order 了（order_agent.py:133-134 注释自述）。

**Q2：审批挂起期间，客户再发一条消息会怎样？**
两个入口都有"挂起态守卫"：/chat 先 `aget_state` 看 `snap.interrupts`（app.py:102-105），
stream 还多看 `snap0.next`（stream_chat.py:78-83），发现有待审批单就**不跑图**，把
"您的订单已提交审批，请稍候"再回一遍并 return。若没有守卫，新消息会以新输入 `ainvoke`
到一张"停在半路"的图上——该 thread 有未完成运行，LangGraph 要求显式
`Command(resume=...)` 才能续跑，裸塞新输入与挂起语义冲突（/chat 的
except 会兜成"系统异常"，app.py:140-143），更危险的是新消息可能污染正在等审批的图状态。
追问点：守卫只挡"订单审批类"挂起（`find_pending_draft` 按 `type == "order_approval"` 找），
本项目只有这一种 interrupt，所以够用。

**Q3：interrupt 的"落盘"到底指什么？重启会丢吗？**
`ainvoke` 撞 interrupt 时，图状态被 checkpointer 持久化；`aget_state` 能读回、
`Command(resume=...)` 能续跑，全靠它。但本项目 checkpointer 是 **MemorySaver（进程内
内存）**，approval 注册表也是**进程内 dict**（approval.py 注释自认"与 MemorySaver 一致，
生产换 Redis/DB 即可"）——**服务一重启，挂起的单和公示栏一起消失**，客户永远等不到审批；
生产要换 LangGraph 官方 PG checkpointer + Redis 注册表才能跨重启存活。主动说出
"重启即丢 + 怎么修"是加分项。

**Q4：为什么客户不能 approve 自己挂起的单？审批权限到底怎么控的？**
三层：
1. **注册只能建 customer**（app.py:182 注释；admin 只能由 scripts/create_admin.py 幂等创建），普通客户拿不到 admin 角色；
2. **三个审批端点全部 `Depends(require_admin)`**（app.py:251/290/297），非 admin 一律 403——
   即使客户从前端网络面板看到审批 API 的 URL 也调不动；
3. 审计把 admin 身份写进 audit_log（actor=admin["user_id"]），操作可追溯。
更深一层要想明白：`resume` 能力本身只认 thread_id，而 thread_id 就是客户自己的 user_id——
**如果审批端点不校验角色，客户自己就能给自己批单**（自己发个 approve 请求即可）。所以
"注册表公示 + require_admin 门禁 + audit 留痕"三者缺一不可：注册表让管理员能发现单、
require_admin 保证只有管理员能动手、audit 保证动过手赖不掉。追问点：真正的生产系统还应
校验"审批人不能审批自己提交的单/越级审批"，本项目没有组织层级，未做。

**Q5：为什么 register_pending 放在 interrupt 之前、remove_pending 紧跟其后？**
`register_pending` 先于 interrupt，保证**挂起的瞬间公示栏已有这条**——审批端点的存在性
检查（app.py:263）和列表展示都依赖它；若先挂起后登记失败，会出现"图停着但管理员看不见"
的死单。`remove_pending` 紧跟 interrupt 是因为**注册表只是"谁在等审批"的公示栏，不是
真相源**——真相在 checkpoint（图到底停没停）；interrupt 一返回说明审批决定已到手、后续
写库/取消马上执行，公示栏使命完成就撤，避免"已处理完还挂在列表上"的脏状态。且 register/
remove 在节点重放里再跑一遍也安全：register 幂等覆盖、remove pop 不存在不报错——重放安全
正是 HITL 正确性的前提（test_approval.py:74-99 用合成图验证 invoke→interrupt→resume）。

**Q6：为什么 total 不在 LLM 那边算好传过来，库里还要 server 重算？**
create_order 的工具签名**压根没有 total 参数**（order_server.py:35-45），所以 LLM 想传
都传不了——写库的总价永远来自 server 的 `round(quantity*unit_price, 2)`
（order_server.py:51）。理由：钱是敏感数据，不能信任"模型口算"（12.5×200 可能被算成
2499.99，也可能被提示词诱导改数）；数量×单价是确定性计算，放代码里零成本且永远正确。
下单 Agent 算的 total（order_agent.py:141-144）只是确认单展示，让客户和审批人先看个
大概；两边即使有差异，入库值以 server 为准。追问点：演示没有折扣/运费/多行订单，一旦有，
"纯乘法"假设就破，计价逻辑得收敛到 server 一个函数。

**Q7：多会话下，审批结果会写回哪个会话？**（真实的代码弱点）
设计意图：挂起时 `set_pending_session(user_id, session_id)` 记下会话（app.py:126 /
stream_chat.py:132），审批后 `_resume_approval` 读它、把结果存回那个会话
（app.py:279-281）。但代码顺序是 **先 `remove_pending(thread_id)`（app.py:270）再
`get_pending(thread_id)`（app.py:279）**——remove 之后 get 恒为 None，`sid` 恒回退
`"default"`：客户在非 default 会话下的单，审批通过后订单确认会被写进 default 会话，
切回原会话看不到。单会话演示无感，但这是真实的多会话缺陷；修法一句话：把
`info = get_pending(thread_id)` 挪到 `remove_pending` **之前**读 session_id 再删——
这种"注释意图 vs 代码实际"的落差，面试主动指出比背八股可信。

**Q8（追问）：唤醒那一刻，订单是"立刻生成"的吗？中间还会出什么岔子？**
唤醒 = 重放 order_agent 节点：LLM（temperature=0）重新决策 → 再次调用 create_order →
审批值生效 → 写库。两个真实风险：① 重放时 LLM 调用若超时/失败，`_safe_llm_async` 的
fallback（order_agent.py:215）会返回"系统繁忙，请稍后重试"——此时 **approve 端点照样返回
ok:True**，但订单其实没写库，也没有二次校验提醒管理员（诚实取舍：演示接受"审批通过但
落库失败"靠人眼发现）；② 订单号在 create_order 内部才生成（order_server.py:49-50），
所以确认单阶段绝无订单号；重放多次 create_order 只会各自生成新单号，绝无"同一单写两次"。
