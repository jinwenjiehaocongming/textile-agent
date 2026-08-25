"""
流式对话封装（真 token 流版）
============================
在节点事件流（过程可视化）基础上，增加**真 token 流式**：最终用户可见回复的
LLM token 经 SSE 逐字推给前端，前端实时渲染（不再是整包到达 + 本地打字机）。

事件协议（与前端约定一致）：
    {"type": "start"}                            流连接就绪
    {"type": "node", "node", "label", "detail"}  图节点执行过程（多个）
    {"type": "token", "content": str}            LLM 回复 token 流（多个）
    {"type": "done",  "content": str}            最终完整回复（权威文本）
    {"type": "pending", "content": str, ...}     订单挂起待人工审批
    {"type": "error", "content": str}            异常信息

线程模型：
- 图在后台线程跑（asyncio.to_thread），同步 graph.stream(stream_mode="updates")
- 节点事件与 token 都经同一个 threading.Queue 汇入（emit 线程安全）
- async 侧一个 pump 协程把 threading 队列搬到 asyncio，主协程消费并 yield
- token 推送由 src.token_stream 的 ContextVar 注入到 agent/order/after_sales 的
  LLM 调用里（_safe_llm(stream_tokens=True)）
"""

import asyncio
import queue as _queue
from typing import AsyncIterator

from langchain_core.messages import HumanMessage, AIMessage
from src.render_tools import extract_render_data, extract_data_from_tools


def build_input_state(req_message: str, memory, user_id: str) -> dict:
    """构建与 app.py /chat 一致的状态。"""
    history = memory.load_recent(20)
    prefs = memory.retrieve_preferences()
    user_context = "；".join(prefs) if prefs else ""

    messages = history + [HumanMessage(content=req_message)]
    last_type = memory.get_last_query_type()

    return {
        "messages": messages,
        "knowledge_chunks": [],
        "rewrite_query": "",
        "query_type": last_type,
        "user_id": user_id,
        "user_context": user_context,
    }


def _extract_final_reply(final_state: dict) -> str:
    """从完整最终 state 提取最后一条 AI 消息文本。"""
    msgs = (final_state or {}).get("messages") or []
    for m in reversed(msgs):
        if isinstance(m, AIMessage) and getattr(m, "content", None):
            return m.content
    return ""


def _find_pending_draft(state: dict):
    """从 state 的 __interrupt__ / get_state 的 interrupts 里找订单审批 draft。"""
    from src.approval import find_pending_draft as _f
    return _f((state or {}).get("__interrupt__") or [])


async def stream_chat(
    req_message: str,
    memory,
    graph,
    cheap_llm,
    user_id: str = "123456",
) -> AsyncIterator[dict]:
    """产出事件：start → node*/token* → done / pending / error。"""
    from src.agent import thread_config
    from src.approval import find_pending_draft, pending_reply_text
    from src.node_events import NODE_LABELS, describe_node
    from src.token_stream import set_token_pusher

    state = build_input_state(req_message, memory, user_id)
    config = thread_config(user_id)

    try:
        # ── 挂起态守卫：该用户有订单在待审批 → 不再跑图，直接提示 ──
        snap0 = graph.get_state(config)
        if snap0 and snap0.next:
            draft = find_pending_draft(snap0.interrupts)
            if draft:
                yield {"type": "pending", "content": pending_reply_text(draft)}
                return

        # ── 统一事件管道（线程安全） ──
        src_queue: "_queue.Queue" = _queue.Queue(maxsize=200)
        dst_queue: "asyncio.Queue" = asyncio.Queue(maxsize=200)
        pump_stop = _queue.Queue()  # 用哨兵唤醒 pump，避免驻留线程泄漏

        def emit(kind, payload) -> None:
            src_queue.put((kind, payload))

        async def _pump() -> None:
            # 把线程队列搬到 asyncio 队列（get 阻塞→to_thread 各自起线程等待）
            while True:
                item = await asyncio.to_thread(src_queue.get)
                await dst_queue.put(item)
                if item[0] == "__pump_stop__":
                    return  # 消费方已结束，退出泵

        def _stop_pump() -> None:
            """唤醒并停止 pump（幂等；防止中断/异常路径泄漏驻留线程）。"""
            try:
                src_queue.put_nowait(("__pump_stop__", None))
            except Exception:
                pass
            pump_task.cancel()

        def _run_graph() -> None:
            set_token_pusher(lambda text: emit("token", text))  # 本线程 LLM 流式出口
            try:
                for ev in graph.stream(state, config, stream_mode="updates"):
                    emit("node_event", ev)
            except Exception as e:  # noqa: BLE001 抛给主生成器处理
                emit("__error__", e)
            finally:
                emit("__done__", None)  # 哨兵：流结束（消费循环据此退出）

        pump_task = asyncio.create_task(_pump())
        runner = asyncio.create_task(asyncio.to_thread(_run_graph))

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
                    # HITL：下单挂起（checkpoint 已正确落盘，可被审批 resume）
                    draft = find_pending_draft(ev["__interrupt__"])
                    if draft:
                        interrupted = True
                        reply = pending_reply_text(draft)
                        memory.save_messages(
                            [HumanMessage(content=req_message), AIMessage(content=reply)]
                        )
                        yield {
                            "type": "pending",
                            "content": reply,
                            "data": {"type": "order", "data": draft},
                        }
                    break
                for node, update in ev.items():
                    label = NODE_LABELS.get(node, node)
                    detail = describe_node(node, update)
                    yield {"type": "node", "node": node, "label": label, "detail": detail}

            if not interrupted:
                await runner
        finally:
            _stop_pump()

        # ── 最终态：从 checkpoint 取（正常完成 / 挂起均适用）──
        snap = graph.get_state(config)
        values = ((snap.values or {}) if snap else {})

        if interrupted:
            return

        final_reply = _extract_final_reply(values)
        if not final_reply:
            final_reply = "抱歉，我现在无法处理这个请求，请稍后再试。"

        # 提取结构化展示数据（供前端渲染表格）
        final_msgs = values.get("messages") or []
        render_data = extract_render_data(final_msgs)
        # 兜底：LLM 没调 render 工具时，从 create_order/create_refund 工具结果解析
        if not render_data:
            render_data = extract_data_from_tools(final_msgs)

        evt = {"type": "done", "content": final_reply}
        if render_data:
            evt["data"] = render_data
        yield evt

        # 存档本轮（与 /chat 一致）：human + 最终 AI 回复
        memory.save_messages(
            [HumanMessage(content=req_message), AIMessage(content=final_reply)]
        )
        try:
            # 异步提取偏好：走有界任务队列（不裸开线程）
            from src.task_queue import get_extraction_queue
            get_extraction_queue().submit(
                memory.extract_and_store,
                [HumanMessage(content=req_message), AIMessage(content=final_reply)],
                cheap_llm,
            )
        except Exception:
            pass
    except Exception as e:
        import traceback
        traceback.print_exc()
        yield {"type": "error", "content": f"系统异常: {str(e)[:200]}, 请稍后重试"}