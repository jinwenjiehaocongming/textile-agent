"""
一次性迁移：SQLite → PostgreSQL
===============================
把旧数据迁移到 PG：
- data/products.db  → products 表（281 条）
- data/orders.db    → orders + refunds 表（48 + 7 条）
- data/users/*/chat.db → conversations + profile 表（多租户 user_id 行级隔离）

企业级演进路径的一部分：数据从一个"嵌入式文件"搬到"独立数据库服务"。
运行: python scripts/migrate_sqlite_to_pg.py
"""
import asyncio
import sqlite3
from pathlib import Path
from typing import List, Dict, Any

from src.db import ensure_schema, execute, get_engine

PROJECT_ROOT = Path(__file__).parent.parent


def _read_sqlite(db_path: Path, table: str) -> List[Dict[str, Any]]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(f"SELECT * FROM {table}").fetchall()
    con.close()
    return [dict(r) for r in rows]


async def migrate_business(db_paths: dict[str, Path]) -> None:
    """迁移产品/订单/退款。"""
    # products
    products = _read_sqlite(db_paths["products"], "products")
    for p in products:
        await execute(
            """INSERT INTO products (id, name, category, color, width, weight, stock, moq, price, delivery_days)
               VALUES (:id, :name, :category, :color, :width, :weight, :stock, :moq, :price, :delivery_days)
               ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name""",
            {k: p.get(k) for k in ("id", "name", "category", "color", "width", "weight", "stock", "moq", "price", "delivery_days")},
        )
    print(f"✅ products: {len(products)} 条")

    # orders（保留自增 id，避免业务依赖错乱）
    orders = _read_sqlite(db_paths["orders"], "orders")
    for o in orders:
        await execute(
            """INSERT INTO orders (id, order_no, customer_id, product_id, product_name, color,
                                   quantity, unit_price, total, status, created_at,
                                   paid_at, shipped_at, phone, address, delivery_date)
               VALUES (:id, :order_no, :customer_id, :product_id, :product_name, :color,
                       :quantity, :unit_price, :total, :status, :created_at,
                       :paid_at, :shipped_at, :phone, :address, :delivery_date)
               ON CONFLICT (id) DO UPDATE SET order_no = EXCLUDED.order_no""",
            {k: o.get(k) for k in ("id", "order_no", "customer_id", "product_id", "product_name", "color",
                                   "quantity", "unit_price", "total", "status", "created_at",
                                   "paid_at", "shipped_at", "phone", "address", "delivery_date")},
        )
    print(f"✅ orders: {len(orders)} 条")

    refunds = _read_sqlite(db_paths["orders"], "refunds")
    for r in refunds:
        await execute(
            """INSERT INTO refunds (id, order_no, reason, status, created_at)
               VALUES (:id, :order_no, :reason, :status, :created_at)
               ON CONFLICT (id) DO UPDATE SET order_no = EXCLUDED.order_no""",
            {k: r.get(k) for k in ("id", "order_no", "reason", "status", "created_at")},
        )
    print(f"✅ refunds: {len(refunds)} 条")


async def migrate_memory(users_dir: Path) -> None:
    """迁移每用户 chat.db → conversations/profile（加 user_id 列）。"""
    if not users_dir.is_dir():
        return
    total = 0
    for user_dir in sorted(users_dir.iterdir()):
        chat_db = user_dir / "chat.db"
        if not chat_db.exists():
            continue
        uid = user_dir.name
        try:
            conversations = _read_sqlite(chat_db, "conversations")
            profile = _read_sqlite(chat_db, "profile")
        except sqlite3.Error as e:
            print(f"  ⚠️ 跳过 {uid}: {e}")
            continue
        for c in conversations:
            await execute(
                "INSERT INTO conversations (user_id, role, content, created_at) VALUES (:uid, :role, :content, :ts)",
                {"uid": uid, "role": c.get("role"), "content": c.get("content"), "ts": c.get("created_at")},
            )
        for p in profile:
            await execute(
                "INSERT INTO profile (user_id, key, value, updated_at) VALUES (:uid, :key, :value, :ts) "
                "ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value",
                {"uid": uid, "key": p.get("key"), "value": p.get("value"), "ts": p.get("updated_at")},
            )
        total += len(conversations) + len(profile)
        print(f"  · {uid}: {len(conversations)} 条对话 / {len(profile)} 条 profile")
    print(f"✅ 用户记忆: 共 {total} 条")


async def main() -> None:
    await ensure_schema()
    print("开始迁移...")
    await migrate_business({
        "products": PROJECT_ROOT / "data" / "products.db",
        "orders": PROJECT_ROOT / "data" / "orders.db",
    })
    await migrate_memory(PROJECT_ROOT / "data" / "users")
    # 关键：迁移显式插入 id 后，serial 序列必须同步到 MAX(id)，
    # 否则新写入（下单/退款）会撞主键约束（duplicate key）
    for table in ("orders", "refunds"):
        await execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {table}), 1))"
        )
    await get_engine().dispose()
    print("\n🎉 迁移完成（serial 序列已重置）！")


if __name__ == "__main__":
    asyncio.run(main())