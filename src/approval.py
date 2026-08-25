"""
订单人工审批注册表（HITL）
========================
下单 Agent 在调用 create_order 前通过 LangGraph interrupt() 挂起；
审批人（销售经理）通过管理端点 approve/reject 后，以 Command(resume=...)
恢复图执行，真正的订单写入发生在审批通过之后。

本注册表以 thread_id（= user_id）为键，记录「谁、什么单、何时提交」，
供管理员端点和挂起态判断使用。进程内存储（与 MemorySaver 一致）：
生产环境换 Redis/DB 即可，接口保持不变。
"""

import threading
import time
from typing import Optional


_lock = threading.Lock()
_pending: dict[str, dict] = {}


def register_pending(thread_id: str, user_id: str, draft: dict) -> None:
    """登记一笔待审批订单（interrupt 前调用；重放时幂等覆盖）。"""
    with _lock:
        _pending[thread_id] = {
            "user_id": user_id,
            "draft": draft,
            "created_at": time.time(),
        }


def list_pending() -> list:
    """列出全部待审批订单（管理员端点用），按提交时间升序。"""
    with _lock:
        return [
            {"thread_id": tid, **info}
            for tid, info in sorted(_pending.items(), key=lambda kv: kv[1]["created_at"])
        ]


def get_pending(thread_id: str) -> Optional[dict]:
    with _lock:
        return _pending.get(thread_id)


def remove_pending(thread_id: str) -> None:
    """审批处理完成后移除注册（resume 侧在重放后调用）。"""
    with _lock:
        _pending.pop(thread_id, None)


def find_pending_draft(interrupts) -> Optional[dict]:
    """从 interrupts 列表（Interrupt 对象或 dict）里找订单审批 draft。"""
    for it in interrupts or []:
        val = it.value if hasattr(it, "value") else it
        if isinstance(val, dict) and val.get("type") == "order_approval":
            return val.get("draft") or {}
    return None


def pending_reply_text(draft: dict) -> str:
    """把待审批确认单格式化为客户可见的中间回复（/chat 与 /chat/stream 共用）。"""
    lines = [
        "📋 您的订单已提交人工审批",
        f"产品：{draft.get('product_name', '')} | 货号：{draft.get('product_id', '')} | 颜色：{draft.get('color', '')}",
        f"数量：{draft.get('quantity')}米 | 单价：¥{draft.get('unit_price')}/米 | 总价：¥{draft.get('total')}",
    ]
    if draft.get("phone"):
        lines.append(f"电话：{draft.get('phone')}")
    if draft.get("address"):
        lines.append(f"地址：{draft.get('address')}")
    if draft.get("delivery_date"):
        lines.append(f"交期：{draft.get('delivery_date')}")
    lines.append("⏳ 销售同事将尽快人工确认，审批通过后订单号将自动生成，请稍候。")
    return "\n".join(lines)