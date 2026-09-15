"""订单人工审批 — PG 持久化状态机（HITL，2026-09 方案 B）
========================================================
下单 Agent 在调用 create_order 前通过 LangGraph ``interrupt()`` 挂起；审批人
approve/reject 后，以 ``Command(resume=...)`` 恢复图执行，真正的订单写入发生在
审批通过之后。

**为什么从进程内存搬到 PG**（真实事故，值得记住）
==============================================
旧实现把"谁在等审批"放在进程内 dict（本模块）+ LangGraph ``MemorySaver``（同为内存），
而"审批通过 → 真正写订单"那行代码**只存在于图被 resume 的路径上**。于是：

- 一次发版/重启 → 客户已经收到"📋 已提交人工审批，请稍候"，管理员列表却空了，
  订单永远不会生成，而且**全链路无痕迹**（dict 消失没有异常、没有日志、没有审计行）；
- 多 worker/多副本 → 客户在 A 挂起，管理员请求打到 B，看不到也批不了；
- 并发审批 → 两人都通过检查，可能生成两笔订单；
- 无过期机制 → 三天前的单今天批了，凭空生成客户早不要的订单。

所以现在**这张表是业务真相来源**：列表、状态流转、超时、审计、幂等全以它为准。
图 checkpoint 只决定"审批后客户续轮对话的上下文还在不在"，**不再决定订单能不能生成**。

关键设计
========
1. **幂等登记**：同一 thread 只允许一条 ``pending``（部分唯一索引 + upsert）；
   resume 重放时会再次执行 ``register_pending``，这里靠"最近刚判定的行"识别重放并复用，
   不会堆出幽灵待审批。
2. **CAS 抢占**：``claim()`` 用 ``UPDATE ... WHERE status='pending'`` 原子抢占，
   并发审批只有一个人成功，另一个拿到明确的"已被处理"而不是重复下单。
3. **超时**：``expires_at``（默认 24h，``APPROVAL_TTL_HOURS``）+ ``expire_overdue()``
   惰性标记 ``expired``；过期单不再出现在待审批列表，也不能被审批。
4. **写单幂等键**：``id`` 同时作为 ``orders.client_request_id`` —— 兜底直接写单即使重试，
   也只会有一笔订单（唯一索引兜底）。
5. **成功标准**：回执里必须真的出现订单号才算成功。旧实现把"图没抛异常"当成功，
   在"注册表还在、checkpoint 没了"的情况下会返回 ``ok=true`` 而订单根本没生成——
   这种静默假成功比报错更危险。
"""

import json
import os
from typing import Optional

from src.db import execute, execute_returning, query_all, query_one
from src.logging_config import get_logger

logger = get_logger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        logger.warning("环境变量 %s 不是整数，回退默认值 %s", name, default)
        return default


APPROVAL_TTL_HOURS = max(1, _env_int("APPROVAL_TTL_HOURS", 24))
# resume 重放识别窗口：管理员点"通过"与图被恢复之间只隔毫秒级，
# 窗口内出现的"刚判定行"一定是重放，而不是客户新下的单。
REPLAY_WINDOW_SECONDS = max(5, _env_int("APPROVAL_REPLAY_WINDOW_SECONDS", 120))


def _row_to_dict(row: dict) -> dict:
    """DB 行 → 对外 dict（时间转 ISO，JSONB 已是 dict）。

    额外给出 ``approval_id``（= DB 的 id）：前端/接口统一用这个名字定位审批单，
    避免"列表里叫 id、SSE 事件里叫 approval_id"两套叫法混用。
    """
    out = dict(row)
    out["approval_id"] = out.get("id", "")
    for key in ("created_at", "expires_at", "decided_at"):
        val = out.get(key)
        if val is not None and hasattr(val, "isoformat"):
            out[key] = val.isoformat()
    for key in ("draft", "args"):
        val = out.get(key)
        if isinstance(val, str):            # 兜底：驱动返回字符串时解析
            try:
                out[key] = json.loads(val)
            except (ValueError, TypeError):
                out[key] = {}
    return out


# ---------------------------------------------------------------------------
# 登记 / 读取
# ---------------------------------------------------------------------------

async def register_pending(thread_id: str, user_id: str, draft: dict,
                           args: Optional[dict] = None,
                           session_id: str = "") -> dict:
    """登记一笔待审批订单（interrupt 前调用），返回该行。

    三种情况都幂等：
    - **resume 重放**（刚被判定的行还在重放窗口内）→ 复用原行，不插新行；
    - 已有 pending → 更新确认单内容（同一张单被重复确认）；
    - 否则插入新行。
    """
    from src.users import ensure_user_row   # 外键前提：pending_approvals.user_id → users.id
    await ensure_user_row(user_id)
    expired = await expire_overdue(thread_id=thread_id)

    recent = await query_one(
        "SELECT * FROM pending_approvals "
        "WHERE thread_id = :tid AND status IN ('approved', 'rejected') "
        "  AND decided_at > now() - make_interval(secs => :win) "
        "ORDER BY decided_at DESC LIMIT 1",
        {"tid": thread_id, "win": REPLAY_WINDOW_SECONDS},
    )
    if recent:
        logger.debug("[审批] 识别为 resume 重放，复用已判定行 %s（不重复登记）",
                     recent["id"])
        return _row_to_dict(recent)

    row = await execute_returning(
        "INSERT INTO pending_approvals (id, thread_id, user_id, session_id, draft, args, expires_at) "
        "VALUES (:id, :tid, :uid, :sid, CAST(:draft AS jsonb), CAST(:args AS jsonb), "
        "        now() + make_interval(hours => :ttl)) "
        "ON CONFLICT (thread_id) WHERE status = 'pending' DO UPDATE "
        "SET draft = EXCLUDED.draft, args = EXCLUDED.args, "
        "    session_id = CASE WHEN EXCLUDED.session_id = '' THEN pending_approvals.session_id "
        "                      ELSE EXCLUDED.session_id END, "
        "    expires_at = EXCLUDED.expires_at "
        "RETURNING *",
        {"id": _new_id(), "tid": thread_id, "uid": user_id, "sid": session_id or "",
         "draft": json.dumps(draft or {}, ensure_ascii=False),
         "args": json.dumps(args or {}, ensure_ascii=False),
         "ttl": APPROVAL_TTL_HOURS},
    )
    if expired:
        logger.info("[审批] 顺手标记 %s 笔超时未审批的订单为 expired", expired)
    return _row_to_dict(row or {})


def _new_id() -> str:
    import uuid
    return uuid.uuid4().hex


async def set_pending_session(thread_id: str, session_id: str) -> None:
    """补记挂起发生在哪个会话（chat/stream 检测到 interrupt 后调用）。"""
    if not session_id:
        return
    await execute(
        "UPDATE pending_approvals SET session_id = :sid "
        "WHERE thread_id = :tid AND status = 'pending'",
        {"sid": session_id, "tid": thread_id},
    )


async def get_pending(thread_id: str) -> Optional[dict]:
    """该线程当前待审批的行（未过期的 pending）。无 → None。"""
    row = await query_one(
        "SELECT * FROM pending_approvals "
        "WHERE thread_id = :tid AND status = 'pending' AND expires_at > now() "
        "ORDER BY created_at DESC LIMIT 1",
        {"tid": thread_id},
    )
    return _row_to_dict(row) if row else None


async def get_by_id(approval_id: str) -> Optional[dict]:
    row = await query_one("SELECT * FROM pending_approvals WHERE id = :id",
                          {"id": approval_id})
    return _row_to_dict(row) if row else None


async def list_pending() -> list:
    """全部待审批（未过期），按提交时间升序。管理员端点用。"""
    await expire_overdue()
    rows = await query_all(
        "SELECT * FROM pending_approvals "
        "WHERE status = 'pending' AND expires_at > now() ORDER BY created_at"
    )
    return [_row_to_dict(r) for r in rows]


async def list_decided(limit: int = 50) -> list:
    """最近已判定/过期的单（审计视图用）。"""
    rows = await query_all(
        "SELECT * FROM pending_approvals WHERE status <> 'pending' "
        "ORDER BY decided_at DESC NULLS LAST LIMIT :n", {"n": limit})
    return [_row_to_dict(r) for r in rows]


async def expire_overdue(thread_id: str = "") -> int:
    """把已过期仍挂着的单标记为 expired（惰性调用，无需定时任务）。"""
    sql = ("UPDATE pending_approvals SET status = 'expired', decided_by = 'system', "
           "       decided_at = now(), reason = '超过审批时限自动过期' "
           "WHERE status = 'pending' AND expires_at <= now()")
    params: dict = {}
    if thread_id:
        sql += " AND thread_id = :tid"
        params["tid"] = thread_id
    sql += " RETURNING id"
    rows = await query_all(sql, params)
    return len(rows or [])


# ---------------------------------------------------------------------------
# 判定（CAS 抢占）与收尾
# ---------------------------------------------------------------------------

async def claim(approval_id: str, actor: str, approved: bool, reason: str = "") -> dict:
    """原子抢占并写入判定结果。

    返回 ``{"ok": bool, "code": str, "row": dict|None}``；
    ``code`` ∈ claimed | already_decided | expired | not_found。

    用单条 ``UPDATE ... WHERE status='pending'`` 做 CAS：两个管理员同时点"通过"，
    只有一个能拿到行，另一个拿到 already_decided —— 从根上避免重复下单。
    """
    status = "approved" if approved else "rejected"
    row = await execute_returning(
        "UPDATE pending_approvals SET status = :st, decided_at = now(), "
        "       decided_by = :actor, reason = :reason "
        "WHERE id = :id AND status = 'pending' AND expires_at > now() "
        "RETURNING *",
        {"st": status, "actor": actor or "", "reason": reason or "", "id": approval_id},
    )
    if row:
        return {"ok": True, "code": "claimed", "row": _row_to_dict(row)}

    existing = await query_one("SELECT status, expires_at, decided_by FROM pending_approvals "
                               "WHERE id = :id", {"id": approval_id})
    if not existing:
        return {"ok": False, "code": "not_found", "row": None}
    if existing["status"] == "pending":          # 只能是被 expires_at 挡住
        await expire_overdue()
        return {"ok": False, "code": "expired", "row": None}
    return {"ok": False, "code": "already_decided", "row": None}


async def record_order(approval_id: str, order_no: str, resumed: bool = False) -> None:
    """记账：这一单的最终订单号，以及是走图 resume 还是兜底直接写单。"""
    await execute(
        "UPDATE pending_approvals SET order_no = :no, resumed = :res WHERE id = :id",
        {"no": order_no or "", "res": bool(resumed), "id": approval_id},
    )


async def remove_pending(approval_id: str) -> None:
    """删除**仍处于 pending** 的行（resume 重放时产生的幽灵行用 id 精确清理）。

    只删这一条 id，不碰同线程别的行 —— 避免"清理顺手删掉客户刚提交的新单"。
    已判定的行保留下来作为审计记录。
    """
    if not approval_id:
        return
    await execute("DELETE FROM pending_approvals WHERE id = :id AND status = 'pending'",
                  {"id": approval_id})


# ---------------------------------------------------------------------------
# 纯函数（与 interrupt payload 交互，不碰 DB）
# ---------------------------------------------------------------------------

def find_pending_draft(interrupts) -> Optional[dict]:
    """从 interrupts 列表（Interrupt 对象或 dict）里找订单审批 draft。"""
    for it in interrupts or []:
        val = it.value if hasattr(it, "value") else it
        if isinstance(val, dict) and val.get("type") == "order_approval":
            return val.get("draft") or {}
    return None


def find_approval_id(interrupts) -> str:
    """从 interrupts 里取出 approval_id（前端/管理员审批时用它精确定位）。"""
    for it in interrupts or []:
        val = it.value if hasattr(it, "value") else it
        if isinstance(val, dict) and val.get("type") == "order_approval":
            return val.get("approval_id") or ""
    return ""


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
