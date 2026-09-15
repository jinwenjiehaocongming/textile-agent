#!/usr/bin/env python
"""脏数据修复：orders 表 3 行"列错位"数据（2026-09）
=================================================
现象
----
`data/orders.db`（SQLite 时代的种子数据）里有 3 行订单，它们的值整体错位了一列：

| 列              | 现在存的值        | 实际含义        |
|-----------------|-------------------|-----------------|
| total           | 13800000000.00    | 垃圾（种子脚本算错） |
| status          | 2026-07-11T14:58… | 其实是 created_at |
| created_at      | 2026-07-12T14:58… | 其实是 paid_at    |
| paid_at         | 2026-07-14T14:58… | 其实是 shipped_at |
| shipped_at      | 21300.0           | **其实是 total** |
| phone           | 杭州西湖区文三路100号 | 其实是 address |
| address         | 3天               | 其实是 delivery_date |
| delivery_date   | 已完成            | 其实是 status    |

危害是**可量化的**：`SUM(total)` 因此从约 41 万被抬高到 **414 亿**——
分析类查询会把这个数字当成真实 GMV 报出去。这正是"脏数据 + LLM = 自信地胡说"的样本。

为什么可以确定性还原
--------------------
三行的 `quantity × unit_price` 与 `shipped_at` 里的金额**精确吻合**
（1500×14.2=21300 / 2000×7.8=15600 / 800×11.6=9280），错位方向一致，
所以除 `phone`（原始值已丢失 → 置空）之外每个字段都能还原。

用法
----
    python scripts/fix_dirty_orders.py                  # 只报告（默认，不动数据）
    python scripts/fix_dirty_orders.py --mode repair    # 按上表还原 + 状态归一化
    python scripts/fix_dirty_orders.py --mode delete    # 直接删除这 3 行（更保守）

三种模式都会写 audit_log（actor=system_data_fix），便于事后追溯。
"""
import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://rain@localhost:5432/study1")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from src.db import execute, query_all  # noqa: E402
from src.logging_config import get_logger  # noqa: E402

logger = get_logger(__name__)

VALID_STATUS = ("待付款", "已付款", "已发货", "已收货", "已取消")
# 种子数据里的"已完成"不在状态集合里 → 归一化到"已收货"（语义最接近的终态）
STATUS_ALIAS = {"已完成": "已收货"}
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_LOOKS_LIKE_ADDRESS = re.compile(r"[省市区县路街号镇村]")


async def find_dirty_rows() -> list:
    """脏行判定：status 不是合法状态（种子那批正好把时间戳塞进了 status）。"""
    rows = await query_all(
        "SELECT * FROM orders WHERE status <> ALL(:valid) ORDER BY id",
        {"valid": list(VALID_STATUS)},
    )
    return rows


def plan_repair(row: dict) -> dict:
    """按错位规律算出差额：返回将要写入的字段（不含未变化字段）。"""
    quantity, unit_price = row["quantity"], float(row["unit_price"])
    expected_total = round(quantity * unit_price, 2)
    shipped_as_total = float(row["shipped_at"]) if row.get("shipped_at") else None

    status = (row.get("delivery_date") or "").strip()
    status = STATUS_ALIAS.get(status, status)
    if status not in VALID_STATUS:
        status = "已收货"          # 兜底：错位行本意都是已完成的历史单

    phone = row.get("phone") or ""
    address = row.get("address") or ""
    return {
        "id": row["id"],
        "total": expected_total,
        "total_matches_shifted": shipped_as_total == expected_total,
        "status": status,
        "created_at": row.get("status"),
        "paid_at": row.get("created_at"),
        "shipped_at": row.get("paid_at"),
        "phone": "" if _LOOKS_LIKE_ADDRESS.search(phone) else phone,
        "address": phone if _LOOKS_LIKE_ADDRESS.search(phone) else address,
        "delivery_date": address,
    }


async def report(rows: list) -> None:
    gmv = await query_all("SELECT coalesce(sum(total),0) g FROM orders")
    valid = await query_all(
        "SELECT coalesce(sum(total),0) g FROM orders WHERE status = ANY(:valid)",
        {"valid": list(VALID_STATUS)},
    )
    print(f"\n发现 {len(rows)} 行 status 非法的订单：")
    for r in rows:
        p = plan_repair(r)
        print(f"\n  ── id={r['id']}  {r['order_no']}  （客户 {r['customer_id']}）")
        print(f"     total         {r['total']}  →  {p['total']}"
              f"   {'✓ 与错位到 shipped_at 的金额一致' if p['total_matches_shifted'] else '⚠️ 与 shipped_at 不一致，请人工确认'}")
        print(f"     status        {r['status']!r}  →  {p['status']!r}")
        print(f"     created_at    {r['created_at']!r}  →  {p['created_at']!r}")
        print(f"     paid_at       {r['paid_at']!r}  →  {p['paid_at']!r}")
        print(f"     shipped_at    {r['shipped_at']!r}  →  {p['shipped_at']!r}")
        print(f"     phone         {r['phone']!r}  →  {p['phone']!r}   （原值无法还原，置空）")
        print(f"     address       {r['address']!r}  →  {p['address']!r}")
        print(f"     delivery_date {r['delivery_date']!r}  →  {p['delivery_date']!r}")
    for r in rows:
        p = plan_repair(r)
        if p["status"] in ("已发货", "已收货") and not p["shipped_at"]:
            print(f"  ⚠️ id={r['id']}：还原后 status={p['status']} 但 shipped_at 为空"
                  f"（源数据本身如此，未擅自补造时间）")
    print(f"\n  影响面：SUM(total) 全表 = {gmv[0]['g']}")
    print(f"          只算合法状态     = {valid[0]['g']}")
    print(f"          差值 = 这 {len(rows)} 行的垃圾金额（分析查询会把它们当成真实 GMV）")


async def do_repair(rows: list) -> int:
    """还原列错位值。修复前把**原值快照**写进 audit_log.detail（可追溯、可回滚）。"""
    import json as _json
    from datetime import datetime as _dt

    snapshot = [
        {"id": r["id"], "order_no": r["order_no"],
         "before": {k: (str(r[k]) if r[k] is not None else None)
                    for k in ("total", "status", "created_at", "paid_at", "shipped_at",
                              "phone", "address", "delivery_date")}}
        for r in rows
    ]
    fixed = 0
    for r in rows:
        p = plan_repair(r)
        if not p["total_matches_shifted"]:
            logger.warning("跳过 id=%s：total 无法确认（shipped_at=%s 与 quantity×unit_price=%s 不符）",
                           r["id"], r["shipped_at"], p["total"])
            continue
        await execute(
            """UPDATE orders SET total = :total, status = :status, created_at = :created_at,
                                 paid_at = :paid_at, shipped_at = :shipped_at,
                                 phone = :phone, address = :address, delivery_date = :dd
               WHERE id = :id""",
            {"total": p["total"], "status": p["status"], "created_at": p["created_at"],
             "paid_at": p["paid_at"], "shipped_at": p["shipped_at"], "phone": p["phone"],
             "address": p["address"], "dd": p["delivery_date"], "id": p["id"]},
        )
        fixed += 1
        logger.info("已修复 id=%s → status=%s total=%s", p["id"], p["status"], p["total"])
    if fixed:
        await execute(
            "INSERT INTO audit_log (actor, action, thread_id, detail, created_at) "
            "VALUES ('system_data_fix', 'orders_repair', '', :d, :ts)",
            {"d": f"修复列错位订单 {fixed} 行；原值快照={_json.dumps(snapshot, ensure_ascii=False)}",
             "ts": _dt.now().isoformat()},
        )
    return fixed


async def do_delete(rows: list) -> int:
    ids = [r["id"] for r in rows]
    await execute("DELETE FROM orders WHERE id = ANY(:ids)", {"ids": ids})
    await execute(
        "INSERT INTO audit_log (actor, action, thread_id, detail, created_at) "
        "VALUES ('system_data_fix', 'orders_delete_dirty', '', :d, :ts)",
        {"d": f"删除列错位订单 {len(ids)} 行：{ids}",
         "ts": __import__("datetime").datetime.now().isoformat()},
    )
    return len(ids)


async def main() -> None:
    ap = argparse.ArgumentParser(description="修复 orders 表列错位脏数据")
    ap.add_argument("--mode", choices=["report", "repair", "delete"], default="report",
                    help="report=只报告（默认）；repair=按错位规律还原；delete=删掉这几行")
    args = ap.parse_args()

    rows = await find_dirty_rows()
    if not rows:
        print("✅ 没有发现 status 非法的订单（无需修复）")
        return
    if args.mode == "report":
        await report(rows)
        print("\n未做任何修改。确认无误后执行："
              "\n  python scripts/fix_dirty_orders.py --mode repair    # 还原"
              "\n  python scripts/fix_dirty_orders.py --mode delete    # 删除")
        return

    await report(rows)
    n = await do_repair(rows) if args.mode == "repair" else await do_delete(rows)
    print(f"\n✅ 完成：{args.mode} 处理 {n} 行（已写 audit_log）")
    after = await query_all("SELECT coalesce(sum(total),0) g, count(*) n FROM orders")
    print(f"   现在 SUM(total) = {after[0]['g']}（共 {after[0]['n']} 单）")


if __name__ == "__main__":
    asyncio.run(main())
