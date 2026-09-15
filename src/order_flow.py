"""订单状态机 + 退单联动（领域层，2026-09）
==========================================
为什么单独抽一个模块：订单状态现在有**两条写入路径** —— 管理端操作（`admin_orders.py`）
和售后 Agent 的退款工具（`src/mcp_servers/refund_server.py`）。状态机放在任何一条路径里，
另一条就必然绕过它（"改了一个入口，另一个还在乱写"是这类代码最典型的腐化方式）。

本模块管三件事
==============
1. **状态机**：合法流转写死在 `ALLOWED_TRANSITIONS`，非法流转直接报错；
2. **退单联动**：退款工单与订单状态互相关联（见下），而不是"审完什么都不发生"；
3. **审计**：每次状态变化写 `audit_log`（谁、何时、从什么改到什么）。

退单联动为什么必须做
====================
改之前：`refunds` 由售后 Agent 插入（`status='待审核'`），管理端审核后只改工单自己的
`status` —— **订单状态一个字都不动**。后果实测：

- 19 张"已通过"的退款工单，对应订单还挂在 已发货/已收货，¥197,500 的退款在系统里查不到；
- 分析模块的"净成交额 = GMV − 已退款金额"**无数据可减**（库里根本没有"已退款"状态）。

联动模型（三条规则，够用且可解释）
==================================
1. **工单创建 → 订单进「退款中」**，并把订单**当时的状态**记进工单的
   `order_status_before`。记这个字段是为了驳回时能**原样退回**：
   不记就只能猜（"退回到已付款？"），猜错就是把业务数据改坏。
2. **审核通过 → 订单「已退款」**（终态，落 `orders.refunded_at`）；
   ⚠️ 若退款前订单是**待付款**，则改为**「已取消」** —— 没付过钱的订单没有退款可退，
   叫"已退款"会让财务对不上账。
3. **审核驳回 → 订单退回 `order_status_before`**，前提是**该订单没有其他待审核工单**
   （否则退款流程还在进行，订单必须留在「退款中」）。

「退款中」是一个**闸门状态**：它只能由退款审核离开（通过/驳回），不能再发货、不能再取消 ——
否则会出现"货发了、钱也退了"的双向损失。管理员要手工干预就先审工单（工单是唯一入口）。
"""

from datetime import datetime

from sqlalchemy import text

from src.db import query_all, query_one, transaction
from src.logging_config import get_logger

logger = get_logger(__name__)

# ── 状态定义 ────────────────────────────────────────────────
ORDER_STATUSES = ("待付款", "已付款", "已发货", "已收货", "退款中", "已退款", "已取消")
REFUND_STATUSES = ("待审核", "已通过", "已驳回")

# 可以发起退款的订单状态（已退款/已取消是终态，不能再退）
REFUNDABLE_FROM = ("待付款", "已付款", "已发货", "已收货")

ALLOWED_TRANSITIONS = {
    "待付款": ("已付款", "已取消", "退款中"),
    "已付款": ("已发货", "已取消", "退款中"),
    "已发货": ("已收货", "退款中"),
    "已收货": ("退款中",),
    # 退款中只能由**退款审核**离开：通过→已退款，驳回→restore_after_refund 显式退回。
    # 这里刻意不留"退款中→已发货"之类的边：否则会出现"货已发、款也退"的双向损失。
    "退款中": ("已退款",),
    "已退款": (),          # 终态
    "已取消": (),          # 终态
}

# 某个状态对应要补的时间戳（TEXT ISO，与既有列一致）
STATUS_TIMESTAMP = {"已付款": "paid_at", "已发货": "shipped_at", "已退款": "refunded_at"}

# 订单状态中文 → 退款审核通过后应该落到哪（待付款没有钱可退，走取消）
_APPROVE_TARGET = {"待付款": "已取消"}


class OrderActionError(Exception):
    """状态流转不合法 / 目标不存在。HTTP 层转 400/404。"""

    def __init__(self, detail: str, status: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status = status


async def audit(actor: str, action: str, thread_id: str = "", detail: str = "") -> None:
    """审计留痕（失败不影响主流程，与 app._audit 同语义）。

    取舍：审计写在业务事务**之外**且吞异常。原因是 `audit_log` 刻意没有外键、
    必须能在最糟的情况下写进去；反过来"因为审计写不进去就回滚业务"会让一次
    日志抖动升级成下单失败。代价是极小的概率下"状态改了但没留痕"。
    """
    try:
        await _insert_audit(None, actor, action, thread_id, detail)
    except Exception as e:  # noqa: BLE001
        logger.warning("[订单流] 审计写入失败: %s", e)


async def _insert_audit(conn, actor: str, action: str, thread_id: str, detail: str) -> None:
    """写审计行；传 conn 则复用调用方事务（退款审核这类"要么都成"的操作用）。"""
    sql = ("INSERT INTO audit_log (actor, action, thread_id, detail, created_at) "
           "VALUES (:a, :act, :t, :d, :ts)")
    params = {"a": actor or "unknown", "act": action, "t": thread_id or "",
              "d": (detail or "")[:500], "ts": datetime.now().isoformat()}
    if conn is not None:
        await conn.execute(text(sql), params)
    else:
        from src.db import execute
        await execute(sql, params)


# ── 状态流转 ────────────────────────────────────────────────

async def transition_order(order_no: str, new_status: str, actor: str,
                           note: str = "") -> dict:
    """手工状态流转：校验合法性 → 更新状态（补时间戳）→ 写审计。

    退款相关的两个状态**不从这里进来**（`退款中` 由工单创建触发、`已退款` 由审核触发），
    但保留状态机里的合法性判断，避免有人从管理端把订单直接"改到已退款"却不审工单。
    """
    if new_status not in ORDER_STATUSES:
        raise OrderActionError(f"非法状态：{new_status}")
    order = await query_one("SELECT order_no, status, customer_id FROM orders WHERE order_no = :no",
                            {"no": order_no})
    if not order:
        raise OrderActionError(f"订单不存在：{order_no}", status=404)
    current = order["status"]
    if new_status == current:
        raise OrderActionError(f"订单已经是「{current}」，无需重复操作")
    # ⚠️ 顺序很重要：这条**必须先于**状态机判断。
    # 「退款中 → 已退款」本来就在 ALLOWED_TRANSITIONS 里（退款审核走的就是这条语义），
    # 如果先判状态机，管理端就能用同一个接口手工把订单改成"已退款"而**不审任何工单** ——
    # 账上多一笔退款、系统里没有对应工单，正是这次要消灭的那类不一致。
    if new_status in ("退款中", "已退款"):
        raise OrderActionError(
            f"「{new_status}」只能由退款工单决定：请到「退款审核」通过或驳回对应工单")
    if new_status not in ALLOWED_TRANSITIONS.get(current, ()):
        allowed = "、".join(ALLOWED_TRANSITIONS.get(current, ())) or "（终态，不可再变更）"
        raise OrderActionError(f"不允许从「{current}」变为「{new_status}」；当前可流转到：{allowed}")

    await _write_status(order_no, new_status)
    detail = f"订单 {order_no}：{current} → {new_status}" + (f"（{note}）" if note else "")
    logger.info("[订单流] %s by %s", detail, actor)
    await audit(actor, "order_status_change", order_no, detail)
    return {"ok": True, "order_no": order_no, "from": current, "to": new_status}


async def _write_status(order_no: str, new_status: str, conn=None) -> None:
    """只改状态（+ 对应时间戳，`coalesce` 只补不覆盖）。"""
    ts_col = STATUS_TIMESTAMP.get(new_status)
    if ts_col:
        sql = (f"UPDATE orders SET status = :st, {ts_col} = coalesce({ts_col}, :ts) "
               f"WHERE order_no = :no")
        params = {"st": new_status, "ts": datetime.now().isoformat(), "no": order_no}
    else:
        sql = "UPDATE orders SET status = :st WHERE order_no = :no"
        params = {"st": new_status, "no": order_no}
    if conn is not None:
        await conn.execute(text(sql), params)
    else:
        from src.db import execute
        await execute(sql, params)


# ── 退单联动 ────────────────────────────────────────────────

async def create_refund_ticket(order_no: str, reason: str,
                               actor: str = "refund_agent") -> dict:
    """创建退款工单**并联动订单**（售后 Agent 与人工补单都走这里）。

    返回：{ok, ticket_id, order_status, order_status_before, skipped?}

    - 订单已是「已退款/已取消」→ 不再进退款流程（`ok=False` + 说明），但工单照建：
      客户确实提交过申请，留痕比静默丢弃好；
    - 订单已在「退款中」→ **沿用它原始的 before 状态**，不能把「退款中」记成退回目标
      （否则驳回时订单会卡在退款中，等于永久冻结）；
    - 工单插入与订单状态变更在**同一个事务**里：工单成了但订单没动，是"审完什么都没发生"的
      老问题重演；订单动了但工单没成，则会出现一张永远无法审核的「退款中」订单（死锁）。
    """
    order = await query_one(
        "SELECT order_no, status FROM orders WHERE order_no = :no", {"no": order_no})
    if not order:
        raise OrderActionError(f"订单不存在：{order_no}", status=404)
    status = order["status"]

    if status in ("已退款", "已取消"):
        async with transaction() as conn:
            row = await conn.execute(text(
                "INSERT INTO refunds (order_no, reason, status, created_at, order_status_before) "
                "VALUES (:no, :reason, '待审核', :ts, :before) RETURNING id"),
                {"no": order_no, "reason": reason, "ts": datetime.now().isoformat(),
                 "before": status})
            ticket_id = row.mappings().first()["id"]
        logger.info("[订单流] 订单 %s 已是「%s」，仅登记工单 #%s", order_no, status, ticket_id)
        return {"ok": False, "ticket_id": ticket_id, "order_status": status,
                "order_status_before": status,
                "reason": f"订单已是「{status}」，无需再走退款流程"}

    if status == "退款中":
        prev = await query_one(
            "SELECT order_status_before FROM refunds "
            "WHERE order_no = :no AND status = '待审核' AND order_status_before IS NOT NULL "
            "ORDER BY id LIMIT 1", {"no": order_no})
        before = (prev or {}).get("order_status_before") or status
        moved = False
    else:
        before = status
        moved = True

    async with transaction() as conn:
        row = await conn.execute(text(
            "INSERT INTO refunds (order_no, reason, status, created_at, order_status_before) "
            "VALUES (:no, :reason, '待审核', :ts, :before) RETURNING id"),
            {"no": order_no, "reason": reason, "ts": datetime.now().isoformat(),
             "before": before})
        ticket_id = row.mappings().first()["id"]
        if moved:
            await _write_status(order_no, "退款中", conn=conn)

    if moved:
        detail = f"订单 {order_no}：{before} → 退款中（工单 #{ticket_id}）"
        logger.info("[订单流] %s by %s", detail, actor)
        await audit(actor, "order_status_change", order_no, detail)
    return {"ok": True, "ticket_id": ticket_id, "order_status": "退款中",
            "order_status_before": before, "moved": moved}


async def apply_refund_decision(refund_id: int, approve: bool, actor: str,
                                note: str = "") -> dict:
    """审核退款工单**并联动订单**（CAS：只有仍是"待审核"的工单能被处理）。

    返回：{ok, id, status, order_no, order_status, order_changed, order_note}

    三条规则（与模块 docstring 对应）：
    1. 通过 → 订单「已退款」（退款前是待付款则「已取消」）：没付过钱的不叫退款；
    2. 驳回 → 订单退回 `order_status_before`，但**该订单还有其他待审核工单时不退**
       （退款流程仍在进行，订单必须留在退款中）；
    3. 订单已经是「已退款」→ 幂等跳过（管理员先手工处理过的情况不报错）。
    """
    target = "已通过" if approve else "已驳回"
    row = await query_one(
        "SELECT id, order_no, status, order_status_before FROM refunds WHERE id = :i",
        {"i": refund_id})
    if not row:
        raise OrderActionError(f"退款工单不存在：{refund_id}", status=404)
    if row["status"] != "待审核":
        raise OrderActionError(f"该工单已被处理过（{row['status']}）")

    order_no = row["order_no"]
    before = row["order_status_before"]

    async with transaction() as conn:
        # ① CAS 抢占：并发下只有一个管理员能改到这张工单
        cas = await conn.execute(text(
            "UPDATE refunds SET status = :st, decided_at = :ts, decided_by = :actor, note = :note "
            "WHERE id = :i AND status = '待审核' RETURNING id"),
            {"st": target, "ts": datetime.now().isoformat(), "actor": actor,
             "note": note or "", "i": refund_id})
        if not cas.mappings().first():
            raise OrderActionError("该工单刚被其他人处理过了")

        # ② 顺带把订单状态改掉（同一事务：不会出现"工单审了、订单没动"）
        order = (await conn.execute(
            text("SELECT status FROM orders WHERE order_no = :no FOR UPDATE"),
            {"no": order_no})).mappings().first()
        order_status, changed, order_note = (order or {}).get("status", ""), False, ""
        if order is None:
            order_note = "订单不存在（历史脏数据），只改了工单"
        elif approve:
            want = _APPROVE_TARGET.get(before or "", "已退款")
            if order_status == want:
                order_note = f"订单已是「{want}」，无需再改"
            elif order_status in ("已取消", "已退款"):
                order_note = f"订单已是「{order_status}」（终态），保持不动"
            else:
                await _write_status(order_no, want, conn=conn)
                order_status, changed = want, True
                order_note = f"{before or '（未知）'} → {want}"
        else:
            others = (await conn.execute(text(
                "SELECT count(*) AS n FROM refunds "
                "WHERE order_no = :no AND status = '待审核' AND id <> :i"),
                {"no": order_no, "i": refund_id})).mappings().first()["n"]
            if order_status != "退款中":
                order_note = f"订单不在「退款中」（{order_status}），无需退回"
            elif others:
                order_note = f"该订单还有 {others} 张待审核工单，订单保持「退款中」"
            elif not before:
                # 历史工单没有记录退款前状态（回填脚本之前的脏数据）——猜一个退回目标是改坏数据
                order_note = "工单未记录退款前状态，订单保持「退款中」待人工处理"
            else:
                await _write_status(order_no, before, conn=conn)
                order_status, changed = before, True
                order_note = f"退款中 → {before}（驳回退回）"

        detail = (f"退款工单 #{refund_id}（订单 {order_no}）→ {target}"
                  + (f"（{note}）" if note else "") + f"；订单：{order_note}")
        await _insert_audit(conn, actor, "refund_decision", order_no, detail)

    logger.info("[订单流] 退款工单 #%s → %s by %s；订单 %s", refund_id, target, actor, order_note)
    return {"ok": True, "id": refund_id, "status": target, "order_no": order_no,
            "order_status": order_status, "order_changed": changed, "order_note": order_note}


# 「有未决工单 ⇒ 订单在退款中」是这套模型的核心不变量。
# 写下来是因为**回滚/修数据时最容易把它破坏**：我自己手工回滚 E2E 误操作时，
# 把订单退回原状态却让工单留在"待审核"，正好造出了这次要消灭的那种不一致
# （客户以为在退款、系统里没有任何标记）。终态订单（已取消/已退款）允许例外：工单只是留痕。
_INVARIANT_SQL = """
    SELECT r.id AS ticket_id, r.order_no, o.status AS order_status
    FROM refunds r JOIN orders o ON o.order_no = r.order_no
    WHERE r.status = '待审核' AND o.status NOT IN ('退款中', '已取消', '已退款')
    ORDER BY r.id"""


async def check_refund_invariants() -> list:
    """自检：返回违反"未决工单 ⇒ 订单退款中"的行（空列表 = 一致）。

    退单联动牵扯两张表，任何一次手工改数/回滚都可能把两边改岔。这个函数给
    `scripts/backfill_refund_linkage.py` 和测试共用，让不变量**可被检查**而不是靠记忆。
    """
    return await query_all(_INVARIANT_SQL)


async def refund_context(order_no: str) -> dict:
    """订单的退款上下文（前端展示用：是否有未决工单、退款前状态）。"""
    rows = await query_all(
        "SELECT id, status, reason, created_at, order_status_before FROM refunds "
        "WHERE order_no = :no ORDER BY id DESC", {"no": order_no})
    pending = [r for r in rows if r["status"] == "待审核"]
    return {"tickets": len(rows), "pending": len(pending),
            "pending_ids": [r["id"] for r in pending],
            "order_status_before": next((r["order_status_before"] for r in pending if r["order_status_before"]), "")}
