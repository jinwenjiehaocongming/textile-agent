#!/usr/bin/env python
"""退款工单 ↔ 订单状态 历史数据回填（2026-09 退单联动）
=======================================================
背景：退单联动上线前，退款工单和订单状态**互不相干**（售后 Agent 只写 refunds，
管理端审核只改工单自己的 status）。所以在老库里会看到这种自相矛盾的状态：

- 19 张 `refunds.status='已通过'`（退款已批准），对应订单却还是 已发货/已收货
  —— 金额 ¥197,500 的退款在系统里查不到；
- 11 张 `待审核` 的工单，订单没进「退款中」—— 谁也不知道这单正在退款流程里。

本脚本按新模型把存量对齐：

    待审核工单 → 订单「退款中」 + 记 `order_status_before`
    已通过工单 → 订单「已退款」（退款前是待付款则「已取消」）+ 记 `refunded_at`
    已驳回工单 → 订单不动，只补记 `order_status_before`（留档，便于以后追溯）

    ⚠️ 会改业务数据。默认只 dry-run 打印，必须显式 --apply。

    python scripts/backfill_refund_linkage.py            # 只报告：哪些单会变成什么
    python scripts/backfill_refund_linkage.py --apply    # 真的改（单事务 + 写审计）

安全性
------
- **单事务**：要么全部对齐，要么一行不改（改一半会留下比现在更难解释的状态）；
- **只碰该碰的行**：订单已是终态（已取消/已退款）或工单挂在不存在订单上的，跳过并报告；
- **每次改动都写审计**（actor=`system_data_fix`，含原因），保证事后能解释"为什么这单状态变了"；
- 回填后再跑一次是**幂等**的（`order_status_before` 已填、订单已在目标状态 → 跳过）。
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://rain@localhost:5432/study1")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import text  # noqa: E402

from src.db import query_all, transaction  # noqa: E402
from src.order_flow import check_refund_invariants  # noqa: E402

ACTOR = "system_data_fix"


async def collect_plan() -> dict:
    """算出"要改什么"，不改任何数据。"""
    tickets = await query_all("""
        SELECT r.id, r.order_no, r.status, r.reason, r.created_at, r.decided_at,
               r.order_status_before, o.status AS order_status, o.total, o.refunded_at
        FROM refunds r LEFT JOIN orders o ON o.order_no = r.order_no
        ORDER BY r.id""")

    plan = {"refunding": [], "refunded": [], "reject_fill": [], "skipped": []}
    for t in tickets:
        if t["order_status"] is None:
            plan["skipped"].append({**t, "why": "订单不存在（脏数据）"})
            continue
        if t["status"] == "待审核":
            if t["order_status"] == "退款中":
                plan["skipped"].append({**t, "why": "订单已在「退款中」（已对齐）"})
            elif t["order_status"] in ("已取消", "已退款"):
                plan["skipped"].append({**t, "why": f"订单已是终态「{t['order_status']}」"})
            else:
                plan["refunding"].append(t)
        elif t["status"] == "已通过":
            if t["order_status"] in ("已取消", "已退款"):
                plan["skipped"].append({**t, "why": f"订单已是终态「{t['order_status']}」（已对齐）"})
            else:
                plan["refunded"].append(t)
        elif t["status"] == "已驳回":
            if t["order_status_before"] is None:
                plan["reject_fill"].append(t)     # 只补字段，订单不动
            else:
                plan["skipped"].append({**t, "why": "已驳回且已记退款前状态"})
        else:
            plan["skipped"].append({**t, "why": f"未知工单状态「{t['status']}」"})
    return plan


def print_plan(plan: dict) -> None:
    def money(rows):
        return sum(float(r["total"] or 0) for r in rows)

    print("=" * 78)
    print("回填计划（dry-run，未改动任何数据）")
    print("=" * 78)
    print(f"\n① 待审核工单 → 订单进「退款中」：{len(plan['refunding'])} 单，金额 ¥{money(plan['refunding']):,.2f}")
    for r in plan["refunding"][:5]:
        print(f"   工单 #{r['id']} {r['order_no']}  {r['order_status']} → 退款中"
              f"（before={r['order_status']}）")
    if len(plan["refunding"]) > 5:
        print(f"   … 其余 {len(plan['refunding']) - 5} 单同理")

    print(f"\n② 已通过工单 → 订单「已退款」（待付款则「已取消」）："
          f"{len(plan['refunded'])} 单，金额 ¥{money(plan['refunded']):,.2f}")
    for r in plan["refunded"][:5]:
        target = "已取消" if r["order_status"] == "待付款" else "已退款"
        print(f"   工单 #{r['id']} {r['order_no']}  {r['order_status']} → {target}")
    if len(plan["refunded"]) > 5:
        print(f"   … 其余 {len(plan['refunded']) - 5} 单同理")

    print(f"\n③ 已驳回工单补记退款前状态（订单不动）：{len(plan['reject_fill'])} 张")
    print(f"\n④ 跳过：{len(plan['skipped'])} 张")
    for r in plan["skipped"][:8]:
        print(f"   工单 #{r['id']} {r['order_no']}：{r['why']}")

    changed_orders = len(plan["refunding"]) + len(plan["refunded"])
    print(f"\n合计将变更 {changed_orders} 张订单状态。确认后执行："
          f"python scripts/backfill_refund_linkage.py --apply")


async def apply_plan(plan: dict) -> None:
    """单事务把计划落地，每笔改动写审计。"""
    now = datetime.now().isoformat()
    async with transaction() as conn:
        for r in plan["refunding"]:
            await conn.execute(
                text("UPDATE refunds SET order_status_before = :b WHERE id = :i AND order_status_before IS NULL"),
                {"b": r["order_status"], "i": r["id"]})
            await conn.execute(
                text("UPDATE orders SET status = '退款中' WHERE order_no = :no AND status NOT IN ('已取消','已退款','退款中')"),
                {"no": r["order_no"]})
            await _audit(conn, r["order_no"], now,
                         f"存量回填：存在待审核退款工单 #{r['id']}，订单 {r['order_status']} → 退款中")

        for r in plan["refunded"]:
            target = "已取消" if r["order_status"] == "待付款" else "已退款"
            await conn.execute(
                text("UPDATE refunds SET order_status_before = :b WHERE id = :i AND order_status_before IS NULL"),
                {"b": r["order_status"], "i": r["id"]})
            await conn.execute(
                text("UPDATE orders SET status = :st, refunded_at = coalesce(refunded_at, :ts) "
                     "WHERE order_no = :no AND status NOT IN ('已取消','已退款')"),
                {"st": target, "ts": r["decided_at"] or r["created_at"] or now, "no": r["order_no"]})
            await _audit(conn, r["order_no"], now,
                         f"存量回填：退款工单 #{r['id']} 已通过，订单 {r['order_status']} → {target}")

        for r in plan["reject_fill"]:
            await conn.execute(
                text("UPDATE refunds SET order_status_before = :b WHERE id = :i AND order_status_before IS NULL"),
                {"b": r["order_status"], "i": r["id"]})

    total = len(plan["refunding"]) + len(plan["refunded"]) + len(plan["reject_fill"])
    print(f"✅ 已回填 {total} 张工单（订单状态变更 "
          f"{len(plan['refunding']) + len(plan['refunded'])} 张），全部写入审计")


async def _audit(conn, order_no: str, ts: str, detail: str) -> None:
    await conn.execute(
        text("INSERT INTO audit_log (actor, action, thread_id, detail, created_at) "
             "VALUES (:a, 'order_status_change', :t, :d, :ts)"),
        {"a": ACTOR, "t": order_no, "d": detail[:500], "ts": ts})


async def main() -> None:
    ap = argparse.ArgumentParser(description="退款工单与订单状态的历史数据回填（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真的执行（单事务，写审计）")
    args = ap.parse_args()

    plan = await collect_plan()
    broken = await check_refund_invariants()
    if broken:
        print(f"⚠️ 发现 {len(broken)} 张工单违反不变量（待审核但订单不在退款中）：")
        for b in broken[:10]:
            print(f"   工单 #{b['ticket_id']} {b['order_no']}：订单状态 {b['order_status']}")
        print("   这些会在下面的回填里一并修正。\n")
    if not any(plan[k] for k in ("refunding", "refunded", "reject_fill")):
        print("没有需要回填的数据（已对齐）。")
        return
    print_plan(plan)
    if args.apply:
        print()
        await apply_plan(plan)


if __name__ == "__main__":
    asyncio.run(main())
