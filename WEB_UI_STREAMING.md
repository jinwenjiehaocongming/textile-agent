# Web 前端重构 + 流式输出 — 修改记录

> 记录本次「新前端 + 流式输出」改造的全部改动，含背景、决策、踩坑与验证结果。
> 日期：2026-08-22（会话记录）｜ 分支：main（未提交）

---

## 0. 背景与目标

原系统前端是 `app.py` 内嵌的一段原生 HTML（模仿企业微信绿色界面），且 `/chat` 接口是**同步返回**的（等整轮跑完才给出完整回复，无打字机效果）。

本次改造目标：

1. **全新前端**：React + Vite + Tailwind，现代企业级/克制精致的视觉风格（中性石板灰 + 单一企业蓝强调色），替换内嵌 HTML。
2. **流式输出**：对话回复呈现打字机/流式效果。

---

## 1. 前置调研结论

### 1.1 API 通道确认（重要）

代码里所有变量名都叫 `DEEPSEEK_BASE_URL`，但实际指向 **opencode 网关**而非 DeepSeek 官方：

| 项目 | 值 |
|------|-----|
| `.env` 中 `DEEPSEEK_BASE_URL` | `https://opencode.ai/zen/go/v1` |
| `~/.config/opencode/opencode.json` | 同一网关 + 明文 API key |
| 代码 fallback（未生效） | `https://api.deepseek.com/v1` |

**关键影响**：opencode 网关对**流式调用（stream=True）支持不稳定**（见 §4 踩坑），直接决定了后端的流式实现方案。

### 1.2 后端图结构（重建认知）

```
query_reformulator → context_retriever → supervisor → (agent ⇄ tool_executor) → review → END
                                                        └→ order_agent ────────────→ review → END
                                                        └→ after_sales_agent ──────→ review → END
```

- 售前 `agent`、下单 `order_agent`、售后 `after_sales_agent` 是三个**真正生成用户可见回复**的节点；
- `query_reformulator` / `supervisor` / `review` 也调用 LLM，但属于**中间加工**，不应展示给用户。
- `AgentState` 含 `messages / knowledge_chunks / rewrite_query / query_type / user_id / user_context`。

---

## 2. 后端修改

### 2.1 新增 `src/stream_chat.py`（全新文件，96 行）

流式对话封装模块，供 SSE 端点使用。最终方案：

- **`build_input_state()`**：构建与 `/chat` 完全一致的 AgentState（加载 20 条历史 + 偏好 + last_query_type）。
- **`stream_chat()`**：`graph.astream(state, stream_mode="values")` 每次 yield **完整 state**，最后一次即最终状态；从中提取**最后一条 AI 消息**作为最终回复。
- 流结束自动**存档**（`memory.save_messages`）+ **异步提取偏好**（线程，不阻塞）。

事件协议（与前端约定）：

```
{"type": "start"}                      流连接就绪
{"type": "done",  "content": str}      最终完整回复
{"type": "error", "content": str}      异常信息
```

> 之所以不逐 token 推：见 §4「踩坑记录」。

### 2.2 修改 `app.py`

| 改动 | 说明 |
|------|------|
| 新增导入 | `CORSMiddleware`、`StreamingResponse`、`cheap_llm`、`stream_chat` |
| 新增 **CORS 中间件** | `allow_origins=["*"]`（开发期），允许独立前端跨域访问 |
| 新增 **`POST /chat/stream`** | SSE 端点：先发 `start`，再逐条转发 `stream_chat` 产出的事件；`text/event-stream` + `X-Accel-Buffering: no` 防代理缓冲 |
| 保留旧接口 | `GET /`（旧内嵌页）、`POST /chat`（同步）、`GET /history` 均保留，向后兼容 |
| 端口 | 沿用工作区现有 `8005`（注意：`git HEAD` 中是 `8003`，此差异为**此前未提交的改动**，本次未触碰） |

遗留小问题（未改）：`app.py` 启动打印信息写的 `http://127.0.0.1:8003`，与实际端口 `8005` 不一致。

---

## 3. 前端新建 `web/`（React + Vite + Tailwind）

全新独立前端目录，共 13 个文件（不含依赖）。

### 3.1 目录结构与职责

```
web/
├── package.json              # react 18 + vite 5 + tailwind 3.4（本项目用 v3，稳定）
├── vite.config.js            # dev server 5173；/api → 代理到 http://127.0.0.1:8005
├── tailwind.config.js        # brand 蓝色系、Space Grotesk/DM Sans、soft/lift 阴影、动画
├── postcss.config.js         # tailwindcss + autoprefixer
├── index.html                # 挂载点 + Google Fonts（DM Sans / Space Grotesk）
├── .gitignore                # node_modules / dist / .npm-cache
└── src/
    ├── main.jsx              # React 入口（StrictMode）
    ├── index.css             # Tailwind 指令 + 打字机光标 caret 动画 + 细滚动条
    ├── api.js                # SSE 消费封装（fetch + ReadableStream，解析 data: 事件）
    ├── App.jsx               # 主界面：侧栏 + 聊天区 + 输入区 + 打字机逻辑
    └── components/
        ├── MessageBubble.jsx # 消息气泡（用户右/客服左、**加粗**、列表、时间戳、光标）
        └── TypingIndicator.jsx # 三点打字指示器（等待回复时）
```

### 3.2 设计决策（克制精致 / 企业级）

- **配色**：中性 `slate` 基底 + 单一强调色 `brand-600 #2563eb`（企业蓝，非 AI 紫），全页锁定一种强调色。
- **字体**：标题 `Space Grotesk` + 正文 `DM Sans`（Google Fonts 引入，`font-display: swap`）。
- **布局**：桌面端左侧 256px 能力侧栏（品牌 + 可帮事项 + 在线状态），右侧主聊天；移动端侧栏隐藏。
- **交互细节**：
  - 空状态：欢迎语 + 4 个快捷问题（圆角 pill 按钮）
  - 打字机：AI 回复逐字 reveal，带闪烁光标（`.typing-caret`）
  - 等待态：三点脉冲 TypingIndicator
  - 按钮：`active:scale-95` 物理按压反馈
  - 输入框：圆角、聚焦蓝色 ring，Enter 发送 / Shift+Enter 换行
- **消息渲染**：轻量文本渲染（**加粗**、`-` 列表、`#` 标题），不引入重型 markdown 库。

### 3.3 流式（打字机）实现（前端）

后端返回完整回复后，前端**本地打字机 reveal**：

```js
startTyping(msgId, fullText, onDone) {
  // 每 24ms reveal 1-2 字符，逐字填充气泡；打完回调 setStreaming(false)
}
```

好处：视觉流式效果一致，且完全不依赖后端/网关的 chunk 粒度与稳定性。

---

## 4. 踩坑记录（重要）

流式实现过程中踩了 3 个坑，最终方案是它们的直接结论：

| # | 尝试 | 结果 | 结论 |
|---|------|------|------|
| 1 | `stream_mode="messages"` 逐 token 推给前端 | ❌ **opencode 网关流式不稳定**：日志大量 `Retrying request to /chat/completions` + `LLM 降级: Connection error`；chunk id 异常导致一条回复被拆成多条、内容重复拼接（`T400黑色多少钱T400黑色多少钱...` 反复出现） | 网关不可靠，**放弃后端逐 token 流式** |
| 2 | `stream_mode="updates"` 拿最终 state | ❌ 解包崩溃：`astream(updates)` yield 的是**单个 dict**「{node_name: update}」，不是 `(mode, payload)` 二元组 → `not enough values to unpack (expected 2, got 1)` | 改用 `stream_mode="values"`（yield 完整 state） |
| 3 | `stream_mode="values"`（最终方案） | ✅ 每次 yield 完整 state，最后一次即最终状态，提取最后一条 AI 消息 | **确定为最终方案** + 前端本地打字机 |

**补充**：调试期往用户 `123456` 的 memory 里存了脏数据（拼接重复的回复），已用 `memory.clear_history()` 清理干净。

---

## 5. 验证结果

### 5.1 后端 SSE（curl 直测）

请求 `POST /api/chat/stream` `{"message":"T400黑色多少钱"}`：

```
data: {"type": "start"}
data: {"type": "done", "content": "T400黑色现货价格因规格而异：**复合弹力布**：¥11.3–14.2/米..."}
```

**只回 2 个事件，done 是干净的单条最终回复**（无拼接、无重复）。

### 5.2 端到端（Vite 代理 5173 → 后端 8005）

- `GET /api/history`：✅ 200，返回历史数组
- `POST /api/chat/stream`「羽绒服推荐什么面料」：✅ 触发了真实工具调用（`search_product` 查尼丝纺/春亚纺），最终回复 414 字符，含规格、价格、MOQ、交期、防绒工艺要点。
- 服务状态：
  - 后端 FastAPI **http://127.0.0.1:8005**（uvicorn，PID 3604）
  - 前端 Vite **http://localhost:5173**（PID 3674）

### 5.3 构建

`npm run build` 通过：34 modules，JS 153.9 kB（gzip 49.9 kB）、CSS 14.2 kB（gzip 3.7 kB）。

---

## 6. 运行方式

```bash
# 终端 1：后端（study_1 根目录）
cd ~/Desktop/study_1
.venv/bin/python -m uvicorn app:app --port 8005

# 终端 2：前端（web 目录）
cd ~/Desktop/study_1/web
npm install        # 首次
npm run dev        # 打开 http://localhost:5173
```

---

## 补记（2026-08-24）：真 token 流式已落地

本文最初结论是「放弃后端 token 流式、前端打字机伪装」，**该结论已过时**，更新如下：

- **根因修正**：opencode 网关「可用但首包极慢」（实测 15-38s，整段生成完才吐流），token 流式在该网关上是负收益——所以当时选择打字机是对的，但**不是"opencode 用不了流式"**。
- **真流式实现**：`_safe_llm(stream_tokens=True)` 内部改用 `llm.stream()`，通过 `src/token_stream.py` 的 ContextVar pusher 逐 chunk 推给 SSE（`{"type":"token"}`），前端 `onToken` 实时渲染。节点事件流与 token 流走同一个线程安全管道。
- **适用性**：任何首包延迟正常的兼容网关（官方 DeepSeek API 等）都能直接受益；opencode 网关下机制可用但首包仍慢，属网关侧特性。
- **前端兜底**：收到 token 流时 `done` 直接收尾；无 token（挂起/异常路径）时回退本地打字机。

---

## 7. 遗留事项 / 建议

1. `app.py` 启动打印的端口文案与 `port=8005` 不一致，可顺手修正。
2. CORS `allow_origins=["*"]` 为开发期配置，**生产环境应收紧为具体域名**。
3. opencode API key 明文存于 `~/.config/opencode/opencode.json` 与 `.env`，若项目共享/开源需注意凭据安全。
4. 旧 `GET /`（内嵌 HTML 页）仍保留在 8005 端口；若不再需要可在后续移除或改跳转到前端。
5. 前端 Google Fonts 走外网，内网/离线部署时需改为自托管字体（`@font-face`）。
6. 打字机速度（`TYPE_TICK_MS = 24`，每次 1-2 字符）可按喜好调整。