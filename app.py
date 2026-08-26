"""
纺织客服 Web UI
===============
FastAPI + 原生 HTML，模仿企业微信界面

运行: python app.py → http://127.0.0.1:8000
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.types import Command
from src.agent import build_graph, cheap_llm, thread_config
from src.approval import (
    find_pending_draft, get_pending, list_pending, pending_reply_text, remove_pending,
)
from src.memory import get_user
from src.mcp_client import init_mcp
from src.stream_chat import stream_chat
from src.user_identity import resolve_user_id

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


app = FastAPI(title="宏润纺织 AI 客服", lifespan=lifespan)
agent_graph = build_graph()

# ── CORS：允许独立前端 (Vite dev server) 跨域访问 ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应收紧为具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 用户身份：从请求头 X-User-Id 注入（企业微信场景对应 external_userid）──
# 解析/校验规则见 src.user_identity（与 src.memory 共用同一份约束）：
#   - 缺省 → 降级到 guest（开发期便利，多用户场景必传）
#   - 显式传入但非法 → 400 拒绝（防目录穿越 / 注入）
def _user_id_from_header(request: Request) -> str:
    try:
        return resolve_user_id(request.headers.get("X-User-Id"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class ChatRequest(BaseModel):
    message: str


@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    import asyncio
    try:
        # 每个请求独立取用户记忆，互不串数据
        user_id = _user_id_from_header(request)
        memory = get_user(user_id)
        config = thread_config(user_id)

        # 挂起态守卫：该用户有订单在待审批 → 不跑图，直接提示
        snap = await agent_graph.aget_state(config)
        draft = find_pending_draft(getattr(snap, "interrupts", None)) if snap else None
        if draft:
            return {"reply": pending_reply_text(draft), "pending": True, "draft": draft}

        # 加载历史 + 偏好（异步）
        history = await memory.load_recent(20)
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
            await memory.save_messages([HumanMessage(content=req.message), AIMessage(content=reply)])
            return {"reply": reply, "pending": True, "draft": draft}

        # 保存本轮状态供下一轮延续
        await memory.save_last_query_type(result.get("query_type", "chat"))

        # 存档 + 异步提取偏好（后台协程）
        await memory.save_messages([HumanMessage(content=req.message), result["messages"][-1]])
        asyncio.create_task(memory.extract_and_store(result["messages"], cheap_llm))

        return {"reply": result["messages"][-1].content}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"reply": f"系统异常: {str(e)[:200]}, 请稍后重试"}


@app.get("/history")
async def get_history(request: Request):
    user_id = _user_id_from_header(request)
    memory = get_user(user_id)
    rows = await memory.load_recent(30)
    return [{"role": m.type, "content": m.content} for m in rows]


# ════════════════════════════════════════════════════════════
# HITL：订单人工审批管理端点（销售经理使用）
# ⚠️ P3 前未加鉴权，生产必须接管理员登录/角色校验
# ════════════════════════════════════════════════════════════

@app.get("/approval/pending")
def approval_pending():
    """列出全部待审批订单。"""
    return {"pending": list_pending()}


class ApprovalAction(BaseModel):
    thread_id: str
    reason: str = ""


async def _resume_approval(thread_id: str, approved: bool, reason: str = "") -> dict:
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
        await get_user(thread_id).save_messages([AIMessage(content=ai_text)])

    return {"ok": True, "approved": approved, "reply": ai_text}


@app.post("/approval/approve")
async def approval_approve(body: ApprovalAction):
    """审批通过 → 生成订单。"""
    return await _resume_approval(body.thread_id, approved=True, reason=body.reason)


@app.post("/approval/reject")
async def approval_reject(body: ApprovalAction):
    """审批拒绝 → 取消订单。"""
    return await _resume_approval(body.thread_id, approved=False, reason=body.reason)


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
async def chat_stream(req: ChatRequest, request: Request):
    user_id = _user_id_from_header(request)
    memory = get_user(user_id)

    async def event_gen():
        # 先发一个连接就绪事件，前端据此清空输入、进入等待态
        yield "data: {\"type\": \"start\"}\n\n"
        async for evt in stream_chat(req.message, memory, agent_graph, cheap_llm, user_id=user_id):
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


# ── 新前端（web/dist 构建产物）：必须在所有 API 路由之后挂载，避免吞掉 /chat 等接口 ──
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
    print("🏭 宏润纺织 AI 客服 Web 版")
    print("   打开 http://127.0.0.1:8005")
    print("="*50)
    uvicorn.run(app, host="0.0.0.0", port=8005, log_level="warning")
