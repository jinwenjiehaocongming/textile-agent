"""管理端订单/退款接口（2026-09）
==============================
「管理员工作台」需要的**读**能力，之前后端一个都没有：

- **全站订单列表**（原来只有 `/orders` = "我的订单"，管理员看不到别人的单）
- **工作台指标**（待审批 / 待发货 / 退款待审 / 退款中 / 本月 GMV…）
- **退款工单列表**（`refunds` 有 `status=待审核`，但**没有任何接口能审它**）

状态机、退单联动、审计都在 ``src/order_flow.py``（领域层）—— 因为订单状态现在有两条
写入路径（管理端 + 售后 Agent 的退款工具），状态机放在任何一条路径里，另一条就会绕过它。
本模块只负责"读"和把管理端的审核动作转交给领域层。
"""

from typing import Optional

from src.db import query_all, query_one
from src.logging_config import get_logger
from src.order_flow import (                                    # noqa: F401  （对外沿用旧名）
    ALLOWED_TRANSITIONS, ORDER_STATUSES, REFUND_STATUSES, STATUS_TIMESTAMP,
    OrderActionError, apply_refund_decision, audit as _audit, create_refund_ticket,
    transition_order,
)

logger = get_logger(__name__)

# ── 列表与统计 ──────────────────────────────────────────────

async def list_orders(status: str = "", keyword: str = "",
                      limit: int = 50, offset: int = 0) -> dict:
    """全站订单（管理端）：按状态/关键词筛选 + 分页。

    keyword 匹配订单号 / 客户 id / 客户昵称 / 产品编号 / 产品名 —— 管理端真实找单方式：
    客服手里最常只有"订单号后几位""客户编号"或者"产品编号 P0163"，所以产品编号也进搜索。
    """
    where, params = ["1=1"], {}
    if status:
        if status not in ORDER_STATUSES:
            raise OrderActionError(f"非法状态：{status}")
        where.append("o.status = :status")
        params["status"] = status
    if keyword:
        where.append(
            "(o.order_no ILIKE :kw OR o.customer_id ILIKE :kw OR o.product_id ILIKE :kw "
            "OR o.product_name ILIKE :kw OR u.display_name ILIKE :kw)")
        params["kw"] = f"%{keyword}%"
    cond = " AND ".join(where)
    # ⚠️ count 查询也要带上同一个 LEFT JOIN：关键词条件里引用了 u.display_name，
    # 只给列表查询加 join 会让计数查询直接报 "missing FROM-clause entry for table u"
    total = (await query_one(
        f"SELECT count(*) AS n FROM orders o LEFT JOIN users u ON u.id = o.customer_id "
        f"WHERE {cond}", params))["n"]
    rows = await query_all(
        f"""SELECT o.order_no, o.customer_id, u.display_name AS customer_name,
                   o.product_id, o.product_name, o.color, o.quantity,
                   o.unit_price, o.total, o.status, o.created_at, o.paid_at, o.shipped_at,
                   o.refunded_at, o.phone, o.address, o.delivery_date,
                   -- 未决退款工单号：管理端在订单行上能直接看到"这单在退款流程里，工单是哪张"，
                   -- 不用自己去退款页搜订单号（两边对不上时最费时间的就是这一步）
                   (SELECT r.id FROM refunds r
                     WHERE r.order_no = o.order_no AND r.status = '待审核'
                     ORDER BY r.id LIMIT 1) AS pending_refund_id
            FROM orders o LEFT JOIN users u ON u.id = o.customer_id
            WHERE {cond} ORDER BY o.id DESC LIMIT :limit OFFSET :offset""",
        {**params, "limit": max(1, min(limit, 200)), "offset": max(0, offset)})
    return {"orders": rows, "total": total, "limit": limit, "offset": offset}


async def summary() -> dict:
    """工作台指标（一次查完，管理端首页用）。"""
    row = await query_one("""
        SELECT
          (SELECT count(*) FROM pending_approvals WHERE status = 'pending' AND expires_at > now()) AS pending_approvals,
          (SELECT count(*) FROM orders WHERE status = '已付款') AS to_ship,
          (SELECT count(*) FROM orders WHERE status = '待付款') AS unpaid,
          (SELECT count(*) FROM refunds WHERE status = '待审核') AS refunds_to_review,
          -- 退款中 = 已进退款流程、钱还没退出去的敞口（2026-09 退单联动新增状态）
          (SELECT count(*) FROM orders WHERE status = '退款中') AS refunding,
          (SELECT round(coalesce(sum(total), 0), 2) FROM orders
             WHERE status = '退款中') AS refunding_amount,
          (SELECT count(*) FROM orders) AS orders_total,
          (SELECT round(coalesce(sum(total), 0), 2) FROM orders
             WHERE status <> '已取消' AND substr(created_at, 1, 7) = to_char(now(), 'YYYY-MM')) AS gmv_this_month,
          (SELECT count(*) FROM orders
             WHERE substr(created_at, 1, 7) = to_char(now(), 'YYYY-MM')) AS orders_this_month,
          (SELECT count(*) FROM users WHERE status = 'active') AS active_users
    """)
    # 近 7 天订单量（工作台小趋势）
    # ⚠️ 是 `- interval '6 days'`，不是 '7 days'：`>= 今天-7天` 会**多算一天**，
    # 返回 8 个自然日（含今天），前端按 7 根柱子画就会对不上账。
    # created_at 是 TEXT ISO，用 'YYYY-MM-DD' 前缀做字典序比较（同格式下等价于按日比较）。
    trend = await query_all("""
        SELECT substr(created_at, 1, 10) AS day, count(*) AS orders,
               round(coalesce(sum(total), 0), 2) AS gmv
        FROM orders WHERE created_at >= to_char(now() - interval '6 days', 'YYYY-MM-DD')
        GROUP BY 1 ORDER BY 1""")
    return {**row, "trend_7d": trend}


async def list_refunds(status: str = "", limit: int = 50) -> dict:
    """退款工单（管理端）：带订单信息，默认按时间倒序。"""
    where, params = ["1=1"], {}
    if status:
        if status not in REFUND_STATUSES:
            raise OrderActionError(f"非法退款状态：{status}")
        where.append("r.status = :st")
        params["st"] = status
    cond = " AND ".join(where)
    rows = await query_all(
        f"""SELECT r.id, r.order_no, r.reason, r.status, r.created_at,
                   r.decided_at, r.decided_by, r.note, r.order_status_before,
                   o.customer_id, o.product_name, o.quantity, o.total, o.status AS order_status
            FROM refunds r LEFT JOIN orders o ON o.order_no = r.order_no
            WHERE {cond} ORDER BY r.id DESC LIMIT :limit""",
        {**params, "limit": max(1, min(limit, 200))})
    return {"refunds": rows}


# ── 写操作：转交领域层 ───────────────────────────────────────

async def decide_refund(refund_id: int, approve: bool, actor: str, note: str = "") -> dict:
    """退款审核（管理端入口）→ 领域层处理工单 CAS + **订单状态联动**。"""
    return await apply_refund_decision(refund_id, approve, actor, note=note)
