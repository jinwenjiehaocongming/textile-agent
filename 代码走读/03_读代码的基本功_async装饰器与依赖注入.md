# 03 · 读代码的基本功：async / 装饰器 / Depends / 占位符 / yield

> 目标：把本仓库里反复出现、但对新手最劝退的语法点一次性讲透。
> 读完你能**独立读懂任意一个接口函数**。本文不是理论课，全部用本仓库真实代码举例。

---

## 一、async / await：为什么到处都是

普通函数：调用 → 干完 → 返回，期间**卡住调用者**。
异步函数：遇到 `await`（等）就**让出控制权**去干别的，等结果好了再回来继续。

类比：你去银行办事，同步 = 排队时干瞪眼占着窗口；异步 = 取号后先去喝咖啡，
叫到号再回来——**同一个窗口（线程）能服务更多人**。

仓库里凡是"慢 IO"都是 async：查数据库（db.py）、调 LLM（llm_utils.py）、
连 MCP 子进程（mcp_client.py）、跑图（agent.py 全 async 节点）。

### 阅读规则（背下来）

1. 函数声明带 `async def` → 内部几乎必有 `await`。
2. `await X()` = "调用 X 并等它干完，期间去服务别人"。
3. `async for ... in ...` = "这个对象是异步流（生成器），一个一个等它吐"。
4. `asyncio.create_task(fn())` = "**后台启动 fn，不等它**，先干后面的"（fire-and-forget）。
5. `asyncio.Queue` = 异步线程安全的消息管道（06 步主角）。
6. 同步函数里**不能直接 await**；但 async 函数里可以调同步函数（会阻塞，慎用）。

真实例子（app.py:137）——"存档后**顺手**在后台提取用户偏好，不阻塞回复"：

```python
await memory.save_messages(...)          # 等存档完成
asyncio.create_task(memory.extract_and_store(...))  # 后台跑，不等它
return {"reply": ...}                    # 立刻回给用户
```

## 二、装饰器 `@`：函数上的"魔法标签"

`@app.post("/auth/login")` 写在函数上方 = **"把这个函数注册成 /auth/login 的路由"**。
FastAPI 收到该路径的 POST 请求，就去调用下面这个函数。类比：菜单条目 → 后厨菜谱。

装饰器本质：`@d` 等价于 `f = d(f)`——把函数传给装饰器，装饰器加工后**返回一个新函数**
替换原函数。FastAPI 的 `@app.post(...)` 返回的"新函数"附带路由信息。

本仓库其余装饰器速查：
- `@app.get("/me")` → GET 路由
- `Depends(get_current_user)` → 不是装饰器，是**参数默认值语法**，见下节
- `@asynccontextmanager`（app.py:51）→ 让一个生成器函数成为"异步上下文管理器"
  （进入时初始化、退出时清理，见 04 步 lifespan）

## 三、Depends：FastAPI 依赖注入（最容易被"看不懂"的一个）

看签名（app.py:93）：

```python
async def chat(req: ChatRequest, user: dict = Depends(get_current_user)):
```

普通人第一反应：`Depends(...)` 是什么默认值？答案是 **FastAPI 的魔法**：
`user: dict = Depends(get_current_user)` 表示——**调用 chat 之前，先调用
`get_current_user`，把它的返回值作为 user 参数传入**。

- FastAPI 看到 `Depends(...)` 就自动执行里面的函数；
- `get_current_user` 自己又声明了 `authorization: str = Header(default="")` →
  FastAPI 自动从**请求头 Authorization** 取值喂给它；
- 依赖可以嵌套：`require_admin` 内部 `Depends(get_current_user)`，先认证后授权。

类比：点餐时"自动附赠例汤"——你只写要什么菜，FastAPI 负责把例汤（用户身份）
端到你桌上。好处：**每个接口不用自己写"验 token"那几行**，一行 `Depends` 全搞定，
逻辑收敛在 auth.py 单点（改一处全局生效）。

## 四、SQL 占位符 `:name`：为什么值不能直接拼进字符串

看真实代码（users.py:131）：

```python
await execute(
    "INSERT INTO users (id, username, password_hash, ...) "
    "VALUES (:id, :username, :pw, ...)",
    {"id": uid, "username": u, "pw": hash_password(password), ...},
)
```

`:id` `:username` 是**占位符**（空位），真实值在下方字典里按名对应。
**绝不能**这样写：

```python
f"VALUES ('{uid}', '{u}', '{pw}')"   # ❌ SQL 注入
```

因为 `u`（用户名）是**用户输入**。若输入 `zhangsan'); DROP TABLE users; --`，
拼进去就成了"删表"命令。占位符写法让数据库**永远把输入当纯数据**处理。
铁律：**一切用户输入进 SQL 必须走占位符**。面试问"怎么防 SQL 注入"答这句。

## 五、yield 与生成器：函数也能"挤牙膏"

普通函数 `return` 一次给完。带 `yield` 的函数每次执行到 `yield` 就把当前值交出，
**函数暂停**，下次再续。它叫**生成器**，可被 `for` 遍历。

真实场景：SSE 推送（app.py:320）——

```python
async def event_gen():
    yield "data: {\"type\": \"start\"}\n\n"      # 先推第一段
    async for evt in stream_chat(...):           # stream_chat 也是生成器
        yield f"data: {_json.dumps(evt, ...)}\n\n"   # 一段一段推
return StreamingResponse(event_gen(), media_type="text/event-stream")
```

`StreamingResponse` 收到生成器后：**连接不关**，每取到一次 `yield` 就发给浏览器一次，
直到生成器跑完（StopIteration）才关连接。这就是 SSE 的全部秘密——
"挤牙膏式的响应"。06 步细讲。

## 六、其余高频"黑话"速查

| 写法 | 含义 | 出现处 |
|---|---|---|
| `def f() -> dict:` | 返回类型注解（提示，不强制） | 到处 |
| `class X(BaseModel)` | pydantic 模型：自动校验请求体字段类型 | `LoginBody` 等 |
| `raise HTTPException(status_code, detail)` | 抛 HTTP 错误（带状态码+文案） | app.py 到处 |
| `try/except E as e:` | 捕获特定异常 | app.py:185 |
| `ContextVar` | 线程内隐式传值的"传话筒"（不用显式传参） | `token_stream.py` |
| `create_task` | 后台协程 | app.py:137 |
| `stream_mode="updates"` | LangGraph：每跑完一个节点吐一次状态 | agent.py |
| `Command(resume=...)` | LangGraph：唤醒被 interrupt 挂起的图 | app.py:267 |
| `os.getenv("X", default)` | 读环境变量 | 到处 |
| `from X import Y` | 导入（模块/函数） | 到处 |

---

## Q&A

**Q1：为什么这里到处 async/await？同步写会怎样？**
聊天要等 LLM 几秒~几十秒。同步服务器 = 一个请求占一个线程直到结束，100 并发就要
100 线程，且大量线程在空等网络 IO。asyncio 单事件循环在 `await` 期间转去处理其他
请求，**少量线程扛大量慢 IO**。这也是"企业级演进"的核心：全链路 async
（FastAPI→db→MCP→LangGraph→LLM astream 单事件循环）。

**Q2：Depends 到底是装饰器还是默认值？**
都是"写法"，本质是 FastAPI 的参数解析钩子：`user: dict = Depends(fn)` 让 FastAPI
在调用本函数前执行 fn 并把结果注入参数。它是**声明式依赖注入**——接口只声明
"我需要一个已认证用户"，怎么认证由 auth.py 决定，单点维护。

**Q3：同步代码里能 await 吗？异步代码里能调用同步慢函数吗？**
同步里不能 await（语法错误），要用 `asyncio.run` 包一层或改用异步。异步里调同步慢
函数**可以但会阻塞整个事件循环**（所有人都卡），所以本项目 LLM/DB/IO 全走异步版；
纯 CPU 快操作（正则、bcrypt 校验除外）无所谓。bcrypt 校验在 async 登录里是同步调用，
几 ms~0.3s 级别可接受（严格可用 `asyncio.to_thread` 丢线程池）。

**Q4：`asyncio.create_task` 和直接 `await` 什么区别？**
`await` = 等它干完才继续（串行）。`create_task` = 让它在后台跑，自己立刻继续（并发），
之后可选择 `await task` 收结果。本项目用它在回复后后台提取偏好：**用户体验优先，
慢活让路**。注意：后台任务要自己 try/except（无人 await 的异常会"凭空消失"或报错）。

**Q5：装饰器原理一句话？**
`@d` 等价 `f = d(f)`：d 接收原函数 f，返回加工后的函数替换之。FastAPI 的
`@app.post(path)` 是"注册路由并附带方法/路径元数据"的加工。

**Q6：pydantic BaseModel 起什么作用？**
`body: RegisterBody` 时 FastAPI 自动：解析 JSON → 按字段类型校验 → 缺字段/类型错
直接 400（不会执行函数体）。`LoginBody(username: str, password: str)` 保证了
`body.username` 一定存在且是字符串，代码里不用再手工判空。
