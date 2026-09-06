"""
纺织客服 Web UI
===============
FastAPI + 原生 HTML，模仿企业微信界面

运行: python app.py → http://127.0.0.1:8000
"""

from contextlib import asynccontextmanager
import os
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.types import Command
from src.agent import build_graph, get_cheap_llm, thread_config
from src.approval import (
    find_pending_draft, get_pending, list_pending, pending_reply_text,
    remove_pending, set_pending_session,
)
from src.auth import create_token, require_admin, get_current_user, AuthError
from src.db import execute, query_all
from src.memory import get_user
from src.mcp_client import init_mcp
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
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🔌 连接 MCP 工具服务器...")
    await init_mcp(SERVERS)
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
    try:
        user_id = user["user_id"]
        session_id = await _resolve_session(user_id, req.session_id)
        memory = get_user(user_id)
        config = thread_config(user_id)

        # 挂起态守卫：该用户有订单在待审批 → 不跑图，直接提示
        snap = await agent_graph.aget_state(config)
        draft = find_pending_draft(getattr(snap, "interrupts", None)) if snap else None
        if draft:
            return {"reply": pending_reply_text(draft), "pending": True, "draft": draft}

        # 加载历史 + 偏好（异步）
        history = await memory.load_recent(20, session_id)
        prefs = await memory.retrieve_preferences()
        user_context = "；".join(prefs) if prefs else ""

        messages = history + [HumanMessage(content=req.message)]
        last_type = await memory.get_last_query_type()

        state = {"messages": messages, "knowledge_chunks": [], "rewrite_query": "",
                 "query_type": last_type, "user_id": user_id, "user_context": user_context}
        result = await agent_graph.ainvoke(state, config=config)

        # ── HITL：下单挂起，等人工审批 ──
        draft = find_pending_draft(result.get("__interrupt__"))
        if draft:
            reply = pending_reply_text(draft)
            await memory.save_last_query_type("chat")
            await memory.save_messages(
                [HumanMessage(content=req.message), AIMessage(content=reply)], session_id)
            set_pending_session(user_id, session_id)
            await touch_session(user_id, session_id, req.message)
            return {"reply": reply, "pending": True, "draft": draft}

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
# 账号：注册 / 登录（2026-09，取代 /dev/login 成为正式身份源）
# ── POST /auth/register  开放注册（仅 customer）→ 自动登录签发 token
# ── POST /auth/login     用户名 + 密码 → {token, user_id, role, display_name}
# ── GET  /me             登录态探测（无 token → 401，前端据此跳登录页）
# ── POST /dev/login      仅 DEV_MODE=1 注册（本地演示便利，生产消失）
# ════════════════════════════════════════════════════════════

class RegisterBody(BaseModel):
    username: str
    password: str
    display_name: str = ""


class LoginBody(BaseModel):
    username: str
    password: str


def _issue_token(user_pub: dict) -> dict:
    """签发 token 并拼出登录响应（auth.py「换证」思想：前端只认 {token, role}）。"""
    token = create_token(user_pub["user_id"], role=user_pub["role"])
    return {**user_pub, "token": token}


@app.post("/auth/register")
async def auth_register(body: RegisterBody):
    """开放注册：角色强制 customer（admin 只能由 scripts/create_admin.py 创建）。"""
    try:
        pub = await create_user(body.username, body.password, body.display_name)
    except UsernameTaken as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(pub["user_id"], "register", detail=f"username={pub['username']}")
    return _issue_token(pub)


@app.post("/auth/login")
async def auth_login(body: LoginBody):
    """登录：校验通过签发 JWT；失败统一 401（不泄露用户名是否存在）。"""
    pub = await auth_user(body.username, body.password)
    if not pub:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return _issue_token(pub)


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
    async def dev_login(body: DevLoginBody):
        """开发/演示用 mock 登录：签发任意角色的 JWT（演示/测试便利，生产由账号登录取代）。"""
        role = body.role if body.role in ("customer", "admin") else "customer"
        uid = (body.user_id or "").strip() or ("dev_admin" if role == "admin" else "dev_customer")
        if not is_valid_user_id(uid):
            raise HTTPException(status_code=400, detail="非法 user_id")
        token = create_token(uid, role=role)
        return {"token": token, "user_id": uid, "role": role}


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
def approval_pending(admin: dict = Depends(require_admin)):
    """列出全部待审批订单（仅管理员）。"""
    return {"pending": list_pending()}


class ApprovalAction(BaseModel):
    thread_id: str
    reason: str = ""


async def _resume_approval(thread_id: str, approved: bool, reason: str = "", actor: str = "") -> dict:
    """恢复挂起的下单图（异步）：审批通过 → create_order 写库；拒绝 → 取消。"""
    if not thread_id or not get_pending(thread_id):
        return {"ok": False, "error": f"没有待审批的订单: {thread_id!r}"}

    config = thread_config(thread_id)
    result = await agent_graph.ainvoke(
        Command(resume={"approved": approved, "reason": reason}), config=config
    )
    remove_pending(thread_id)

    final_msgs = (result or {}).get("messages") or []
    ai_text = ""
    for m in reversed(final_msgs):
        if getattr(m, "content", ""):
            ai_text = m.content
            break
    if ai_text:
        info = get_pending(thread_id) or {}
        sid = info.get("session_id") or "default"
        await get_user(thread_id).save_messages([AIMessage(content=ai_text)], sid)

    # 审计：谁批的、批了什么、理由
    await _audit(actor, "approve" if approved else "reject", thread_id, reason)

    return {"ok": True, "approved": approved, "reply": ai_text}


@app.post("/approval/approve")
async def approval_approve(body: ApprovalAction, admin: dict = Depends(require_admin)):
    """审批通过 → 生成订单（仅管理员）。"""
    return await _resume_approval(body.thread_id, approved=True,
                                  reason=body.reason, actor=admin["user_id"])


@app.post("/approval/reject")
async def approval_reject(body: ApprovalAction, admin: dict = Depends(require_admin)):
    """审批拒绝 → 取消订单（仅管理员）。"""
    return await _resume_approval(body.thread_id, approved=False,
                                  reason=body.reason, actor=admin["user_id"])


@app.get("/healthz")
def healthz():
    """存活探测 + 轻量运行状态（Docker 健康检查 / 观测用）。"""
    from src.task_queue import get_extraction_queue
    return {
        "status": "ok",
        "queue": get_extraction_queue().metrics(),
    }


# ── SSE 流式聊天端点（供 React 前端使用）──
@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, user: dict = Depends(get_current_user)):
    user_id = user["user_id"]
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


# ── /api 前缀兼容：web/dist 生产前端请求 /api/xxx（vite 开发代理剥前缀后也是后端无前缀路由）──
# 与上方无前缀路由共享同一组 handler，仅路径不同
# ⚠️ 必须注册在静态 mount 之前（Starlette 按注册顺序匹配）
from fastapi import APIRouter

_api = APIRouter(prefix="/api")
_api.post("/chat")(chat)
_api.get("/history")(get_history)
_api.post("/auth/register")(auth_register)
_api.post("/auth/login")(auth_login)
_api.get("/me")(me)
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
