#!/usr/bin/env python
"""孤儿数据清理（加外键前的存量治理）
=====================================
背景：业务表都用 user_id/order_no 关联父表，但账号体系上线前的历史数据里存在大量
"没有父行"的记录（实测 63 个 user_id、50 单订单、236 条对话）。加外键时这些行会被
`VALIDATE CONSTRAINT` 拦下，所以要先清理。

    ⚠️ 这是一次**破坏性**操作（会删数据）。默认只 dry-run 打印，必须显式 --apply。

    python scripts/cleanup_orphans.py             # 只报告：要删什么、多少行、样例
    python scripts/cleanup_orphans.py --apply     # 真的删（先备份到 data/orphans_backup_*.json）

清理规则（按外键的子→父顺序）
------------------------------
- conversations / profile / pending_approvals：user_id 在 users 里不存在 → 删
- sessions：user_id 不存在 → 删（其消息已被上一条覆盖）
- orders：customer_id 或 product_id 在父表里不存在 → 删（连带其退款，外键 CASCADE）
- refunds：order_no 不存在 → 删
- audit_log：**不清理**（它刻意没有外键，且审计要保留"系统做过什么"的证据）

清理完执行 `python scripts/create_admin.py`（内部 ensure_schema）即可完成外键 VALIDATE。
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://rain@localhost:5432/study1")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from src.db import execute, execute_returning, query_all  # noqa: E402

# (标签, 统计 SQL, 删除 SQL, 备份 SQL)
# 备份 SQL 选关键业务字段，供万一要人工回捞
RULES = [
    ("conversations（user_id 无账号）",
     "SELECT count(*) AS n FROM conversations c LEFT JOIN users u ON u.id = c.user_id WHERE u.id IS NULL",
     "DELETE FROM conversations c WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = c.user_id)",
     "SELECT id, user_id, session_id, role, content, created_at FROM conversations c "
     "WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = c.user_id)"),
    ("profile（user_id 无账号）",
     "SELECT count(*) AS n FROM profile p LEFT JOIN users u ON u.id = p.user_id WHERE u.id IS NULL",
     "DELETE FROM profile p WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = p.user_id)", None),
    ("pending_approvals（user_id 无账号）",
     "SELECT count(*) AS n FROM pending_approvals p LEFT JOIN users u ON u.id = p.user_id WHERE u.id IS NULL",
     "DELETE FROM pending_approvals p WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = p.user_id)",
     "SELECT id, thread_id, user_id, draft, status, created_at FROM pending_approvals p "
     "WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = p.user_id)"),
    ("sessions（user_id 无账号）",
     "SELECT count(*) AS n FROM sessions s LEFT JOIN users u ON u.id = s.user_id WHERE u.id IS NULL",
     "DELETE FROM sessions s WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = s.user_id)", None),
    ("orders（customer_id 或 product_id 无父行）",
     "SELECT count(*) AS n FROM orders o WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = o.customer_id) "
     "   OR NOT EXISTS (SELECT 1 FROM products p WHERE p.id = o.product_id)",
     "DELETE FROM orders o WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = o.customer_id) "
     "   OR NOT EXISTS (SELECT 1 FROM products p WHERE p.id = o.product_id)",
     "SELECT order_no, customer_id, product_id, product_name, quantity, unit_price, total, status, created_at "
     "FROM orders o WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = o.customer_id) "
     "   OR NOT EXISTS (SELECT 1 FROM products p WHERE p.id = o.product_id)"),
    ("refunds（order_no 无订单）",
     "SELECT count(*) AS n FROM refunds r LEFT JOIN orders o ON o.order_no = r.order_no WHERE o.order_no IS NULL",
     "DELETE FROM refunds r WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.order_no = r.order_no)", None),
]


async def main() -> None:
    ap = argparse.ArgumentParser(description="清理孤儿数据（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真的执行删除（会先备份）")
    args = ap.parse_args()

    print(f"{'只报告（不改数据）' if not args.apply else '⚠️ 执行删除'}｜"
          f"库：{os.getenv('DATABASE_URL', '').rsplit('/', 1)[-1]}\n")

    plan, total = [], 0
    for label, count_sql, del_sql, backup_sql in RULES:
        n = (await query_all(count_sql))[0]["n"]
        total += n
        plan.append((label, n, del_sql, backup_sql))
        print(f"  {label:34} {n:>5} 行")

    if not total:
        print("\n✅ 没有孤儿数据，外键可以直接 VALIDATE")
        return

    if not args.apply:
        # dry-run：给样例，让人确认"删的确实是该删的"
        print("\n样例（每类前 3 条）：")
        for label, n, _, backup_sql in plan:
            if not n or not backup_sql:
                continue
            rows = await query_all(backup_sql + " LIMIT 3")
            print(f"  ── {label}")
            for r in rows:
                brief = {k: (str(v)[:36] if v is not None else None) for k, v in list(r.items())[:5]}
                print(f"     {brief}")
        print(f"\n共 {total} 行将被删除。确认后执行：python scripts/cleanup_orphans.py --apply")
        return

    # 备份 → 删除 → 审计
    backup = {"ts": datetime.now().isoformat(), "total": total, "tables": {}}
    for label, n, _, backup_sql in plan:
        if n and backup_sql:
            rows = await query_all(backup_sql)
            backup["tables"][label] = [
                {k: (str(v) if v is not None else None) for k, v in r.items()} for r in rows]
    if backup["tables"]:
        out_dir = Path(__file__).parent.parent / "data"
        out_dir.mkdir(exist_ok=True)
        out_file = out_dir / f"orphans_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        out_file.write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n💾 已备份到 {out_file.relative_to(out_file.parent.parent)}（{out_file.stat().st_size} 字节）")

    deleted = {}
    for label, n, del_sql, _ in plan:
        if not n:
            continue
        await execute(del_sql)
        deleted[label] = n
        print(f"  🗑  删除 {label}：{n} 行")

    await execute(
        "INSERT INTO audit_log (actor, action, thread_id, detail, created_at) "
        "VALUES ('system_data_fix', 'cleanup_orphans', '', :d, :ts)",
        {"d": f"清理孤儿数据 {total} 行：" + "；".join(f"{k}={v}" for k, v in deleted.items()),
         "ts": datetime.now().isoformat()})

    # 复核
    left = 0
    for _, count_sql, _, _ in RULES:      # 生成器里不能 await，老老实实循环
        left += (await query_all(count_sql))[0]["n"]
    print(f"\n{'✅ 清理完成' if left == 0 else f'⚠️ 还剩 {left} 行'}（已写 audit_log）")
    print("   下一步：python scripts/create_admin.py  # 内部 ensure_schema，完成外键 VALIDATE")


if __name__ == "__main__":
    asyncio.run(main())
