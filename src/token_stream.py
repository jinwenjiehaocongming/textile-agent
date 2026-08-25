"""
真 token 流式通道
=================
把「最终用户可见回复」的 LLM token 逐字推给 SSE 层，实现真正的流式打字效果
（区别于旧的"整包到达 + 前端本地打字机伪装"）。

实现：ContextVar 在当前会话线程内传递一个 push 回调。
- stream_chat 在跑图线程里 set_token_pusher(emit)
- agent_node / order_agent / after_sales 的最终回答 LLM 调用走
  _safe_llm(..., stream_tokens=True)，内部用 llm.stream() 逐 chunk 回调该 pusher
- 工具调用轮的 chunk 内容为空，天然不推任何字，只有最终文字生成才可见

线程安全：pusher 由 stream_chat 提供（threading.Queue.put，安全）。
"""

from contextvars import ContextVar
from typing import Callable, Optional

# 当前会话的 token 推送回调（None = 未开启流式）
_token_pusher: "ContextVar[Optional[Callable[[str], None]]]" = ContextVar(
    "token_pusher", default=None
)


def set_token_pusher(fn: Optional[Callable[[str], None]]) -> None:
    """在跑图线程内开启/关闭 token 推送（ContextVar 会随线程内调用链传播）。"""
    _token_pusher.set(fn)


def get_token_pusher() -> Optional[Callable[[str], None]]:
    return _token_pusher.get()