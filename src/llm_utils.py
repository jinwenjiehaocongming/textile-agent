"""
LLM 调用通用工具：统一超时、重试、降级兜底 + 可选真流式（同步/异步双版本）
====================================================================
所有 Agent 的 LLM 调用都应走 _safe_llm（同步）或 _safe_llm_async（异步），
避免网关抽风时无限挂起。

stream_tokens=True 时（仅限最终用户可见回复的调用点）：
- 内部改用 llm.stream()/astream()，逐 chunk 通过 src.token_stream 的 pusher 推给 SSE
- 用 AIMessageChunk 逐段累加（content 与 tool_calls 都会正确合并），
  工作方式与 invoke() 一致；工具调用轮的 chunk 内容为空，不会推到前端
"""

import asyncio
import time as _time

from src.logging_config import get_logger

logger = get_logger(__name__)


def _safe_llm(llm_inst, messages, fallback=None, stream_tokens: bool = False):
    """同步版：重试 2 次（1s/2s）→ 降级 → 保底。"""
    for i in range(3):
        try:
            if stream_tokens:
                return _invoke_streaming(llm_inst, messages)
            return llm_inst.invoke(messages)
        except Exception as e:
            if i < 2:
                logger.warning(f"LLM 调用失败，{i+1}s 后重试: {str(e)[:80]}")
                _time.sleep(i + 1)
            else:
                if fallback is not None:
                    logger.warning(f"LLM 降级: {str(e)[:60]}")
                    return fallback
                raise


async def _safe_llm_async(llm_inst, messages, fallback=None, stream_tokens: bool = False):
    """异步版：await ainvoke/astream，重试用 asyncio.sleep（不阻塞事件循环）。"""
    for i in range(3):
        try:
            if stream_tokens:
                return await _invoke_streaming_async(llm_inst, messages)
            return await llm_inst.ainvoke(messages)
        except Exception as e:
            if i < 2:
                logger.warning(f"LLM 异步调用失败，{i+1}s 后重试: {str(e)[:80]}")
                await asyncio.sleep(i + 1)
            else:
                if fallback is not None:
                    logger.warning(f"LLM 降级: {str(e)[:60]}")
                    return fallback
                raise


def _invoke_streaming(llm_inst, messages):
    """同步流式：llm.stream() 逐 chunk 推 token，返回累加后的 AIMessageChunk。"""
    from src.token_stream import get_token_pusher

    pusher = get_token_pusher()
    if pusher is None:
        return llm_inst.invoke(messages)

    acc = None
    try:
        for chunk in llm_inst.stream(messages):
            content = chunk.content
            if content:
                pusher(content)
            acc = chunk if acc is None else (acc + chunk)
    except Exception as e:
        logger.warning(f"LLM 流式中断，回落 invoke: {str(e)[:80]}")
        return llm_inst.invoke(messages)

    return acc if acc is not None else llm_inst.invoke(messages)


async def _invoke_streaming_async(llm_inst, messages):
    """异步流式：llm.astream() 逐 chunk 推 token，返回累加后的 AIMessageChunk。"""
    from src.token_stream import get_token_pusher

    pusher = get_token_pusher()
    if pusher is None:
        return await llm_inst.ainvoke(messages)

    acc = None
    try:
        async for chunk in llm_inst.astream(messages):
            content = chunk.content
            if content:
                pusher(content)
            acc = chunk if acc is None else (acc + chunk)
    except Exception as e:
        logger.warning(f"LLM 异步流式中断，回落 ainvoke: {str(e)[:80]}")
        return await llm_inst.ainvoke(messages)

    return acc if acc is not None else await llm_inst.ainvoke(messages)