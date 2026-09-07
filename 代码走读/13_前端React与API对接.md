# 13 · 前端 React 与 API 对接

> 目标：从浏览器视角把整条链路串一遍——怎么过门禁、token 放哪、SSE 怎么被一帧帧读出来、
> 气泡/表格/审批面板各自谁在管。读完你能解释清楚"为什么前端用 fetch 手写读流而不是
> EventSource""为什么 token 放 sessionStorage""改 localStorage 里 role 为什么没用"。
> 覆盖 `web/src/api.js`（268 行）、`App.jsx`（637 行）、六个组件、vite/构建配置。

---

## 一、先建立心智模型：前端只干四件事

| # | 事 | 载体 | 关键代码 |
|---|---|---|---|
| ① 过门禁 | 启动先问后端"我是谁" | `/me` + token | App.jsx:76-81、api.js:95-99 |
| ② 拉数据 | 会话列表/历史/订单/待审批 | 普通 GET/POST + Bearer | api.js:102-268 |
| ③ 流式消费 | 聊天是"边算边推" | **fetch reader 手写读流** | api.js:130-215 |
| ④ 渲染 | 气泡、表格、审批、订单卡 | 组件树 | App.jsx + components/ |

前后端两种跑法（同一份前端代码）：

```
开发：vite dev 跑在 5173，浏览器请求 /api/xxx → vite 代理转发到 8005 并剥掉 /api 前缀
生产：web/dist 由 FastAPI 直接托管，浏览器请求 /api/xxx → 后端 APIRouter(prefix="/api") 处理
```

所以 `BASE` 永远指向 `/api`（api.js:13：`import.meta.env.VITE_API_BASE || '/api'`），
开发靠代理、生产靠同源 + 后端 /api 路由，前端代码一行不用改。

## 二、构建与代理（vite.config.js:5-18 / app.py:389-423）

```js
server: {
  port: 5173,
  proxy: { '/api': {
      target: 'http://127.0.0.1:8005',
      changeOrigin: true,
      rewrite: (path) => path.replace(/^\/api/, ''),   // 剥前缀 → 后端无前缀路由
  } },
}
```

**为什么 dev 要代理？** 前端 5173、后端 8005，跨域请求会撞 CORS。代理让浏览器眼里
"只有一个源"：请求发到同源的 5173/api，vite 在服务端转发去 8005——浏览器全程无感知，
后端那行 `allow_origins=["*"]`（app.py:66）都未必用得上（CORS 是给不走代理的直连留的）。

**生产**：`npm run build` 产出 `web/dist`，FastAPI 在**所有接口注册完之后**再
`app.mount("/", StaticFiles(directory=web/dist, html=True))`（app.py:415-419）——
注释写得很清楚："必须最后挂载，避免吞掉 /chat /api 等接口"（Starlette 按注册顺序匹配，
静态托管放前面会把接口请求也当文件去找）。而构建产物请求的是 `/api/...`，后端为此把
同一组 handler 又注册了一份带 `/api` 前缀的路由（app.py:392-412：APIRouter(prefix="/api")
把无前缀的 handler 函数对象直接复用），因此**无前缀与 /api 双路由共享同一实现**。

## 三、启动门禁：fetchMe 401 → 登录页

`main.jsx` 只是把 App 挂到 #root（main.jsx:6-10），真正的门禁在 App 挂载后的第一个
effect（App.jsx:76-81）：

```jsx
useEffect(() => {
  fetchMe()
    .then((u) => setUser(u))      // 有 token 且有效 → 拿到 {user_id, role, ...}
    .catch(() => setUser(null))    // 任何失败 → 当未登录
    .finally(() => setBooting(false))
}, [])
```

`fetchMe`（api.js:95-99）就是带 `Authorization` 头请求 `/me`，**非 2xx 一律返回 null**
（不区分 401/网络错——演示期"没登录跟后端挂了"都跳登录页，见取舍）。于是渲染分三态：
`booting` → 闪屏（App.jsx:322-331）；`!user` → `<LoginPage>`（:333-335）；
有 user → 主界面。

登录成功走 `LoginPage` 的回调（App.jsx:84-90 handleAuthed）：setUser + 回 chat 视图 +
清空 messages/steps。LoginPage 自己先做**客户端预校验**（用户名正则与后端一字不差地
复刻一份：`USERNAME_RE = /^[A-Za-z0-9_-]{3,32}$/`，LoginPage.jsx:15、75-82），过了再
调 `login`/`register`（api.js:64-87），成功即 `setToken(body.token)`——**注册成功也
自动登录**，因为后端 register 直接返回了签发好的 token（02 步）。

登出（App.jsx:93-105 handleLogout）只做一件事：`logout()`（api.js:90-92）= 清掉本地
sessionStorage 的 token + 重置一堆 state。注释写明"服务端无状态，无需调接口"——
JWT 没有服务端会话可销毁，这既是设计也是局限（旧 token 到期前仍有效，02 步 Q4）。

## 四、token 管理：为什么是 sessionStorage（api.js:9-12 注释即答案）

```js
const TOKEN_KEY = 'hongrun_token'
const store = window.sessionStorage          // api.js:32-33
export function getToken() { return store.getItem(TOKEN_KEY) || '' }
function authHeaders(extra = {}) {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}`, ...extra } : { ...extra }
}
```

注释原话（api.js:9-12）就是面试答案：**sessionStorage 每个标签页独立，可开多个窗口
分别登录不同账号（客户 + 管理员）互不覆盖；localStorage 是同源所有标签共享的——
后登录者会把别的窗口顶掉**。演示"一边是客户在聊、一边是管理员在审批"就必须双开两
个窗口两个身份。代价（诚实，02 步 Q5 深讲）：sessionStorage 对 XSS 不设防、关标签页
即失效（刷新还在，重开要重登）。

所有请求都走 `authHeaders()`（api.js:48-51）：有 token 就带 `Authorization: Bearer`，
没有就不带（让后端 401 来决定命运）。**收到 401 前端怎么办**（App.jsx:294-305 的
onError）：聊天流中途若错误文案里含 "401" → 把当前气泡改成"登录已过期，请重新登录"、
停流，600ms 后调 handleLogout 踢回登录页——其他普通接口（fetchSessions 等）没有逐
个 401 处理，靠下一轮交互的 /me 兜底（诚实点：非流式路径的 401 体验不统一）。

### 顺带看 uuid()：一个防"非安全上下文"的兜底（api.js:16-30）

```js
export function uuid() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function')
    return crypto.randomUUID()
  // crypto.randomUUID 只在 HTTPS 或 localhost（安全上下文）可用；
  // 公网 http://IP 部署时它不存在 → 用 getRandomValues 手工拼 uuid v4 兜底
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  bytes[6] = (bytes[6] & 0x0f) | 0x40      // 版本位 → v4
  bytes[8] = (bytes[8] & 0x3f) | 0x80      // 变体位 → RFC 4122
  ...拼接 8-4-4-4-12 格式
}
```

消息/会话的**前端临时 id**全靠它（App.jsx 给每条消息 `id: uuid()`）。注释透露了真实
踩坑：`crypto.randomUUID` 只在安全上下文（HTTPS/localhost）存在，用 IP 直连的
http 部署会直接 `undefined`——所以先探能力、再手工拼一个合规的 v4 UUID。这种
"环境差异 → 兜底"的写法本身就是个面试好故事。

## 五、SSE 消费：fetch reader 逐块读，按 `\n\n` 拆事件

`streamChat`（api.js:130-215）是前端最核心的函数，参数 7 个回调
（onStart/onReset/onToken/onNode/onDone/onError，JSDoc 在 :120-129）。

### 1) 超时与请求（api.js:132-157）

```js
const controller = new AbortController()
const timeoutTimer = setTimeout(() => controller.abort(), 60000)  // 60s 硬超时
...
resp = await fetch(`${BASE}/chat/stream`, {
  method: 'POST',
  headers: authHeaders({ 'Content-Type': 'application/json' }),
  body: JSON.stringify({ message, session_id: sessionId }),
  signal: controller.signal,
})
```

注释："后端网关不稳定时，避免前端无限转圈"。AbortError 会落到 catch（:142-146）里被
翻译成"请求超时（60秒），请稍后重试"。连接一旦成功就 `clearTimeout`（:147）——
**超时只保护"迟迟连不上/首包不来"，不掐已开始的流**（读流阶段的保护另有兜底，见 Q4）。

### 2) 逐块读 + 手动分帧（api.js:161-199）

```js
const reader = resp.body.getReader()
const decoder = new TextDecoder()
let buffer = ''
while (true) {
  const { done, value } = await reader.read()
  if (done) break
  buffer += decoder.decode(value, { stream: true })   // 二进制块 → 文本（防多字节截断）

  let idx
  while ((idx = buffer.indexOf('\n\n')) !== -1) {     // SSE 事件以空行分隔
    const rawEvent = buffer.slice(0, idx)
    buffer = buffer.slice(idx + 2)
    for (const line of rawEvent.split('\n')) {
      if (!line.startsWith('data:')) continue          // 只认 data 行
      const data = line.slice(5).trim()
      ...JSON.parse 后按 type 分发...
    }
  }
}
```

三个要点：
- **`decoder.decode(value, { stream: true })`**：UTF-8 是变长的，一个汉字可能被切成
  两个 TCP 包——`stream: true` 告诉解码器"还没完，先别把不完整字符报错"，拼到 buffer
  里等后续字节。
- **`buffer + indexOf('\n\n')`**：网络分包不会正好断在事件边界，所以要自己攒缓冲、
  按 SSE 的"空行分隔"规则切出完整事件再解析（这层活 EventSource 是内置的，
  见 Q3 我们为什么不用它）。
- **按行过滤 `data:` 前缀**：一份事件可能有 data/event/id 多行，本项目只发 data，
  所以 `line.slice(5).trim()` 后 JSON.parse（:176-181）。

### 3) 事件分发（api.js:181-196，与 06 步协议一一对应）

| 后端事件 | 前端动作 |
|---|---|
| `start` | 跳过（连接就绪信号，App 用它清空输入） |
| `reset` | onReset（App 里是空实现，注释"后端已不使用，保留兼容" App.jsx:269） |
| `token` | onToken(逐字内容) → 追加渲染 |
| `node` | onNode({node,label,detail}) → 顶部步骤条 |
| `done` / `pending` | onDone(content, data) → 权威收尾 + 可能带表格数据 |
| `error` | onError(文案) |

读完 while 循环后还有一段"处理残余 buffer"（api.js:201-211）——若流结束的那一包
恰好没带 `\n\n` 结尾，把尾巴当最后一个事件再试一次；最后 `finally` 里
`reader.releaseLock()`（:213）归还读锁。

### 4) App 侧：token 拼消息、done 权威收尾、无 token 打字机补偿（App.jsx:236-312）

send() 的关键结构：先造两条消息（用户气泡 + 一个空的 AI 气泡 `streaming: true`，
:259-262），再调 streamChat：

```jsx
let receivedTokens = false                          // 是否收到过真 token 流
onToken: (token) => {
  receivedTokens = true
  patchMessage(aiMsg.id, (m) => ({ content: (m.content || '') + token }))  // 逐字追加
},
onDone: (full, data) => {
  const text = full || '（无回复）'
  if (receivedTokens) {
    // 真 token 流已渲染 → 用服务端权威最终文本收尾（06 步的 done 语义）
    patchMessage(aiMsg.id, () => ({ content: text, streaming: false }))
    setStreaming(false)
    if (data) patchMessage(aiMsg.id, () => ({ data }))     // 挂表格数据
  } else {
    // 无 token（挂起/兜底路径）→ 本地打字机补偿
    startTyping(aiMsg.id, text, () => { setStreaming(false); if (data) ... })
  }
},
```

为什么有两套收尾？`receivedTokens` 是**分水岭**：
- 有真 token：气泡已经逐字渲染，`done` 的完整文本是权威（防网络丢包导致缺字），
  直接覆盖 + 收尾；
- 没 token：说明这次回复根本没走真流（HITL 挂起的 pending、非流式兜底），前端要用
  `startTyping` **自己演一遍打字机**（App.jsx:220-234：setInterval 每 24ms 揭示
  1~2 个字符，走完 clearInterval 回调 onDone），保证用户看到的体验一致。
最后无条件 `refreshSessions()`（:309）——后端可能把会话标题改成了首条消息摘要
（touch_session 自动命名，05 步），静默刷新侧栏。

挂起的"待审批卡片"也在这里：pending 事件被分发成 onDone(content, data)，data 是
`{type:'order', data: draft}` → 气泡正文显示"订单已挂起等待人工审批…"，`data` 字段
让 MessageBubble 渲染出一张订单卡片（见第七节 DataTable）。

## 六、视图状态与会话管理（App.jsx:56-206）

**view 三态 + 权限显隐**（App.jsx:71、338-342、461-484）：

```jsx
const isAdmin = user.role === 'admin'
const showApproval = view === 'approval' && isAdmin   // 双条件：非 admin 即使 view 是 approval 也不渲染
...
{isAdmin && (<button onClick={() => goView('approval')}>订单审批</button>)}
```

"我的订单"所有登录用户可见（app.py:377-386 `/orders` 只查自己的），"订单审批"按钮
**只有 isAdmin 渲染**，且面板渲染条件还带 `&& isAdmin` 兜底。前端 role 只做 UI 显隐，
真正的授权在服务端 require_admin（02 步）——改 sessionStorage 里的值骗不了 JWT 验签
（Q2 细讲）。

**会话列表三连**（登录后 effect App.jsx:107-133）：拉 `fetchSessions()` → 没会话就
`createSession()` 自动建一个并激活 → 点会话 `handleSelectSession` 换 sessionId →
另一个 effect（:185-201）监听 sessionId 变化去 `fetchHistory` 拉该会话历史（role
human→user 转换 + 每行生成 uuid），**切会话必清空再拉，天然不串数据**。新建/删除会话
（:141-182）都有对应 API（api.js:233-260），删除当前会话后自动切到列表第一条。

**steps 节点事件渲染**（App.jsx:70、257、276-278、523-550）：每次 send 前清空 steps；
收到 node 事件就 push；渲染成消息面板**顶部一条横向丝带**（不随消息滚动），每个节点
一颗胶囊：最后一个节点且仍在流式时显示 ping 呼吸点，否则打 ✓，带 label + detail。
这就是用户在侧栏看到的"改写查询 → 检索 → 路由 → 应答 → 审核"逐格点亮。

## 七、六个组件，一句话一个

| 组件 | 一句话职责 | 关键代码 |
|---|---|---|
| `LoginPage.jsx` | 登录/注册双态表单，客户端预校验（正则复刻后端）+ 提交后回调 onSuccess | :15, :68-96 |
| `MessageBubble.jsx` | 渲染一条消息：左右分边、**轻量文本渲染**（只支持 `**加粗**`/换行/`-` 列表，刻意不引 markdown 库，:8-43）、`data` 存在时追加 DataTable | :45-87 |
| `TypingIndicator.jsx` | 三点跳动"正在输入"动画（`animate-pulse-dot`，tailwind.config.js:45），仅当最后一条 AI 气泡内容为空且 streaming 时出现（App.jsx:564） | :1-20 |
| `DataTable.jsx` | 按 `data.type` 分发：products → 多行表格；order/refund → 键值卡片（dl 布局）；三种都带语义色状态徽章 | :11-17 |
| `OrderList.jsx` | "我的订单"卡片列表 + **15s 轻轮询静默刷新**（审批在另一个窗口发生时新订单自动出现，:95-108） | :76-152 |
| `ApprovalPanel.jsx` | 待审批表格，通过 / 拒绝（拒绝展开理由输入框）；busyId 防连点 | :45-59 |

一个诚实的瑕疵：状态徽章的配色逻辑在 `DataTable.jsx:130-142`、`OrderList.jsx:20-32`
各写了一份（同样的"待付款→琥珀/已付款→绿"映射）——组件抽得不够彻底，是
复制粘贴式的重复，提出来比藏着好。

## 八、整条链路一图流

```
浏览器                      FastAPI 8005
LoginPage ──login──►  /auth/login ──► JWT 存 sessionStorage
App useEffect fetchMe() ──/me──► 401 → 登录页 / 200 → 主界面
send() ──POST /chat/stream──► SSE 流
   │  reader.read() 逐块 ─► buffer ─► '\n\n' 切帧 ─► JSON.parse ─► 分发
   │   token ──► 逐字追加到气泡（打字机）
   │   node   ──► steps 顶部丝带
   │   done   ──► 权威文本覆盖收尾（+ data 表格）
   │   pending──► 订单挂起卡片（等管理员审批）
管理员窗口 ──GET /approval/pending / POST /approval/approve|reject──► 08 步恢复下单图
我的订单   ──GET /orders──► customer_id 行级隔离
```

---

## Q&A

**Q1：为什么 token 存 sessionStorage 不存 localStorage？代价是什么？**
因为两个存储的生命周期语义不同：localStorage 在同源**所有标签页共享**——你开一个
客户窗口登录、再开一个管理员窗口登录，后者会把前者顶掉（api.js:9-12 注释原话）；
sessionStorage 按**标签页隔离**，每个窗口各自持有一份 token，正好满足"客户在聊、
管理员在旁边审批"的双开场景。代价两层：① sessionStorage 对 XSS 脚本同样可读
（它防的是跨标签页，不是恶意脚本，见 02 步 Q5 的 httpOnly Cookie 讨论）；② 会话
随标签页关闭而消失，重开浏览器要重新登录。面试补充：更严谨的做法是"记住我"选项
才落 localStorage、否则 sessionStorage，以及后端按 token 维度做吊销清单。

**Q2：前端用 `user.role` 决定显隐，改掉 localStorage/sessionStorage 里的 role 能越权吗？**
不能——这正是"前端权限 = 摆设、服务端权限 = 真章"的经典题。role 不是前端存的，
是**服务端签在 JWT payload 里**的（auth.py 签发时写入，02 步第五节），前端每次请求
只回传 token 本身，`require_admin` 依赖在验签后从 payload 里取 role 判 403
（app.py:250-253 `/approval/pending` 需要 admin）。你改 sessionStorage 只能改掉
"前端记住的 token 字符串"——它签名就错了，/me 直接 401。所以哪怕用 DevTools 把
审批按钮强行显示出来，点进去的请求也会被 403 打回。前端 isAdmin 的唯一价值是
UI 整洁（App.jsx:473-484），不是安全。

**Q3：为什么用 fetch 手写读流，不用浏览器自带的 EventSource？**
EventSource 有三个硬限制：① 只支持 GET；② **不能自定义请求头**——本项目聊天要带
`Authorization: Bearer`，EventSource 塞不进去（塞 query 参数会进服务器日志，等于
token 裸奔）；③ 不能带 body（`session_id` 没法传）。fetch + `getReader()` 则任意
方法/头/body 都行，代价是要自己处理两件事：`TextDecoder(stream:true)` 的多字节安全
解码（api.js:169）和 `buffer.indexOf('\n\n')` 的按帧切分（:173）——这就是 EventSource
内置、而我们手动实现的"分帧器"。另外 fetch 还能配 AbortController（EventSource 只能
close，等不了 60s 这种硬超时）。诚实补充：手写解析对协议约定敏感（万一后端以后发
`event:` 行/多行 data，这段解析要跟着升级）。

**Q4：60 秒 AbortController 超时的取舍？超时后后端还在跑吗？**
取舍在 api.js:132-133 注释里写得很白："后端网关不稳定时，避免前端无限转圈"——连接
阶段（fetch 没返回）卡死是最常见的，60s 后 abort 并提示"请求超时"。要注意它
`clearTimeout` 的时机是**连接成功后**（:147），所以读流阶段的慢回复不受 60s 管束。
两个诚实边界：① 一个特别复杂的单子（检索 + 多轮工具 + 长回复）若整体超过 60s 仍会
被掐——没有按"已收到首包就续期"的滑动超时；② **前端 abort 不等于后端取消**——浏览器
断开只是读端放弃，uvicorn 侧的 StreamingResponse 生成器要等它下一个 `await` 点才会
被取消（06 步那条 `await dst_queue.get()` 主循环），也就是说"这单后端到底算完没算完、
落没落库"取决于取消点，**前端拿超时当'没发出'的确证是不可靠的**。生产该做的是把
超时拉长 + 前端防重发 + 后端按消息做幂等去重，三件套缺一不可。

**Q5：`done` 为什么是"权威收尾"？receivedTokens 分支到底在防什么？**
token 事件是碎片：网络抖动丢一包、或流中途断，前端拼出来的文本就可能缺字少句，
所以 06 步设计里 `done` 携带服务端最终状态里抽出的完整回复（stream_chat 从图最后
消息倒序取最后一条非空 AI 消息）。前端 onDone 时若 `receivedTokens` 为真，就用这个
权威文本**覆盖**渲染中的气泡（App.jsx:281-285）——双保险。`receivedTokens=false`
的分支对应"这次根本没有 token 流"的情况：最常见的是 HITL 挂起（pending 事件先到，
图被 interrupt 打断，不会有最终 token）和错误/兜底回复；这时前端调 startTyping
**本地演一遍打字机**（:220-234）把 text 逐字打出来，用户感知不到差别。用一个布尔
flag 分流两套体验，是这段代码最值得面试讲的设计。

**Q6：多窗口多会话时，两条流/两个身份会不会互相干扰？**
身份不干扰：每个标签页独立 sessionStorage（Q1），管理员窗口和客户窗口各持各的 token。
会话数据不干扰：所有会话接口都带 token → 后端解出各自的 user_id → 行级隔离
（fetchSessions/orders 全查自己的，12 步）。同窗口内"切会话"由 effect 串行拉历史
（App.jsx:185-201，切走即清理，防止旧会话的历史渲染进新会话——React 用 `alive` 标志
+ effect cleanup 处理竞态）。一个真实的干扰面：**同一用户在两个标签页同时下单**——后端
按 user_id 只有一个挂起位（08 步的 pending 注册表），后到的会撞"已有待审批订单"的
守卫提示（stream_chat.py:77-83）。这是演示系统的单挂起设计，不是 bug 但值得知道。

**Q7（追问）：读流解析里 `decode(value, {stream: true})` 和 `indexOf('\n\n')` 各防什么？
不写会怎样？**
前者防**多字节截断**：UTF-8 的"纺"占 3 字节，若恰被切成两个 TCP 包，第二包开头是
半个字符，直接 `buffer += value`（默认按完整字符串解）会解出乱码/报错——stream 模式
让解码器知道"没解完的字节先留着，等下一块补齐"。后者防**半包**：一次 read 可能只到
事件的一半，也可能一次含多个事件，`buffer + while(indexOf('\n\n'))` 把"攒够的完整
事件"全部切出去，没凑齐空行的留在 buffer 等下一包——这正是 SSE 分帧协议的
"状态机"实现。少任何一层，高流量/慢网络下就会出现"表格渲染一半"或"JSON.parse 抛错
被静默吞掉"（api.js:194-196 的 catch 就是给坏帧兜底的）。
