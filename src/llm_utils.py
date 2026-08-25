"""
LLM 调用通用工具：统一超时、重试、降级兜底 + 可选真流式。
==================================================
所有 Agent 的 LLM 调用都应走 _safe_llm，避免网关抽风时无限挂起。

stream_tokens=True 时（仅限最终用户可见回复的调用点）：
- 内部改用 llm.stream()，逐 chunk 通过 src.token_stream 的 pusher 推给 SSE
- 用 AIMessageChunk 逐段累加（content 与 tool_calls 都会正确合并），
  工作方式与 invoke() 一致；工具调用轮的 chunk 内容为空，不会推到前端
"""

import time as _time
from src.logging_config import get_logger

logger = get_logger(__name__)


def _safe_llm(llm_inst, messages, fallback=None, stream_tokens: bool = False):
    """
    统一 LLM 调用入口：重试 2 次（1s/2s）→ 降级 → 保底。

    Args:
        llm_inst:      ChatOpenAI 实例（建议带 timeout + max_retries）
        messages:      message 列表
        fallback:      最终兜底返回（BaseMessage）；None 则重试耗尽后抛异常
        stream_tokens: 开启真流式（需 src.token_stream 已设置 pusher），
                       把最终回复的 token 逐字推给 SSE

    Returns:
        LLM 响应（BaseMessage / AIMessageChunk）；若全失败且有 fallback，返回 fallback。
    """
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


def _invoke_streaming(llm_inst, messages):
    """
    真流式调用：llm.stream() 逐 chunk 推 token，返回累加后的 AIMessageChunk。
    流式中途异常 → 回落到 invoke()（保证结果正确性，代价是这批 token 不流了）。
    """
    from src.token_stream import get_token_pusher

    pusher = get_token_pusher()
    if pusher is None:
        # 没有推送通道（如评测/CLI 环境）→ 退化为普通 invoke，行为不变
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

    if acc is None:  # 空流（理论不发生）
        return llm_inst.invoke(messages)
    return acc