"""
流式对话封装（真 token 流 · 全异步版）
===================================
在节点事件流（过程可视化）基础上，增加**真 token 流式**：最终用户可见回复的
LLM token 经 SSE 逐字推给前端，前端实时渲染。

事件协议（与前端约定一致）：
    {"type": "start"}                            流连接就绪
    {"type": "node", "node", "label", "detail"}  图节点执行过程（多个）
    {"type": "token", "content": str}            LLM 回复 token 流（多个）
    {"type": "done",  "content": str}            最终完整回复（权威文本）
    {"type": "pending", "content": str, ...}     订单挂起待人工审批
    {"type": "error", "content": str}            异常信息

企业级演进（2026-08）：
- 图已全面 async（astream），原"后台线程 + 线程队列桥"整体移除，
  事件直接经 asyncio.Queue 汇聚——全链路单事件循环，无线程切换开销。
"""

import asyncio
from typing import AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage
from src.render_tools import extract_data_from_tools, extract_render_data


async def build_input_state(req_message: str, memory, user_id: str) -> dict:
    """构建与 app.py /chat 一致的状态（异步）。"""
    history = await memory.load_recent(20)
    prefs = await memory.retrieve_preferences()
    user_context = "；".join(prefs) if prefs else ""

    messages = history + [HumanMessage(content=req_message)]
    last_type = await memory.get_last_query_type()

    return {
        "messages": messages,
        "knowledge_chunks": [],
        "rewrite_query": "",
        "query_type": last_type,
        "user_id": user_id,
        "user_context": user_context,
    }


def _extract_final_reply(final_state: dict) -> str:
    msgs = (final_state or {}).get("messages") or []
    for m in reversed(msgs):
        if isinstance(m, AIMessage) and getattr(m, "content", None):
            return m.content
    return ""


def _find_pending_draft(state: dict):
    from src.approval import find_pending_draft as _f
    return _f((state or {}).get("__interrupt__") or [])


async def stream_chat(
    req_message: str,
    memory,
    graph,
    cheap_llm,
    user_id: str = "123456",
) -> AsyncIterator[dict]:
    """产出事件：start → node*/token* → done / pending / error（全异步）。"""
    from src.agent import thread_config
    from src.approval import find_pending_draft, pending_reply_text
    from src.node_events import NODE_LABELS, describe_node
    from src.token_stream import set_token_pusher

    state = await build_input_state(req_message, memory, user_id)
    config = thread_config(user_id)

    try:
        # ── 挂起态守卫：该用户有订单在待审批 → 不再跑图，直接提示 ──
        snap0 = await graph.aget_state(config)
        if snap0 and snap0.next:
            draft = find_pending_draft(snap0.interrupts)
            if draft:
                yield {"type": "pending", "content": pending_reply_text(draft)}
                return

        # ── 统一事件管道（asyncio.Queue：async 节点直接投递）──
        dst_queue: "asyncio.Queue" = asyncio.Queue(maxsize=500)

        def emit(kind, payload) -> None:
            # 语义化：LLM 流式 token 由节点内同步回调触发（协程上下文内），put_nowait 安全
            dst_queue.put_nowait((kind, payload))

        async def _run_graph() -> None:
            set_token_pusher(lambda text: emit("token", text))
            try:
                async for ev in graph.astream(state, config, stream_mode="updates"):
                    await dst_queue.put(("node_event", ev))
            except Exception as e:  # noqa: BLE001 抛给主生成器处理
                await dst_queue.put(("__error__", e))
            finally:
                await dst_queue.put(("__done__", None))  # 流结束哨兵

        runner = asyncio.create_task(_run_graph())

        try:
            interrupted = False
            while True:
                kind, payload = await dst_queue.get()
                if kind == "__error__":
                    raise payload
                if kind == "__done__":
                    break
                if kind == "token":
                    yield {"type": "token", "content": payload}
                    continue
                if kind != "node_event":
                    continue

                ev = payload or {}
                if "__interrupt__" in ev:
                    # HITL：下单挂起（checkpoint 已落盘，可被审批 resume）
                    draft = find_pending_draft(ev["__interrupt__"])
                    if draft:
                        interrupted = True
                        reply = pending_reply_text(draft)
                        await memory.save_messages(
                            [HumanMessage(content=req_message), AIMessage(content=reply)]
                        )
                        yield {
                            "type": "pending",
                            "content": reply,
                            "data": {"type": "order", "data": draft},
                        }
                    break
                for node, update in (ev or {}).items():
                    label = NODE_LABELS.get(node, node)
                    detail = describe_node(node, update)
                    yield {"type": "node", "node": node, "label": label, "detail": detail}

            if not interrupted:
                await runner
        finally:
            if not runner.done():
                runner.cancel()

        # ── 最终态：从 checkpoint 取（正常完成 / 挂起均适用）──
        snap = await graph.aget_state(config)
        values = ((snap.values or {}) if snap else {})

        if interrupted:
            return

        final_reply = _extract_final_reply(values)
        if not final_reply:
            final_reply = "抱歉，我现在无法处理这个请求，请稍后再试。"

        # 提取结构化展示数据（供前端渲染表格）
        final_msgs = values.get("messages") or []
        render_data = extract_render_data(final_msgs)
        if not render_data:
            render_data = extract_data_from_tools(final_msgs)

        evt = {"type": "done", "content": final_reply}
        if render_data:
            evt["data"] = render_data
        yield evt

        # 存档本轮（与 /chat 一致）：human + 最终 AI 回复
        await memory.save_messages(
            [HumanMessage(content=req_message), AIMessage(content=final_reply)]
        )
        try:
            # 异步提取偏好：后台协程（不阻塞当前回复）
            asyncio.create_task(memory.extract_and_store(
                [HumanMessage(content=req_message), AIMessage(content=final_reply)],
                cheap_llm,
            ))
        except Exception:
            pass
    except Exception as e:
        import traceback
        traceback.print_exc()
        yield {"type": "error", "content": f"系统异常: {str(e)[:200]}, 请稍后重试"}