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

## 四、登录：POST /auth/login（`app.py:193`）

```python
@app.post("/auth/login")
async def auth_login(body: LoginBody):
    pub = await auth_user(body.username, body.password)
    if not pub:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return _issue_token(pub)
```

`auth_user`（users.py:140）：查用户名 → 无此人 / `status != 'active'` / 密码错 → 一律 None。
**防用户枚举**：三种失败同一个 401 同一句文案。诚实取舍：用户不存在时提前 return
没走 bcrypt，响应时间有微妙差异（几十 ms vs 0.3s），严格要"假哈希校验"抹平时延——
演示量级没做（面试主动说出这点是加分项）。

## 五、签发与验签（`src/auth.py`）

签发（auth.py:55）：

```python
payload = {
    "sub": user_id,     # JWT 标准 subject
    "role": role,       # customer | admin
    "iat": int(now.timestamp()),
    "exp": now + timedelta(hours=expire_hours),   # 默认 8 小时
}
return jwt.encode(payload, _secret(), algorithm="HS256")
```

验签（auth.py:67）：`jwt.decode` 一步验**签名 + 过期**；`ExpiredSignatureError` → 401
"登录已过期"；`InvalidTokenError` → 401"无效凭证"；**角色再做白名单二次校验**——
payload 只防篡改不防人为构造，decode 侧兜底（纵深防御）。

密钥 `_secret()`（auth.py:30）三态：设了 `JWT_SECRET` 用它；DEV_MODE 缺失用开发默认并
告警（本地无 .env 能跑）；**生产缺失直接 RuntimeError 拒绝启动**（fail-closed）。

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

`api.js`：login/register 成功 → token 存 **sessionStorage**（每个标签页独立，可同时开
客户/管理员两个窗口；localStorage 同源共享会被互相顶掉）→ 请求带
`Authorization: Bearer <token>`。登出只清本地 token（服务端无状态，JWT 无 session 可销毁）。

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
session：服务端存会话 + 下发随机 session_id，登出/踢人靠删服务端记录；
JWT：无状态，服务端只验签不存会话。代价：**JWT 无法主动吊销**——改密码后旧 token
8 小时内仍有效（本项目演示量级未做黑名单，属诚实取舍，见 18 步坑清单）。
选 JWT 的原因：多端（Web + 未来企业微信）+ 服务端无状态易水平扩展 + 鉴权逻辑集中
在 auth.py 一个模块（配合"换证"思想，未来接 OAuth 只改签发入口）。

**Q5：前端把 token 存 sessionStorage 安全吗？**
对 XSS 不安全（脚本能读 sessionStorage）。更安全是 httpOnly Cookie（脚本读不到），
但要处理 CSRF（SameSite 等）。本项目选 JWT + 前端存储是简化权衡；
能说出"httpOnly + SameSite 是更优生产方案"体现你懂安全纵深。

**Q6：created_at 为什么存字符串不存数据库时间类型？**
历史包袱：从 SQLite 迁移（SQLite 时间存文本方便）；全项目统一 ISO 字符串后跨库
迁移零转换。缺点：不能直接用 PG 时间函数排序/运算。面试可答"这是从 SQLite 演进
留下的约定，新表我会用 timestamptz"——承认取舍比装没看见更可信。

**Q7（追问）：登录接口怎么防暴力破解？**
当前没有限流/失败锁定（users.py 注释自认"演示项目量级；生产可接强度策略/限流"）。
业界方案：IP/账号维度限流（如 5 次/分钟）、失败递增退避、验证码、账号锁定。
另外 bcrypt 成本因子本身就抬高单次尝试成本——慢哈希即第一道防线。
