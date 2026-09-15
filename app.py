"""
纺织客服 Web UI
===============
FastAPI + 原生 HTML，模仿企业微信界面

运行: python app.py → http://127.0.0.1:8000
"""

from contextlib import asynccontextmanager
import os
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.types import Command
from src.agent import build_graph, get_cheap_llm, thread_config
from src.approval import (
    find_approval_id, find_pending_draft, pending_reply_text, set_pending_session,
)
from src.auth import (
    ACCESS_TTL_SECONDS, AuthError, create_token, get_current_user, require_admin,
)
from src import admin_orders as admin_orders_mod
from src import approval, auth_sessions, rate_limit
from src.analytics import graph as analytics_graph
from src.logging_config import get_logger

logger = get_logger(__name__)
from src.db import execute, query_all
from src.memory import get_user, try_ping
from src.mcp_client import get_mcp, init_mcp
from src.stream_chat import stream_chat
from src.user_identity import is_valid_user_id
from src.sessions import (
    create_session, delete_session, get_owned_session,
    list_sessions, rename_session, touch_session,
)
from src.users import (
    auth_user, create_user, get_user_by_id, UsernameTaken,
)

# ── 鉴权开关：DEV_MODE=1 时注册 /dev/login（mock 微信身份，开发/演示用）──
DEV_MODE = os.getenv("DEV_MODE") == "1"

# ── MCP 工具层：lifespan 异步初始化（事件循环内连接，关闭时释放）──
SERVERS = {
    "product": ["python3", "src/mcp_servers/product_server.py"],
    "order":   ["python3", "src/mcp_servers/order_server.py"],
    "refund":  ["python3", "src/mcp_servers/refund_server.py"],
    # 只读分析取数（管理员数据分析 Agent 用；客服 Agent 不会拿到它的工具，
    # 因为各 Agent 都是按名字显式挑选工具的，见 agent.py get_tools_for_langchain）
    "analytics": ["python3", "src/mcp_servers/analytics_server.py"],
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🔌 连接 MCP 工具服务器...")
    await init_mcp(SERVERS)
    # L1 热缓存探活：成功打一行"Redis 就绪"，失败自动降级并告警（超时有上限，不拖启动）
    await try_ping()
    yield
    from src.mcp_client import get_mcp
    await get_mcp().shutdown()


app = FastAPI(title="交易智能体", lifespan=lifespan)
agent_graph = build_graph()

# ── CORS：允许独立前端 (Vite dev server) 跨域访问 ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应收紧为具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 用户身份：一律来自 JWT（Authorization: Bearer <token>）──
# 2026-09：去掉 X-User-Id / guest 回退（防任意冒充），无 token 一律 401。
# 受保护端点统一声明 `user: dict = Depends(get_current_user)`，
# user["user_id"] 即账号 uuid，行级隔离 key 与 thread_id 顺延用它。
# /auth/*、/healthz 与静态页保持公开；/dev/login 仅 DEV_MODE=1 注册（本地演示用）。


class ChatRequest(BaseModel):
    message: str
    session_id: str = ""   # 多会话（2026-09）：空/缺省 → 'default' 遗留会话


async def _resolve_session(user_id: str, session_id: str) -> str:
    """解析并校验会话归属：'default' 始终放行（历史数据），其余必须属于该用户。"""
    sid = (session_id or "").strip() or "default"
    if sid != "default" and not await get_owned_session(user_id, sid):
        raise HTTPException(status_code=404, detail="会话不存在")
    return sid


@app.post("/chat")
async def chat(req: ChatRequest, user: dict = Depends(get_current_user)):
    import asyncio
    # ⚠️ 限流必须放在下面那个 try/except **之外**：/chat 的兜底 except 会把异常吞成
    # 200 + "系统异常…"，那样客户端永远看不到 429（这个坑是测试抓出来的）。
    await rate_limit.enforce("chat_user", user["user_id"], rate_limit.CHAT_USER,
                             "消息发送过于频繁")
    try:
        user_id = user["user_id"]
        session_id = await _resolve_session(user_id, req.session_id)
        memory = get_user(user_id)
        config = thread_config(user_id)

        # 挂起态守卫：该用户有订单在待审批 → 不跑图，直接提示。
        # 2026-09：**以 PG 为准**（重启后仍然有效），图 checkpoint 只作二次确认。
        row = await approval.get_pending(user_id)
        if row:
            return {"reply": pending_reply_text(row["draft"]), "pending": True,
                    "draft": row["draft"], "approval_id": row["id"]}

        snap = await agent_graph.aget_state(config)
        draft = find_pending_draft(getattr(snap, "interrupts", None)) if snap else None
        if draft:
            # 反向自愈：图里还挂着、但 DB 没登记（历史数据/异常路径）→ 立刻补登记，
            # 免得这一单又变成"只在内存里"的定时炸弹
            healed = await approval.register_pending(
                user_id, user_id, draft, args={}, session_id=session_id)
            return {"reply": pending_reply_text(draft), "pending": True,
                    "draft": draft, "approval_id": healed.get("id", "")}

        # 加载历史 + 偏好（异步）
        history = await memory.load_recent(20, session_id)
        prefs = await memory.retrieve_preferences()
        user_context = "；".join(prefs) if prefs else ""

        messages = history + [HumanMessage(content=req.message)]
        last_type = await memory.get_last_query_type()

        state = {"messages": messages, "knowledge_chunks": [], "rewrite_query": "",
                 "query_type": last_type, "user_id": user_id, "user_context": user_context,
                 "session_id": session_id}
        result = await agent_graph.ainvoke(state, config=config)

        # ── HITL：下单挂起，等人工审批 ──
        draft = find_pending_draft(result.get("__interrupt__"))
        if draft:
            reply = pending_reply_text(draft)
            await memory.save_last_query_type("chat")
            await memory.save_messages(
                [HumanMessage(content=req.message), AIMessage(content=reply)], session_id)
            await set_pending_session(user_id, session_id)
            await touch_session(user_id, session_id, req.message)
            return {"reply": reply, "pending": True, "draft": draft,
                    "approval_id": find_approval_id(result.get("__interrupt__"))}

        # 保存本轮状态供下一轮延续
        await memory.save_last_query_type(result.get("query_type", "chat"))

        # 存档 + 异步提取偏好（后台协程）
        await memory.save_messages(
            [HumanMessage(content=req.message), result["messages"][-1]], session_id)
        await touch_session(user_id, session_id, req.message)
        asyncio.create_task(memory.extract_and_store(result["messages"], get_cheap_llm()))

        return {"reply": result["messages"][-1].content}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"reply": f"系统异常: {str(e)[:200]}, 请稍后重试"}


@app.get("/history")
async def get_history(user: dict = Depends(get_current_user), session_id: str = ""):
    user_id = user["user_id"]
    session_id = await _resolve_session(user_id, session_id)
    memory = get_user(user_id)
    rows = await memory.load_recent(30, session_id)
    return [{"role": m.type, "content": m.content} for m in rows]


# ════════════════════════════════════════════════════════════
# 账号：注册 / 登录 / 刷新 / 登出 / 会话管理（2026-09 路线②）
# ── POST /auth/register   开放注册（仅 customer）→ 自动登录签发双凭证
# ── POST /auth/login      用户名 + 密码 → {access_token, refresh_token, ...}
# ── POST /auth/refresh    refresh → 新的 access + refresh（轮换 + 重用检测）
# ── POST /auth/logout     撤销服务端会话（refresh 立即失效）
# ── GET  /auth/sessions   我在哪些设备/标签登着
# ── DELETE /auth/sessions/{sid}  踢掉某个会话
# ── GET  /me              登录态探测（无 token → 401，前端据此跳登录页）
# ── POST /dev/login      仅 DEV_MODE=1 注册（本地演示便利，生产消失）
#
# 凭证分工：access = JWT，15 分钟，无状态（验签零查询，所以不可单独撤销）；
#          refresh = 不透明随机串，存 Redis，可轮换/可撤销（撤销窗口 = 1 个 access TTL）。
# ════════════════════════════════════════════════════════════

class RegisterBody(BaseModel):
    username: str
    password: str
    display_name: str = ""


class LoginBody(BaseModel):
    username: str
    password: str


class RefreshBody(BaseModel):
    refresh_token: str = ""


class LogoutBody(BaseModel):
    refresh_token: str = ""


class ChangePasswordBody(BaseModel):
    old_password: str
    new_password: str


class StatusBody(BaseModel):
    status: str          # active | disabled


@app.exception_handler(rate_limit.RateLimited)
async def _rate_limited(request: Request, exc: rate_limit.RateLimited):
    """限流 → 429 + Retry-After（前端可据此倒计时再试）。"""
    return JSONResponse(
        status_code=429,
        content={"detail": exc.detail, "code": "rate_limited", "retry_after": exc.retry_after},
        headers={"Retry-After": str(exc.retry_after)},
    )


@app.exception_handler(auth_sessions.AuthBackendUnavailable)
async def _auth_backend_unavailable(request: Request, exc: auth_sessions.AuthBackendUnavailable):
    """会话存储不可用 → 503。

    **fail-closed**：鉴权路径不能像 L1 缓存那样 fail-open（回源即可），
    放行等于"任何 refresh 都能换到 access"。已签发的 access 仍有效（无状态）。
    """
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(auth_sessions.RefreshInvalid)
async def _refresh_invalid(request: Request, exc: auth_sessions.RefreshInvalid):
    """refresh 无效/过期/被撤销/疑似泄露 → 401（带 code 便于前端区分处置）。"""
    if exc.reuse:
        # 安全事件必须留痕：谁、什么时候、因为什么被判为凭证泄露
        await _audit(exc.user_id or "unknown", "refresh_reuse_detected", detail=exc.code)
    return JSONResponse(
        status_code=401,
        content={"detail": exc.detail, "code": exc.code, "reuse": exc.reuse},
    )


def _client_meta(request: Request) -> tuple:
    """取 UA / IP（存进会话记录，供"登录设备"列表展示与审计）。"""
    if request is None:
        return "", ""
    ua = request.headers.get("user-agent", "") if request.headers else ""
    ip = (request.client.host if request.client else "") or ""
    return ua, ip


def _client_ip(request: Request) -> str:
    """限流用的客户端标识。

    只取 TCP 对端地址（``request.client.host``），**不信任 X-Forwarded-For**：
    该头可被伪造，直接采信等于让攻击者随便换 IP 绕过限流。
    生产在反向代理后面时，应改成"只信任已知代理传来的 XFF"（见 docs 部署说明）。
    """
    return (request.client.host if request and request.client else "") or "unknown"


async def _revoke_user_sessions(user_id: str) -> int:
    """把某用户的**所有**凭证一次性作废（改密码 / 封号 / 强制下线走这里）。

    两步缺一不可：
    1. ``bump_token_version`` —— 让**已签发的 access token** 立刻失效
       （access 无状态本来撤不了，靠版本号对不上来判定）；
    2. ``revoke_all`` —— 删掉所有 refresh 会话，让客户端没法再刷新续命。
    """
    from src.users import bump_token_version
    version = await bump_token_version(user_id)
    await auth_sessions.invalidate_token_version(user_id)   # 本进程缓存立刻失效
    revoked = await auth_sessions.revoke_all(user_id)
    # TODO(多副本)：版本号变更通过 Redis 传播，其他副本 ≤60s 收敛（见 AUTH_VERSION_CACHE_SECONDS）
    logger.info("[鉴权] 用户 %s 凭证已全部作废（ver=%s，撤销会话 %s 个）",
                user_id, version, revoked)
    return revoked


async def _issue_login(user_pub: dict, request: Request) -> dict:
    """登录成功：建服务端会话 → 签发 access + refresh。

    前端只认 {access_token, refresh_token, role}（auth.py「换证」思想）。
    ``token`` 字段是 access 的旧别名（兼容未升级的客户端，后续可移除）。
    """
    from src.users import get_token_version
    ua, ip = _client_meta(request)
    sess = await auth_sessions.create_session(
        user_pub["user_id"], user_pub["role"], ua=ua, ip=ip)
    ver = await get_token_version(user_pub["user_id"])
    access = create_token(user_pub["user_id"], role=user_pub["role"],
                          sid=sess["sid"], ver=ver)
    return {
        **user_pub,
        "access_token": access,
        "token": access,                        # 兼容旧前端（deprecated）
        "refresh_token": sess["refresh_token"],
        "token_type": "bearer",
        "expires_in": ACCESS_TTL_SECONDS,
        "sid": sess["sid"],
    }


@app.post("/auth/register")
async def auth_register(body: RegisterBody, request: Request):
    """开放注册：角色强制 customer（admin 只能由 scripts/create_admin.py 创建）。"""
    await rate_limit.enforce("register_ip", _client_ip(request), rate_limit.REGISTER_IP,
                             "注册过于频繁")
    try:
        pub = await create_user(body.username, body.password, body.display_name)
    except UsernameTaken as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(pub["user_id"], "register", detail=f"username={pub['username']}")
    return await _issue_login(pub, request)


@app.post("/auth/login")
async def auth_login(body: LoginBody, request: Request):
    """登录：先过限流与失败锁定，校验通过签发双凭证；失败统一 401（不泄露用户名是否存在）。

    三层防护（顺序有意义）：
    1. **同 IP 速率**（默认 20 次/分钟）—— 挡住脚本高频爆破；
    2. **同 账号+IP 失败锁定**（默认 15 分钟内 5 次）—— 挡住针对单账号的慢速爆破；
       按"账号+IP"而不是只按账号，是为了避免"攻击者故意失败把别人账号锁死"（DoS）；
    3. 登录成功即清零失败计数（不惩罚正常用户的手滑）。
    """
    ip = _client_ip(request)
    await rate_limit.enforce("login_ip", ip, rate_limit.LOGIN_IP, "登录请求过于频繁")

    fail_key = f"{body.username.strip().lower()}|{ip}"
    decision = await rate_limit.hit("login_fail", fail_key, rate_limit.LOGIN_FAIL)
    if not decision.allowed:
        await _audit(body.username.strip().lower(), "login_locked",
                     detail=f"ip={ip} retry_after={decision.retry_after}")
        raise rate_limit.RateLimited(
            f"失败次数过多，账号已临时锁定（{decision.retry_after} 秒后可重试）",
            decision.retry_after)

    pub = await auth_user(body.username, body.password)
    if not pub:
        await _audit(body.username.strip().lower(), "login_failed", detail=f"ip={ip}")
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    await rate_limit.reset("login_fail", fail_key)      # 成功即清失败计数
    await _audit(pub["user_id"], "login", detail=f"username={pub['username']}")
    return await _issue_login(pub, request)


@app.post("/auth/refresh")
async def auth_refresh(body: RefreshBody, request: Request):
    """刷新：轮换 refresh 并换发新 access。

    - 旧的 refresh 立即作废（RFC 9700 对 public client 要求轮换）
    - 旧 refresh 被重放（超出宽限窗口）→ 视为泄露 → 作废整个会话（401 + reuse）
    - Redis 不可用 → 503（fail-closed，由异常处理器统一转）
    """
    await rate_limit.enforce("refresh_sid", body.refresh_token.split(".", 1)[0] or "unknown",
                             rate_limit.REFRESH_SESSION, "刷新过于频繁")
    rotated = await auth_sessions.rotate(body.refresh_token)
    if rotated.get("grace"):
        # 并发刷新的宽限命中：不是错，但值得观测（前端单飞失效时会看到）
        await _audit(rotated["user_id"], "refresh_grace", detail="并发刷新宽限命中")

    # 账号状态复核：封号在这里立刻生效（access 另有 token_version 兜底）
    from src.users import get_token_version
    row = await get_user_by_id(rotated["user_id"])
    if row is not None and row.get("status") != "active":
        await auth_sessions.revoke_all(rotated["user_id"])
        raise HTTPException(status_code=401, detail="账号已被禁用，请联系管理员")

    role = (row or {}).get("role") or rotated["role"]
    ver = await get_token_version(rotated["user_id"])
    access = create_token(rotated["user_id"], role=role, sid=rotated["sid"], ver=ver)
    return {
        "access_token": access,
        "token": access,                        # 兼容旧前端（deprecated）
        "refresh_token": rotated["refresh_token"],
        "token_type": "bearer",
        "expires_in": ACCESS_TTL_SECONDS,
        "sid": rotated["sid"],
    }


@app.post("/auth/logout")
async def auth_logout(body: LogoutBody = LogoutBody(), authorization: str = Header(default="")):
    """登出：**撤销服务端会话**（refresh 当场失效）。

    前端仍要清本地存储；服务端撤销才是"token 被拷走也没用"的那一半。
    带 refresh_token 就撤它；没带则用 access 里的 sid 兜底（仍需认证）。
    """
    revoked = False
    actor = ""
    if body.refresh_token:
        sid = body.refresh_token.split(".", 1)[0]
        await auth_sessions.revoke(body.refresh_token)
        revoked = True
        actor = sid
    else:
        user = await get_current_user(authorization)   # 无 token → 401
        if user.get("sid"):
            revoked = await auth_sessions.revoke_session(user["user_id"], user["sid"])
            actor = user["user_id"]
    await _audit(actor or "unknown", "logout", detail=f"revoked={revoked}")
    return {"ok": True, "revoked": revoked}


@app.get("/auth/sessions")
async def auth_sessions_list(user: dict = Depends(get_current_user)):
    """当前有效登录会话列表（会话管理 / "我在哪些设备登着"）。"""
    items = await auth_sessions.list_sessions(user["user_id"], current_sid=user.get("sid", ""))
    return {"sessions": items, "count": len(items)}


@app.delete("/auth/sessions/{sid}")
async def auth_sessions_revoke(sid: str, user: dict = Depends(get_current_user)):
    """踢掉某个会话（校验归属，防越权）。"""
    if not await auth_sessions.revoke_session(user["user_id"], sid):
        raise HTTPException(status_code=404, detail="会话不存在")
    await _audit(user["user_id"], "revoke_session", detail=sid)
    return {"ok": True}


@app.post("/auth/logout-all")
async def auth_logout_all(user: dict = Depends(get_current_user)):
    """所有设备/标签一起下线（含"当前这台"）—— 怀疑账号被盗时的第一反应。

    与改密码/封号共用同一套作废逻辑：bump token_version（连已签发的 access 也失效）
    + 撤销全部 refresh 会话。
    """
    revoked = await _revoke_user_sessions(user["user_id"])
    await _audit(user["user_id"], "logout_all", detail=f"revoked={revoked}")
    return {"ok": True, "revoked": revoked}


@app.post("/auth/change-password")
async def auth_change_password(body: ChangePasswordBody, request: Request,
                               user: dict = Depends(get_current_user)):
    """改密码：验旧密码 → 改哈希 → **全端下线** → 给当前设备换发新凭证。

    为什么必须全端下线：密码变更通常意味着"怀疑泄露"。如果别处的 refresh 还有效，
    改密码就只是安慰剂。当前设备换发新凭证（而不是把用户也踢去重登）是按主流体验做的
    （GitHub/Google 改密后其他设备下线、当前会话继续）。
    """
    from src.users import set_password, verify_user_password

    uid = user["user_id"]
    if not await verify_user_password(uid, body.old_password):
        await _audit(uid, "change_password_failed", detail="旧密码错误")
        raise HTTPException(status_code=401, detail="旧密码不正确")
    if body.new_password == body.old_password:
        raise HTTPException(status_code=400, detail="新密码不能与旧密码相同")
    try:
        await set_password(uid, body.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    revoked = await _revoke_user_sessions(uid)          # 全端下线（含 access）
    await _audit(uid, "change_password", detail=f"revoked={revoked}")

    row = await get_user_by_id(uid)
    pub = {"user_id": uid, "role": (row or {}).get("role", user["role"]),
           "username": (row or {}).get("username", ""),
           "display_name": (row or {}).get("display_name", "")}
    return await _issue_login(pub, request)             # 当前设备重新拿一对凭证


@app.post("/admin/users/{user_id}/status")
async def admin_set_user_status(user_id: str, body: StatusBody,
                                admin: dict = Depends(require_admin)):
    """启用/禁用账号（管理员）。禁用 = **立刻全端下线**。

    禁用不只是在登录时拦一下：已签发的 access 也要立刻失效（靠 token_version），
    否则"封号"要等最多 15 分钟才生效。
    """
    from src.users import set_status

    if not is_valid_user_id(user_id):
        raise HTTPException(status_code=400, detail="非法 user_id")
    try:
        ok = await set_status(user_id, body.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="用户不存在")

    revoked = 0
    if body.status == "disabled":
        revoked = await _revoke_user_sessions(user_id)
    await _audit(admin["user_id"], f"user_{body.status}", detail=f"target={user_id}")
    return {"ok": True, "user_id": user_id, "status": body.status, "revoked": revoked}


@app.get("/me")
async def me(user: dict = Depends(get_current_user)):
    """返回当前身份 {user_id, role, username, display_name}。无 token → 401。"""
    row = await get_user_by_id(user["user_id"])
    return {
        "user_id": user["user_id"],
        "role": user["role"],
        "username": (row or {}).get("username", ""),
        "display_name": (row or {}).get("display_name") or user["user_id"],
    }


if DEV_MODE:
    class DevLoginBody(BaseModel):
        role: str = "customer"   # customer | admin
        user_id: str = ""        # 可选，默认 dev_customer / dev_admin

    @app.post("/dev/login")
    async def dev_login(body: DevLoginBody, request: Request):
        """开发/演示用 mock 登录：签发任意角色的双凭证（生产由账号登录取代）。"""
        role = body.role if body.role in ("customer", "admin") else "customer"
        uid = (body.user_id or "").strip() or ("dev_admin" if role == "admin" else "dev_customer")
        if not is_valid_user_id(uid):
            raise HTTPException(status_code=400, detail="非法 user_id")
        from src.users import ensure_user_row     # mock 身份也建影子行（外键前提）
        await ensure_user_row(uid, role=role)     # 昵称默认取 uid，不额外造花名
        return await _issue_login({"user_id": uid, "role": role}, request)


# ════════════════════════════════════════════════════════════
# HITL：订单人工审批管理端点（销售经理使用）
# ✅ 鉴权：require_admin —— 无 token 401 / 非 admin 403；审批动作写 audit_log
# ════════════════════════════════════════════════════════════

async def _audit(actor: str, action: str, thread_id: str = "", detail: str = "") -> None:
    """审计留痕：谁、何时、做了什么（鉴权审计 + 合规审计一次做掉）。"""
    try:
        await execute(
            "INSERT INTO audit_log (actor, action, thread_id, detail, created_at) "
            "VALUES (:actor, :action, :thread_id, :detail, :ts)",
            {"actor": actor, "action": action, "thread_id": thread_id,
             "detail": detail, "ts": datetime.now().isoformat()},
        )
    except Exception as e:  # noqa: BLE001 审计失败不影响主流程
        import traceback
        traceback.print_exc()
        print(f"[audit] 写入失败: {e}")


@app.get("/approval/pending")
async def approval_pending(admin: dict = Depends(require_admin)):
    """列出全部待审批订单（仅管理员）。

    2026-09：读 **PG**（`pending_approvals`）而不是进程内存 —— 服务重启/多副本下，
    管理员看到的东西必须一致，且超时单会被惰性标记为 expired 后不再出现。
    """
    return {"pending": await approval.list_pending()}


class ApprovalAction(BaseModel):
    thread_id: str = ""
    approval_id: str = ""     # 优先用 id 精确定位（前端新版本会传）
    reason: str = ""


def _extract_order_no(text: str) -> str:
    """从工具/回复文本里抠订单号。**成功与否只看它**，不看"图有没有抛异常"。"""
    m = _ORDER_NO_RE.search(text or "")
    return m.group(0) if m else ""


_ORDER_NO_RE = __import__("re").compile(r"ORD-\d{8}-\d{13,}")


async def _decide_approval(approval_id: str, thread_id: str, approved: bool,
                           reason: str = "", actor: str = "") -> dict:
    """审批一笔待审批订单：**CAS 抢占 → 优先图 resume → 兜底直接写单**。

    为什么这么绕（每一步都对应一个真实故障）：
    1. **CAS 抢占**：两个管理员同时点"通过"时，只有一个能改到 pending→approved，
       另一个收到"已被处理"——不会重复下单；
    2. **优先 resume**：图状态还在时恢复图执行，客户续轮对话的上下文不丢；
    3. **兜底直接写单**：服务重启过、没有 checkpoint 时，直接用登记在表里的
       `args` 调 create_order —— 订单照生成，**不再因为"图没状态"而丢单**；
    4. **成功标准 = 真拿到订单号**：旧实现把"图没抛异常"当成功，在"注册表在、
       checkpoint 没了"的情况下会返回 ok=true 但订单根本没写（静默假成功），
       所以这里以订单号为准，拿不到就如实报错。
    """
    target = None
    if approval_id:
        target = await approval.get_by_id(approval_id)
        if target and target["status"] != "pending":
            return {"ok": False, "code": "already_decided",
                    "error": f"该订单已被 {target.get('decided_by') or '他人'} 处理"
                             f"（{target['status']}）"}
    if target is None and thread_id:
        target = await approval.get_pending(thread_id)
    if target is None:
        return {"ok": False, "code": "not_found",
                "error": f"没有待审批的订单: approval_id={approval_id!r} thread_id={thread_id!r}"}

    approval_id, thread_id = target["id"], target["thread_id"]
    claimed = await approval.claim(approval_id, actor, approved, reason)
    if not claimed["ok"]:
        msg = {
            "already_decided": "这单刚被其他人处理过了（并发保护：只有一次生效）",
            "expired": "这单已超过审批时限，不能再审批",
            "not_found": "审批单不存在",
        }.get(claimed["code"], "审批失败")
        return {"ok": False, "code": claimed["code"], "error": msg}

    draft = claimed["row"]["draft"]
    session_id = claimed["row"].get("session_id") or "default"
    order_no, ai_text, resumed = "", "", False

    # ① 优先走图 resume（图状态还在 → 客户续轮上下文完整）
    try:
        result = await agent_graph.ainvoke(
            Command(resume={"approved": approved, "reason": reason}),
            config=thread_config(thread_id))
        for m in reversed((result or {}).get("messages") or []):
            if getattr(m, "content", ""):
                ai_text = m.content
                break
        order_no = _extract_order_no(ai_text)
        resumed = bool(order_no)
    except Exception as e:  # noqa: BLE001
        logger.warning("[审批] 图 resume 失败（可能已重启、无 checkpoint）：%s", e)

    # ② 兜底：图没给出订单号 → 直接用登记的参数写单（幂等键 = 审批单 id）
    if approved and not order_no:
        args = dict(claimed["row"].get("args") or {})
        if args:
            # ⚠️ 必须**强制覆盖**，不能 setdefault：LLM 传了 customer_id 时 setdefault 会保留
            # LLM 的值 —— 那意味着"提示词注入 → 订单记在别人名下"，而且延迟到审批之后才发生。
            # 这里一次覆盖两件事：补齐漏传的参数 + 冲掉任何来源不可信的身份值。
            args["customer_id"] = claimed["row"].get("user_id", "") or args.get("customer_id", "")
            args.pop("client_request_id", None)
            try:
                resp = await get_mcp().call_tool(
                    "create_order", {**args, "client_request_id": approval_id})
                order_no = _extract_order_no(str(resp))
                ai_text = ai_text or str(resp)
                logger.warning("[审批] 走兜底直接写单 approval_id=%s → 订单号 %s",
                               approval_id, order_no or "(未拿到)")
            except Exception as e:  # noqa: BLE001
                logger.exception("[审批] 兜底写单失败: %s", e)
        else:
            logger.error("[审批] 无法兜底写单：登记时没存 args（approval_id=%s）", approval_id)

    if approved and not order_no:
        # 如实报错：这一单没成，管理员需要知道（而不是"ok=true 但没订单"）
        await approval.record_order(approval_id, "", resumed=False)
        await _audit(actor, "approve_failed", thread_id,
                     f"approval_id={approval_id} reason={reason}")
        return {"ok": False, "code": "order_not_created",
                "error": "审批已记录，但订单未能生成（图与兜底路径都没拿到订单号），请人工处理",
                "approval_id": approval_id}

    if not ai_text:
        ai_text = (f"✅ 已人工审批通过，订单号：{order_no}" if approved
                   else f"❌ 订单未通过人工审批" + (f"（原因：{reason}）" if reason else ""))
    if not approved:
        ai_text = ai_text or f"❌ 订单未通过人工审批（原因：{reason}）"

    await approval.record_order(approval_id, order_no, resumed=resumed)
    await get_user(thread_id).save_messages([AIMessage(content=ai_text)], session_id)
    await _audit(actor, "approve" if approved else "reject", thread_id,
                 f"approval_id={approval_id} order_no={order_no or '-'} {reason}".strip())

    return {"ok": True, "approved": approved, "reply": ai_text,
            "approval_id": approval_id, "order_no": order_no, "resumed": resumed}


@app.post("/approval/approve")
async def approval_approve(body: ApprovalAction, admin: dict = Depends(require_admin)):
    """审批通过 → 生成订单（仅管理员）。传 approval_id（推荐）或 thread_id。"""
    return await _decide_approval(body.approval_id, body.thread_id, approved=True,
                                  reason=body.reason, actor=admin["user_id"])


@app.post("/approval/reject")
async def approval_reject(body: ApprovalAction, admin: dict = Depends(require_admin)):
    """审批拒绝 → 取消订单（仅管理员）。传 approval_id（推荐）或 thread_id。"""
    return await _decide_approval(body.approval_id, body.thread_id, approved=False,
                                  reason=body.reason, actor=admin["user_id"])


@app.get("/healthz")
async def healthz():
    """存活探测 + 轻量运行状态（Docker 健康检查 / 观测用）。

    ``cache``：redis | local —— Redis 熔断降级时立刻可见（缓存 fail-open，不是故障）。
    ``auth_store``：登录/刷新所依赖的会话存储是否可用（挂掉时登录刷新会 503，
    已签发的 access 仍能用，所以整体仍报 ok）。
    """
    from src.task_queue import get_extraction_queue
    from src.memory import cache_backend
    return {
        "status": "ok",
        "queue": get_extraction_queue().metrics(),
        "cache": cache_backend(),
        "auth_store": await auth_sessions.ping(),
    }


# ── SSE 流式聊天端点（供 React 前端使用）──
@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, user: dict = Depends(get_current_user)):
    user_id = user["user_id"]
    # ⚠️ 限流必须在**建立 SSE 流之前**：一旦返回 StreamingResponse，状态码就已经是 200，
    # 再想表达"你超频了"就只能塞进事件里（前端拿不到 429、也没法读 Retry-After）。
    await rate_limit.enforce("chat_user", user_id, rate_limit.CHAT_USER,
                             "消息发送过于频繁")
    session_id = await _resolve_session(user_id, req.session_id)
    memory = get_user(user_id)

    async def event_gen():
        # 先发一个连接就绪事件，前端据此清空输入、进入等待态
        yield "data: {\"type\": \"start\"}\n\n"
        async for evt in stream_chat(req.message, memory, agent_graph, get_cheap_llm(),
                                     user_id=user_id, session_id=session_id):
            import json as _json
            yield f"data: {_json.dumps(evt, ensure_ascii=False)}\n\n"
    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ════════════════════════════════════════════════════════════
# 管理员数据分析 Agent（2026-09）：自然语言 → 规划 → 只读 SQL → 结论 → 图表
# ── GET  /analytics/examples  快捷问题（前端命令面板用）
# ── POST /analytics/stream    SSE 流式分析（仅管理员 + 限流 + 审计）
# 安全：取数走 src/analytics/sql.py 的四层只读防护；SQL 是 LLM 生成的不可信输入
# ════════════════════════════════════════════════════════════

class AnalyticsBody(BaseModel):
    question: str = ""
    # 快速模式：只跑 1 步（每步 ≈ 2 次 LLM 调用 × 10-30 秒，步数是延迟主因）
    max_steps: int = 0


@app.get("/analytics/examples")
async def analytics_examples(admin: dict = Depends(require_admin)):
    """这个 Agent 能回答什么（前端快捷问题 chips）。"""
    return {"examples": analytics_graph.EXAMPLES}


@app.post("/analytics/stream")
async def analytics_stream(body: AnalyticsBody, admin: dict = Depends(require_admin)):
    """一句话分析：SSE 推送 规划/步骤/SQL/结果/结论/图表。

    限流放在**建立流之前**（同 /chat/stream：一旦开始流式，状态码就锁成 200）。
    """
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    if len(question) > 300:
        raise HTTPException(status_code=400, detail="问题过长（≤300 字）")

    await rate_limit.enforce("analytics_user", admin["user_id"],
                             rate_limit.ANALYTICS_USER, "分析请求过于频繁")

    async def event_gen():
        import json as _json

        summary = {"steps": 0, "sql": 0, "charts": 0, "elapsed_ms": 0, "errors": 0}
        try:
            async for evt in analytics_graph.analyze_stream(
                    question, max_steps=max(0, min(int(body.max_steps or 0), 3))):
                if evt.get("type") == "rows":
                    summary["steps"] += 1
                    if not evt.get("ok"):
                        summary["errors"] += 1
                elif evt.get("type") == "chart":
                    summary["charts"] = len(evt.get("charts") or [])
                elif evt.get("type") == "done":
                    summary["elapsed_ms"] = evt.get("elapsed_ms", 0)
                    summary["sql"] = evt.get("sql_count", 0)
                yield f"data: {_json.dumps(evt, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            logger.exception("[分析] 流式异常: %s", e)
            yield "data: " + _json.dumps({"type": "error", "content": f"分析失败：{str(e)[:150]}"},
                                         ensure_ascii=False) + "\n\n"
        finally:
            # 审计：谁、什么时候、问了什么、跑了几条 SQL、耗时多少
            # （分析会读到客户电话/地址等 PII，访问留痕是合规要求）
            await _audit(admin["user_id"], "analytics_query",
                         detail=f"q={question[:80]} steps={summary['steps']} "
                                f"sql={summary['sql']} charts={summary['charts']} "
                                f"errors={summary['errors']} elapsed={summary['elapsed_ms']}ms")

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


# ════════════════════════════════════════════════════════════
# 多会话（2026-09）：会话列表 / 新建 / 改名 / 删除
# ════════════════════════════════════════════════════════════

class SessionBody(BaseModel):
    title: str = ""


@app.get("/sessions")
async def sessions_list(user: dict = Depends(get_current_user)):
    """当前用户的会话列表（按最近活跃倒序）。"""
    return {"sessions": await list_sessions(user["user_id"])}


@app.post("/sessions")
async def sessions_create(body: SessionBody, user: dict = Depends(get_current_user)):
    """新建会话（title 可留空，首条消息后自动命名）。"""
    return await create_session(user["user_id"], body.title)


@app.patch("/sessions/{sid}")
async def sessions_rename(sid: str, body: SessionBody,
                          user: dict = Depends(get_current_user)):
    if not await rename_session(user["user_id"], sid, body.title):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True}


@app.delete("/sessions/{sid}")
async def sessions_delete(sid: str, user: dict = Depends(get_current_user)):
    if not await delete_session(user["user_id"], sid):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True}


# ════════════════════════════════════════════════════════════
# 历史订单（2026-09）：客户查看自己的订单（行级隔离）
# ════════════════════════════════════════════════════════════

@app.get("/orders")
async def my_orders(user: dict = Depends(get_current_user)):
    """当前用户的历史订单（按时间倒序）。"""
    rows = await query_all(
        "SELECT order_no, product_id, product_name, color, quantity, unit_price, total, "
        "status, phone, address, delivery_date, created_at "
        "FROM orders WHERE customer_id = :uid ORDER BY id DESC LIMIT 100",
        {"uid": user["user_id"]},
    )
    return {"orders": rows}


# ════════════════════════════════════════════════════════════
# 用户管理（2026-09）：管理员查看已注册用户（只读，含状态/角色）
# 绝不返回 password_hash；仅 require_admin
# ════════════════════════════════════════════════════════════

@app.get("/admin/users")
async def admin_users(admin: dict = Depends(require_admin)):
    rows = await query_all(
        "SELECT username, display_name, role, status, created_at "
        "FROM users ORDER BY created_at DESC LIMIT 500",
    )
    return {"users": rows}


# ════════════════════════════════════════════════════════════
# 管理端工作台（2026-09）：全站订单 / 状态流转 / 退款审核
# ── GET  /admin/orders                 全站订单（状态/关键词筛选 + 分页）
# ── GET  /admin/orders/summary         工作台指标（待审批/待发货/退款待审/GMV）
# ── POST /admin/orders/{no}/status      订单状态流转（走状态机，非法转换 400）
# ── GET  /admin/refunds                退款工单（退款 Agent 建的，原先没人能审）
# ── POST /admin/refunds/{id}/decide    退款审核（CAS，防并发重复处理）
# 鉴权：全部 require_admin；所有写操作在 src/admin_orders.py 里写 audit_log
# ════════════════════════════════════════════════════════════

class OrderStatusBody(BaseModel):
    status: str
    note: str = ""


class RefundDecisionBody(BaseModel):
    approve: bool = True
    note: str = ""


def _order_error(e: admin_orders_mod.OrderActionError) -> HTTPException:
    """业务错误 → HTTP：默认 400，目标不存在 404。"""
    return HTTPException(status_code=e.status, detail=e.detail)


@app.get("/admin/orders")
async def admin_orders(status: str = "", keyword: str = "", limit: int = 50,
                       offset: int = 0, admin: dict = Depends(require_admin)):
    """全站订单列表（管理员）。`/orders` 只给当前用户，这里给全部。"""
    try:
        return await admin_orders_mod.list_orders(status=status, keyword=keyword,
                                                  limit=limit, offset=offset)
    except admin_orders_mod.OrderActionError as e:
        raise _order_error(e)


@app.get("/admin/orders/summary")
async def admin_orders_summary(admin: dict = Depends(require_admin)):
    """工作台指标（一次查完：待审批 / 待发货 / 待付款 / 退款待审 / 本月 GMV + 近 7 天趋势）。"""
    return await admin_orders_mod.summary()


@app.post("/admin/orders/{order_no}/status")
async def admin_order_status(order_no: str, body: OrderStatusBody,
                             admin: dict = Depends(require_admin)):
    """订单状态流转（仅管理员）。非法转换会被状态机拒绝（400），不会静默改坏数据。"""
    try:
        return await admin_orders_mod.transition_order(order_no, body.status,
                                                       admin["user_id"], note=body.note)
    except admin_orders_mod.OrderActionError as e:
        raise _order_error(e)


@app.get("/admin/refunds")
async def admin_refunds(status: str = "", limit: int = 50,
                        admin: dict = Depends(require_admin)):
    """退款工单列表（管理员）。status 留空 = 全部，常用 '待审核'。"""
    try:
        return await admin_orders_mod.list_refunds(status=status, limit=limit)
    except admin_orders_mod.OrderActionError as e:
        raise _order_error(e)


@app.post("/admin/refunds/{refund_id}/decide")
async def admin_refund_decide(refund_id: int, body: RefundDecisionBody,
                              admin: dict = Depends(require_admin)):
    """退款审核：通过 / 驳回（仅管理员，且只能审一次）。"""
    try:
        return await admin_orders_mod.decide_refund(refund_id, body.approve,
                                                    admin["user_id"], note=body.note)
    except admin_orders_mod.OrderActionError as e:
        raise _order_error(e)


# ── /api 前缀兼容：web/dist 生产前端请求 /api/xxx（vite 开发代理剥前缀后也是后端无前缀路由）──
# 与上方无前缀路由共享同一组 handler，仅路径不同
# ⚠️ 必须注册在静态 mount 之前（Starlette 按注册顺序匹配）
from fastapi import APIRouter

_api = APIRouter(prefix="/api")
_api.post("/chat")(chat)
_api.get("/history")(get_history)
_api.post("/auth/register")(auth_register)
_api.post("/auth/login")(auth_login)
_api.post("/auth/refresh")(auth_refresh)
_api.post("/auth/logout")(auth_logout)
_api.post("/auth/logout-all")(auth_logout_all)
_api.post("/auth/change-password")(auth_change_password)
_api.post("/admin/users/{user_id}/status")(admin_set_user_status)
_api.get("/auth/sessions")(auth_sessions_list)
_api.delete("/auth/sessions/{sid}")(auth_sessions_revoke)
_api.get("/me")(me)
_api.get("/analytics/examples")(analytics_examples)
_api.post("/analytics/stream")(analytics_stream)
_api.get("/approval/pending")(approval_pending)
_api.post("/approval/approve")(approval_approve)
_api.post("/approval/reject")(approval_reject)
_api.get("/healthz")(healthz)
_api.post("/chat/stream")(chat_stream)
_api.get("/sessions")(sessions_list)
_api.post("/sessions")(sessions_create)
_api.patch("/sessions/{sid}")(sessions_rename)
_api.delete("/sessions/{sid}")(sessions_delete)
_api.get("/orders")(my_orders)
_api.get("/admin/users")(admin_users)
_api.get("/admin/orders")(admin_orders)
_api.get("/admin/orders/summary")(admin_orders_summary)
_api.post("/admin/orders/{order_no}/status")(admin_order_status)
_api.get("/admin/refunds")(admin_refunds)
_api.post("/admin/refunds/{refund_id}/decide")(admin_refund_decide)
if DEV_MODE:
    _api.post("/dev/login")(dev_login)
app.include_router(_api)


# ── 新前端（web/dist 构建产物）：必须最后挂载，避免吞掉 /chat /api 等接口 ──
# 开发模式用 vite dev（cd web && npm run dev → http://localhost:5173，/api 代理到本服务）
_DIST = Path(__file__).parent / "web" / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="web")
else:
    @app.get("/", response_class=HTMLResponse)
    def index_placeholder():
        return "前端未构建：请先 cd web && npm run build（或开发模式 npm run dev → http://localhost:5173）"


if __name__ == "__main__":
    import uvicorn
    print("="*50)
    print("🏭 交易智能体 Web 版")
    print("   打开 http://127.0.0.1:8005")
    print("="*50)
    uvicorn.run(app, host="0.0.0.0", port=8005, log_level="warning")
