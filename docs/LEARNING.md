# 从本地 Demo 到上线部署：完整改造复盘（学习笔记）

> 本文档按时间顺序完整记录了一次真实改造：**纺织 B2B 交易智能体** 从"能跑的 Demo"
> 到"带账号体系 / 多会话 / 历史订单 / 新 UI / 可公网访问"的全过程。
> 每节都有：做了什么 → 为什么 → 关键代码/文件 → 踩过的坑与原理。
> 配套阅读：[README](README.md)（项目全貌）、[docs/DEPLOY.md](DEPLOY.md)（部署手册）、[docs/UNDERSTANDING.md](UNDERSTANDING.md)（原架构讲解）。

---

## 0. 起点：项目原本是什么

LangGraph 多 Agent 纺织 B2B 客服：改写查询 → 混合检索(Qdrant+BM25+RRF) → Supervisor 路由
→ 售前/下单/售后 三个子 Agent → 双层审核 → SSE 流式。存储：PostgreSQL（业务）+ Qdrant（知识）。
下单走 HITL 人工审批（LangGraph interrupt 挂起 → 管理员审批 → Command(resume) 恢复）。

当时**缺**：真实账号体系（靠 X-User-Id 头模拟身份）、会话概念（历史=每个用户一条流水）、
客户端界面（dev 下拉切换身份）、公网部署能力（容器启动不初始化、无前端产物）。

---

## 1. 需求讨论与决策（先聊清楚再动手）

目标优化点：登录注册 + 一个管理员号 / 内部界面大改 / 部署。
讨论后定下：

| 决策 | 结论 | 原因 |
|---|---|---|
| 顺序 | 登录体系先行，UI 大改在后 | UI 的"壳"（身份栏/登出门禁/角色导航）依赖账号状态 |
| 注册策略 | 开放注册（强制 customer）+ 种子脚本建 admin | 越权注册 = 把 admin 当注册口开放，种子脚本幂等可控 |
| 无 token 访问 | 一律 401（删除 guest/X-User-Id 回退） | 否则登录系统形同虚设，任何人可冒充任意客户 |
| 部署 | 最后做；先保证本地全绿再谈服务器 | 先功能后上线，避免边部署边返工 |

> 教训：**大改造先做"地基"，再往上盖**。改 UI 前若没有身份系统，主界面会为"开发下拉框"设计、
> 等登录上线又重拆一遍。

---

## 2. M1：登录注册 + 账号体系（第一次提交的核心）

### 后端
- `users` 表：`id(uuid hex 对外 user_id) / username UNIQUE / password_hash / role(customer|admin) / status`。
  - 为什么 uuid 做对外 id：登录名可改、对外 id 不变，且与老"X-User-Id 任意串"划清边界。
- `src/users.py`：bcrypt 哈希（**绝不存明文**）；username 小写归一化（防 `Alice`/`alice` 撞号）；
  开放注册只允许 customer；`UsernameTaken` 映射 HTTP 409。
- `app.py`：
  - `POST /auth/register`（成功即签发 token，自动登录）、`POST /auth/login`（错密码与用户名不存在**统一 401**，不泄露账号存在性）
  - `GET /me`、`/chat`、`/chat/stream`、`/history` 全部改为 `Depends(get_current_user)`，无 token = 401
  - 保留 `/dev/login` 但仅 `DEV_MODE=1`（本地演示用），生产 `DEV_MODE=0` 即消失
- `scripts/create_admin.py`：幂等建/重置管理员，`.env` 的 `ADMIN_USERNAME/ADMIN_PASSWORD` 驱动。

### 前端
- `LoginPage.jsx`：登录/注册双态卡片；label 在上、错误在字段下、提交 loading；深色玻璃风与主界面一致。
- `api.js`：真实 login/register；401 统一处理。
- `App.jsx`：启动 fetchMe → 无 token 渲染登录页；顶栏显示用户名 + 角色徽标 + 登出。

### 安全细节（面试常问）
- bcrypt 自带随机盐 + 成本因子（`BCRYPT_ROUNDS=12`）
- 注册接口**不接受 role 字段**（忽略越权输入）
- JWT payload 只放 `sub/role/iat/exp`；服务端依赖注入校验角色，前端 role 仅控制 UI 显隐
- 密码/哈希绝不出现在任何接口返回里

---

## 3. UI 玻璃化：方向与多轮迭代教训

目标：登录页 + 主界面统一为"深色玻璃 + 圆角浮层"的现代 B2B 质感。
做法与弯路（这段最值得复习）：

| 版本 | 改动 | 结果 / 教训 |
|---|---|---|
| v1 | 全局 slate → 半透明白（white/4-8%）+ backdrop-blur，改 brand 为 DeepSeek 蓝 | 用户："没变化"。**教训：面板不透明度过高 + 背景无内容可糊，玻璃=看不出来** |
| v2 | 加大 aurora 光晕透明度 | "有一点变化，登录框/侧栏/输入框要更圆角 + 立体感" |
| v3 | 布局重构：侧栏+主区拆成**独立圆角浮层**（`p-3` 留缝），加 `shadow-panel`（inset 顶部受光高光 = 立体感来源），登录卡 rounded-3xl、按钮胶囊化 | ✅ 认可 |
| v4 | 彩色实底 → 淡色玻璃（logo/CTA/气泡 brand-400/20-30%），加 CSS 海浪光斑 | "看不太出来" |
| v5 | 面板降透（white/3-5）让海浪透出 + 海浪加亮提速 + 淡蓝按钮配**深藏青文字**（色淡但对比高） | 明显变化（像素对比蓝调 2.7%→35.8%） |
| v6 | 海浪光带 + 9-21s 动画 | "不太对，回上一版"。**教训：动效宁可克制** |
| v7 | 回退 v5；随后圆角体系整体+档、三块面板（上顶栏/中对话/下输入）分离、移动端适配 | ✅ 定稿 |

### 工程要点
- **圆角/色板/阴影做在 tailwind config 一处**（改 config = 全站生效）：radius 覆盖默认档、boxShadow 加 `panel`（inset 高光+柔影）。
- 玻璃感三要素：半透明面板（需要背景有东西可糊）+ backdrop-blur + hairline 描边（white/10 级）。
- 固定背景层 `#aurora-bg`：4 团 radial 光斑做 transform 平移/缩放动画（GPU），`prefers-reduced-motion` 降级。
- 文案统一：侧栏"交易智能体/纺织面料 B2B 交易助手"，浏览器 title、FastAPI title、**AI 系统提示词**同步改名，避免"界面叫交易智能体、AI 自称宏润"的精神分裂。

---

## 4. 多会话隔离 + 历史订单

### 会话（多对话）
- `sessions` 表 + `conversations.session_id`（幂等 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 迁移老数据 → 'default'）。
- `src/sessions.py`：CRUD + **所有权校验**（`WHERE id=? AND user_id=?`，跨用户一律 404）+ 自动标题（新会话首条用户消息前 18 字）。
- `memory.py`：`load_recent/save_messages` 全部带 session_id；热缓存 key 变 `chat:{uid}:{sid}`。
- `app.py`：chat/history/stream 请求体带 `session_id`；新增 `/api/sessions`（增删改查）、`/api/orders`。
- **审批回写原会话**：挂起时在 approval 注册表记 `session_id`，管理员审批后结果存回客户当时所在对话。
- 前端侧栏 = 会话列表（新建/切换/删除悬停），当前会话高亮；切换会话即切换历史（互不串）。

### 订单
- `GET /orders`：`WHERE customer_id = 当前登录用户` → 行级隔离（B 访问 A 的会话/订单 = 404/空）。
- 前端「我的订单」视图（卡片式：订单号/状态徽章/明细/收货），**15s 静默轮询**（审批在另一端发生时自动出现）。

---

## 5. 一个身份两个窗口：localStorage → sessionStorage

现象：两个浏览器标签，一个登录客户、一个登录管理员，后者总是把前者顶掉。
原因：`localStorage` **按源共享**——所有同源标签共用一个存储，token 写同一 key 互相覆盖。
修复：token 存 **sessionStorage**（每个标签页独立）。代价：关标签 = 登出。
> 知识点：`crypto.randomUUID` 与 secure context 同理——只在 HTTPS/localhost 可用，见第 9 节部署坑。

---

## 6. 下单"没调工具"的 Bug 深挖（最有含金量的一段）

### 症状
客户走完整流程（下单 → 补电话地址 → 说"确认"），日志显示 `[Supervisor] → 售前 Agent`，
create_order 从未触发，订单挂在半路。

### 取证过程（看日志讲故事）
```
20:49:45 [Supervisor] → 下单 Agent      ← "3000米帮我下单" 正确
20:49:55 [下单Agent工具] search_product  ← 查价后等客户补信息
20:51:23 [Supervisor] → 下单 Agent      ← 补地址，又进了下单
20:51:36 [Supervisor] 延续 → 售前        ← "确认" 被路由去售前 ✗
```

### 根因
Supervisor 的状态机（`agent.py` 的 Layer 0.5/0.6）靠 `prev = 记忆里上一轮 query_type` 判断"是否延续下单"。
而记忆里的 `last_query_type` 只有**非流式 `/chat` 在保存**——网页走的是 **SSE `/chat/stream`，从不保存 query_type**。
于是 prev 永远停在最初 `chat`，确认词只能靠 LLM 每轮碰运气分类成 order，误判即断链。

### 修复（三层）
1. `stream_chat.py`：正常结束时把本轮 `query_type` 写回记忆（下单流程的 place_order 得以跨轮延续）；
   挂起分支与 /chat 对齐存 chat（审批后自然回到售前语境）。
2. `agent.py` Layer 0.6：prev=place_order 且上一轮 AI 在收集信息/等确认、客户没说"不用/换话题" → 无条件延续下单，
   不再把补全句（"13857577360，钱塘路1102，10天" 全是数字无关键词）交给 LLM 猜。
3. 配套单测：纯数字补全延续 / 尾问后换新话题不误捕 / 拒绝词正确转售前。

### 通用教训
- **"状态跨轮"必须显式持久化**：两个入口（/chat 与 /chat/stream）行为分叉是隐蔽 bug 温床，改一处必须查另一处。
- 路由决策要**规则兜底 + LLM 分类**分层，短指令/补全信息类用确定性规则，不要全交给模型。
- 修复用单测锁行为：101 个测试全绿后再合并。

---

## 7. Rerank 开关与资源权衡

本地 rerank（bge-reranker-base ~1.1GB）在低配服务器上是负担；云端 rerank API 需要额度。
决定：加环境开关 `RERANK_ENABLED`，**默认 0**（只跑向量+BM25+RRF；评测 Hit@3 本就是 100%，rerank 是精排加分项）。
- `src/retrieval.py` 提供 `rerank_enabled()`；`agent.py` 检索节点改为 `use_rerank=rerank_enabled()`。
- 评测脚本显式传参做消融，**不受开关影响**（保留将来对比能力）。
- 复用价值：需要精排时 `.env` 改 `1` 重启即恢复；服务器省 ~1GB 内存。

---

## 8. Git 提交组织

- `f097906 feat: 账号体系 + 玻璃 UI + 多会话 + 历史订单 + 下单流程修复`（28 文件，用户要求一次提交）
- `28ee9e2 feat(deploy): 容器一键初始化 + 部署文档 + 种子数据`
- `e39a3e8 fix(web): crypto.randomUUID 在 http 非安全上下文不可用 → uuid() 兜底`
- 敏感文件确认：`.env`、`web/dist`、`logs` 全部在 .gitignore（提交前 `git check-ignore` 复核）。

---

## 9. 部署到服务器：完整踩坑记录（每一条都是真金白银）

完整步骤见 [docs/DEPLOY.md](DEPLOY.md)，这里记录**真实踩过的坑与原理**：

### 9.1 传输载体：zip 打包的排除陷阱
- 第一次打包 397MB：漏排 `.uv-cache`（792MB）、`web/.npm-cache`（38MB）。
- **教训：大目录用 `du -sh` 先排雷**，打包后 `unzip -l | sort -nr | head` 复查最大的成员。
- 部署包只放代码：排除 .env（含密钥）、.venv/node_modules/index/日志/本地数据。

### 9.2 Docker 多阶段：前端必须有产物进镜像
- 原 .dockerignore 排除了 `web/dist` 且 Dockerfile 只有 python 单阶段 → 镜像里没有前端 → 只有接口没界面。
- 改成两阶段：`node:20-alpine` 先 `npm ci && npm run build`，python 阶段 `COPY --from=web /web/dist ./web/dist`。

### 9.3 容器"空转"三连坑（healthz 全绿但功能全无）
1. **不初始化**：表没建、管理员没有、知识库空 → 加入口脚本 `docker_entrypoint.sh`：等 PG/Qdrant → `create_admin`(建表+管理员) → `build_index`(灌知识) → 启动。
2. **`python scripts/xxx.py` 找不到 `src`**：python 把脚本所在目录加入 sys.path，不含项目根 → 脚本内 `sys.path.insert(0, 项目根)`（create_admin 早有，build_index 漏了）。
3. **产品表空**：打包时把 `data/*.db` 当"本地数据"误排了，而 281 条产品正藏在这里 → 改为把产品表导出成 `sql/001_products.sql`，compose 里 postgres 首次初始化自动执行（`docker-entrypoint-initdb.d` 挂载）。

### 9.4 模型下载三连坑（香港服务器）
- 镜像 ENV `HF_HUB_OFFLINE=1` → 构建时下载被自己禁网 → 下载行临时 `HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0`。
- huggingface 新存储 Xet 绕过 hf-mirror（报 `FileMetadataError: Distant resource does not seem to be on huggingface.co`）→ 加 `HF_HUB_DISABLE_XET=1`。
- hf-mirror 反而在容器内连不上 → 去掉镜像直连 `huggingface.co`（服务器可达），成功。
- **层顺序优化**：模型下载 RUN 放在 `COPY . .` 之前，以后改业务代码不用重下 0.4GB 模型。

### 9.5 前端公网白屏/发不出消息：secure context
- 症状：登录 OK，发"你好"没反应；Console 报 `crypto.randomUUID is not a function`。
- 原理：`crypto.randomUUID` 只在 **安全上下文（HTTPS 或 localhost）** 存在；`http://公网IP` 明文访问 → 不存在 → 前端崩溃。
- 修复：封装 `uuid()`：可用则 randomUUID，否则用 `crypto.getRandomValues` 手工拼 v4（getRandomValues 无此限制）。
- **通用教训**：本地 localhost 一切正常 ≠ 线上正常；凡依赖 secure context 的 API 都要兜底或直接上 HTTPS。

### 9.6 双进程锁
- Qdrant **LocalMode** 只允许一个进程持有存储目录；本地想再起一个实例验证 → 直接锁冲突。
- 部署版已用独立 Qdrant 容器（QDRANT_URL 指向服务），本地 LocalMode 仅用于开发。

### 9.7 公网访问
- 轻量服务器控制台「防火墙」添加规则 TCP 8005 → 浏览器 `http://公网IP:8005`。
- 白屏排查顺序：强刷(Cmd+Shift+R) → 无痕窗口 → F12 Console/Network 看 404/报错。

---

## 10. 现状核对

| 项 | 状态 |
|---|---|
| 测试 | 101 通过（账号/会话/订单/Supervisor/检索…） |
| 管理员 | `424341405`（部署环境 .env 自定密码） |
| 本地服务 | `python app.py` → http://127.0.0.1:8005 |
| 公网服务 | compose 在服务器，入口脚本自动初始化 |
| Git | 干净，三个功能/修复提交 |

---

## 11. 常用操作速查

```bash
# 本地开发
python scripts/create_admin.py        # 建表 + 管理员（幂等）
python app.py                         # http://127.0.0.1:8005
cd web && npm run build               # 改前端后重新构建（静态文件即时生效）

# 服务器
docker compose up -d --build          # 构建并启动（入口自动初始化）
docker compose logs -f app            # 实时日志（Ctrl+C 退出，不影响服务）
docker compose exec postgres psql -U postgres -d study1 -c "select count(*) from products;"
docker compose exec app python scripts/create_admin.py   # 改密码后重跑（幂等）

# 备份
docker compose exec postgres pg_dump -U postgres study1 > backup.sql
```

---

## 12. 遗留边界（下次可以做的）

- 审批注册表 + LangGraph checkpoint 仍在进程内存：重启丢挂起 → 换 PostgresSaver/Redis（代码只动 compile 一处）。
- 单 worker 部署：多副本需外置 checkpoint。
- 未上 HTTPS：上域名后建议 Caddy（见 DEPLOY.md），顺带解决 secure context 一类问题。
- 管理员密码、数据库默认口令：演示够用，真对外请改强。
