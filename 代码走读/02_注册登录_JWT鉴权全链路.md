# 02 · 注册登录 · JWT 鉴权全链路

> 目标：彻底看懂「注册/登录接口那几行代码」以及背后的 JWT / bcrypt / 认证授权分层。
> 这是二面最可能先问的部分，也是整个系统所有接口的"门禁"。

---

## 一、先建立心智模型：三层分工

| 层 | 文件 | 职责 | 一句话 |
|---|---|---|---|
| 身份来源 | `src/users.py` | users 表读写 + 密码哈希 + 校验 | **谁有资格登录、密码对不对** |
| 身份载体 | `src/auth.py` | JWT 签发/验签 + FastAPI 认证授权依赖 | **登录后你拿什么证明是你** |
| 存储底座 | `src/db.py` | 引擎 + 建表 + 通用查询 | 底层用什么存 |
| HTTP 出口 | `app.py` | `/auth/register` `/auth/login` `/me` `/dev/login` | 前端只看到这层 |

关键设计（auth.py 模块注释原文思想）：**token 一律由 auth.py 签发，签发入口可替换**——
可以是密码登录、DEV_MODE 下的 mock `/dev/login`、将来是企业微信 OAuth 回调。
前端只认 `{token, role}`，身份源怎么换都不影响其他代码——叫 **"换证"思想**。

## 二、数据表 users（`src/db.py:154`）

```sql
id            TEXT PRIMARY KEY      -- uuid4().hex，32位无横线
username      TEXT UNIQUE NOT NULL  -- 登录名（小写归一化）
password_hash TEXT NOT NULL         -- bcrypt 哈希，绝不存明文
display_name  TEXT NOT NULL DEFAULT ''
role          TEXT NOT NULL DEFAULT 'customer'   -- customer | admin
status        TEXT NOT NULL DEFAULT 'active'     -- active | disabled
created_at    TEXT NOT NULL
```

细节：
- **id 不用自增**，用 `uuid.uuid4().hex`：对外 user_id 之后要当**行级隔离 key** 和
  **LangGraph thread_id** 用，随机 id 防枚举；32 位 hex 无横线是特意对齐
  `src/user_identity.py` 的字符集（见 12 步）。
- `created_at` 存 **TEXT + ISO 字符串**：从 SQLite 迁来的统一约定（可吐槽点，取舍见 Q6）。
- `username` UNIQUE + 应用层小写归一化双保险，防大小写撞号。

## 三、注册：POST /auth/register（`app.py:180`）

```python
@app.post("/auth/register")
async def auth_register(body: RegisterBody):
    try:
        pub = await create_user(body.username, body.password, body.display_name)
    except UsernameTaken as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(pub["user_id"], "register", detail=f"username={pub['username']}")
    return _issue_token(pub)
```

拆三段：**创建 → 审计留痕 → 自动签发 token（注册即登录）**。
错误映射讲究：业务层抛业务异常，HTTP 层翻译状态码——
`UsernameTaken` → **409 Conflict**（不是 400：语义是"资源已存在"，前端据此区分）；
`ValueError` → 400。**业务层不认识 HTTP 概念**，app.py 是唯一懂 HTTP 的地方（分层）。

### create_user 校验链（`src/users.py:114`）

1. `validate_username`：正则 `^[A-Za-z0-9_-]{3,32}$`，先 `strip().lower()` 归一化；
2. `validate_password`：最短 6 位；**最长 72 位且拒绝而非截断**（72 是 bcrypt 输入硬上限，
   静默截断会让"两把不同钥匙开同一把锁"）；
3. `validate_display_name`：≤32 字符，空则回退 username；
4. 角色白名单（双保险，端点传不进来）；
5. `get_user_by_username` 查重 → 命中抛 `UsernameTaken`（并发兜底靠数据库 UNIQUE）；
6. `uuid.uuid4().hex` 生成 id + `hash_password` 算哈希 + INSERT。

### 密码哈希（`src/users.py:44`）

```python
BCRYPT_ROUNDS = 12   # 成本因子：单次校验约 0.2-0.3s，暴力破解很贵
def hash_password(password):
    return bcrypt.hashpw(password.encode("utf-8"),
                         bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")
```

- bcrypt **自带随机盐** → 不需单独盐字段；同密码两次注册哈希不同（防彩虹表）。
- `verify_password` 捕获异常返回 False 不抛错 → 不泄露内部信息。
- `to_public()`（users.py:102）显式白名单视图，**绝不带 password_hash 出模块**。

## 四、登录：POST /auth/login（`app.py`，2026-09 改为签**双凭证**）

```python
@app.post("/auth/login")
async def auth_login(body: LoginBody, request: Request):
    pub = await auth_user(body.username, body.password)
    if not pub:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    await _audit(pub["user_id"], "login", detail=f"username={pub['username']}")
    return await _issue_login(pub, request)
```

`_issue_login` 先建**服务端会话**（Redis）再签 access：

```python
sess = await auth_sessions.create_session(user_id, role, ua=..., ip=...)
access = create_token(user_id, role=role, sid=sess["sid"])
return {**pub, "access_token": access, "token": access,          # token 为旧别名（兼容）
        "refresh_token": sess["refresh_token"], "expires_in": ACCESS_TTL_SECONDS,
        "sid": sess["sid"], "token_type": "bearer"}
```

响应里 `sid` 是**登录会话 id**（与聊天会话 `session_id` 不是一回事，别混）。

`auth_user`（users.py:140）：查用户名 → 无此人 / `status != 'active'` / 密码错 → 一律 None。
**防用户枚举**：三种失败同一个 401 同一句文案。诚实取舍：用户不存在时提前 return
没走 bcrypt，响应时间有微妙差异（几十 ms vs 0.3s），严格要"假哈希校验"抹平时延——
演示量级没做（面试主动说出这点是加分项）。

## 五、签发与验签（`src/auth.py`）

签发（access token）：

```python
payload = {
    "sub": user_id, "role": role,
    "typ": "access",                 # 类型校验：refresh 串不得当 access 用
    "jti": uuid.uuid4().hex,         # 唯一 id（黑名单/追溯用）
    "iat": int(now.timestamp()),
    "exp": now + timedelta(seconds=ACCESS_TTL_SECONDS),   # 默认 15 分钟
    "iss": JWT_ISSUER, "aud": JWT_AUDIENCE,               # 防跨系统串用
    "sid": sid,                      # 属于哪个登录会话（/auth/sessions 标"当前"）
}
return jwt.encode(payload, _secret(), algorithm="HS256")
```

验签：`jwt.decode(..., leeway=CLOCK_SKEW_SECONDS)` 一步验**签名 + 过期（含时钟偏移容忍）**；
`ExpiredSignatureError` → 401"登录已过期"；`InvalidTokenError` → 401"无效凭证"；
再手工校验 `typ`/`iss`/`aud`/`role`（**存在才校验**：滚动升级期间老 token 没有这些
claim 仍可用，最多再活一个 TTL，不会一上线就把所有人踢下线）。

**这个函数是纯函数——不查 Redis、不查 PG。** 这是 access 无状态的收益（热路径零 IO），
也是它的代价（不能单独撤销）。撤销能力全部放在 refresh 那一侧，见下一节。

密钥 `_secret()` 三态：设了 `JWT_SECRET` 用它；DEV_MODE 缺失用开发默认并告警；
**生产缺失直接 RuntimeError 拒绝启动**（fail-closed）。

## 五之二、refresh：为什么它必须有状态（`src/auth_sessions.py`）

**JWT 的问题不是强度，是撤销粒度**：签发即不可撤。把撤销能力挪到 refresh 上，
撤销窗口就被压缩成一个 access TTL（15 分钟）——这就是"短期 access + 可轮换 refresh"。

```
access  = JWT，15 分钟，无状态，验签零查询        ← 撤销窗口 = 它的 TTL
refresh = 不透明串 "{sid}.{secret}"，存 Redis     ← 撤销 = 删一个 key，立即生效
```

Redis 结构（与缓存 `study1:chat:*` 分家）：

```
study1:auth:sess:{sid}  → HASH{user_id, role, hash, prev_hash, prev_at, born_at, last_at, ua, ip}
study1:auth:user:{uid}  → SET{sid...}   （列会话 / 全端下线）
```

- **只存 SHA-256 哈希**：Redis 泄露 ≠ 会话可被直接冒用。
- **轮换**（RFC 9700 §2.2.2 对 public client 是 MUST）：每次刷新作废旧的、发新的。
- **重用检测**：已轮换掉的 refresh 再次出现 → 判定泄露 → **整个会话作废**
  （Auth0 的 Automatic Reuse Detection 同理）。
- **竞态宽限**（`REFRESH_ROTATION_LEEWAY_SECONDS`，默认 30s，左闭右开）：
  刚轮换掉的旧 token 在窗口内重现 → 当作并发刷新放行，不触发"核弹"
  （Okta 的 rotation leeway 0–60s 同理）。**前端单飞才是第一道防线**，宽限只是安全网。
- **双封顶**：空闲期 14 天（用一次续一次）+ 绝对上限 30 天（自登录起，防"活跃用户
  被无限续命"）。少了绝对上限，滑动续期就等于永久会话。
- **原子性**：判定 + 换发 + 续期写成**一段 Lua** 交给 Redis 执行——拆成多条命令会出现
  "两个并发刷新都认为自己是合法的"竞态。

端点：

| 端点 | 作用 | 失败语义 |
|---|---|---|
| `POST /auth/refresh` | 轮换 refresh + 换发 access | 401（无效/过期/重用）/ 503（Redis 不可用） |
| `POST /auth/logout` | **撤销服务端会话** | 503（Redis 不可用） |
| `GET /auth/sessions` | 我在哪些设备/标签登着（标 `current`） | 401 |
| `DELETE /auth/sessions/{sid}` | 踢掉某个会话（校验归属，防越权） | 404 / 401 |

**fail-closed 是刻意的**：缓存（`src/memory.py`）Redis 挂了回源 PG 继续跑（fail-open），
但鉴权不能 fail-open——放行等于"任何 refresh 都能换到 access"。所以 Redis 从"可选缓存"
升级成"关键路径依赖"：挂掉时登录/刷新 503，**但已签发的 access 仍有效**（无状态，
最多再活 15 分钟）。配套地，`study1:auth:*` 必须开 AOF 持久化，不能和纯缓存共用同一套
`allkeys-lru` 策略（LRU 会把没登出的会话挤掉，表现为随机掉线）。

## 六、认证 vs 授权：依赖注入（`src/auth.py:83`）

```python
def get_current_user(authorization: str = Header(default="")) -> dict:
    # 认证 Authentication：你是谁？—— 无 token → 401
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token: raise HTTPException(401, "未登录")
    return decode_token(token)

def require_role(role: str):
    # 授权 Authorization：你能干什么？—— 认证过了但不够格 → 403
    def checker(user: dict = Depends(get_current_user)) -> dict:
        if user.get("role") != role:
            raise HTTPException(403, "权限不足")
        return user
    return checker

require_admin = require_role("admin")
```

面试必答点：
- **401 vs 403**：401 = 没证明你是谁；403 = 认识你但没资格。项目严格区分。
- `require_role` = **工厂 + 闭包**：`require_role("admin")` 返回依赖，内部再
  `Depends(get_current_user)`——FastAPI 先解析内层（认证）再查角色（授权）。
- **授权永远在服务端**：前端 role 只决定 UI 显隐；接口一律 `Depends` 兜底。
  你在 DevTools 把 role 改成 admin，接口照样 403。

用法：

```python
@app.get("/me")  def me(user: dict = Depends(get_current_user)): ...          # 登录即可
@app.get("/approval/pending") def f(admin: dict = Depends(require_admin)):    # 仅管理员
```

## 七、管理员从哪来 + 审计

- 注册**只能建 customer**：端点没暴露传 role 的通道。admin 由 `scripts/create_admin.py`
  幂等创建/重置（读 `ADMIN_*` 环境变量，存在则 UPDATE 保 admin 角色）。**防越权注册**。
- 注册/审批等动作写 `audit_log` 表（app.py:235 `_audit`）：actor/action/detail/时间。
  设计点：**审计失败不影响主流程**（try/except 包住只打印）——审计是增强不是关键路径。

## 八、前端一句话（细节见 13 步）

`api.js`：login/register 成功 → access + refresh 都存 **sessionStorage**（每个标签页独立，
可同时开客户/管理员两个窗口；localStorage 同源共享会被互相顶掉）→ 请求带
`Authorization: Bearer <access>`。

**401 处理是重点**：`authFetch` 遇到 401 → 刷新 → 用新 access 重放一次；刷新失败 →
清本地 + 派发 `auth:expired` 事件 → App 回登录页。两个细节：

1. **单飞（single-flight）**：并发 N 个请求同时 401，只允许打**一次** `/auth/refresh`。
   否则第二个请求会拿已被轮换作废的旧 refresh → 服务端判"重用" → **整个会话被注销**，
   用户会莫名其妙被登出。用共享 promise 实现，并有测试守着
   （`web/test/api.singleflight.test.mjs`，`npm test`）。
2. **轮换后的 refresh 必须写回**：每次刷新服务端都换新 refresh，前端不写回就等于
   永远拿旧的在用（第二次刷新必判重用）。

登出**必须调服务端**（`POST /auth/logout` 撤会话）——旧实现"服务端无状态、无需调接口"
那句注释，在引入 refresh 之后就不成立了：不撤的话，被拷走的 refresh 在空闲期内一直是
活凭证。

---

## Q&A

**Q1：为什么注册冲突返回 409 而不是 400？**
400 = 请求本身格式不对（Bad Request）；409 = 请求没问题，但和**当前资源状态冲突**
（Conflict）——用户名已被占用是后者。语义化状态码让前端能精确区分提示：
409 → "换个用户名"，400 → "你填的不合规"。

**Q2：数据库存的是明文密码吗？为什么？**
不是，存 bcrypt 哈希（`$2b$12$...`）。理由：数据库一旦被拖库，明文直接泄露；
哈希 + 随机盐让攻击者只能逐个暴力试（成本因子 12 → 单个约 0.2-0.3s，一万个密码
要很久），且同密码哈希不同，彩虹表失效。密码哈希**绝不**出现在任何接口响应里
（`to_public` 白名单视图保证）。

**Q3：为什么密码最长限制 72 位，而不是不限？**
bcrypt 只取输入前 72 字节，超出部分被静默忽略——那"123456…（73位）+A"和
"123456…（73位）+B"会是同一个哈希，等于两把钥匙开一把锁。所以超长直接拒绝，
而不是静默截断制造安全隐患。

**Q4：JWT 和传统 session 方案的区别？为什么这里用 JWT？**
session：服务端存会话 + 下发随机 session_id，登出/踢人靠删服务端记录；JWT：无状态，
服务端只验签不存会话——代价是**签发即不可撤销**。

**2026-09 的答案已经变了**：现在用"短期 access（15 分钟，无状态）+ 可轮换 refresh
（存 Redis，可撤销）"。撤销能力挪到 refresh 上，撤销窗口被压到一个 access TTL；
而 refresh 本身轮换 + 重用检测。要说清的是**代价里还剩什么**：access 在 TTL 内仍不可撤
（改密码/封号的生效延迟 ≤15 分钟），要秒级生效就得再加黑名单或 token_version，
那是"每请求一次 Redis + fail-closed 依赖"的额外开销，收益不成正比，所以没做。

**这三条路要能横向比较**（面试常追问）：① 服务端会话（opaque session + Redis）——
单体 Web 最省事、撤销免费，JWT 的无状态优势在浏览器场景根本用不上；② short access +
rotating refresh（本项目）；③ BFF/token handler —— token 完全不进浏览器，SPA 的现行
推荐方向。选 ② 是因为要多端 + 未来接企业微信 OIDC，同时保留 JWT 的"换证"弹性。

**Q5：前端把 token 存 sessionStorage 安全吗？**
对 XSS 不安全（脚本能读 sessionStorage）。更安全是 httpOnly Cookie（脚本读不到），
但要处理 CSRF（SameSite 等），而且 cookie 同源全标签共享 → **会失去"多标签分别登录
不同账号"这个刻意做出来的体验**；另外 `Secure` 属性要求 HTTPS，公网 http 裸奔时根本
用不了。本项目选 sessionStorage 是明确权衡（配 CSP 缓解 XSS）；知道"refresh 进
httpOnly Cookie / 更彻底走 BFF"是更优生产方案，就是这条的加分答案。

**Q6：created_at 为什么存字符串不存数据库时间类型？**
历史包袱：从 SQLite 迁移（SQLite 时间存文本方便）；全项目统一 ISO 字符串后跨库
迁移零转换。缺点：不能直接用 PG 时间函数排序/运算。面试可答"这是从 SQLite 演进
留下的约定，新表我会用 timestamptz"——承认取舍比装没看见更可信。

**Q7（追问）：登录接口怎么防暴力破解？**
当前没有限流/失败锁定（users.py 注释自认"演示项目量级；生产可接强度策略/限流"）。
业界方案：IP/账号维度限流（如 5 次/分钟）、失败递增退避、验证码、账号锁定。
另外 bcrypt 成本因子本身就抬高单次尝试成本——慢哈希即第一道防线。

**Q8：refresh 存 Redis，那 Redis 挂了会怎样？**
登录/刷新 503（**fail-closed**），已签发的 access 在 15 分钟内仍能用，之后全员需要
重新登录。这是刻意选的：鉴权 fail-open 等于"任何 refresh 都能换到 access"。
对比 L1 热缓存是 fail-open（回源 PG 即可）——**同一个 Redis，两个用途的失败语义相反**，
所以生产建议分实例/分库，会话那个开 AOF。
