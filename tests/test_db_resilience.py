"""数据库层韧性测试：外部 DDL 之后的第一次查询不该 500
=====================================================
真实场景（实测踩到）：跑迁移脚本（ALTER TABLE ... TYPE numeric）时服务还在跑，
连接池里缓存的 prepared statement 立刻失效 → **运行中的服务第一条查询 500、
第二条才正常**。滚动发版时这就是"旧实例突然吐一批 500"。

修法：db 层识别 `cached statement plan is invalid` → 重建连接池 → 重试一次。
"""
from src.db import execute, query_all, query_one


async def test_query_survives_external_ddl(pg_db):
    """先查一次（把语句缓存进 prepared statement），外部 DDL 后再查必须成功。"""
    await execute("INSERT INTO products (id, name, category, color, width, weight, stock, moq, price, delivery_days) "
                  "VALUES ('RES1', '韧性测试布', '化纤面料', '黑', 150, '100D', 10, 5, 9.9, 7)")

    first = await query_all("SELECT id, price FROM products WHERE id = 'RES1'")
    assert first and first[0]["id"] == "RES1"

    # 外部 DDL（模拟迁移脚本在服务运行时改表结构）
    await execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS _tmp_probe TEXT")

    same_query = await query_all("SELECT id, price FROM products WHERE id = 'RES1'")
    assert same_query and same_query[0]["id"] == "RES1", "外部 DDL 后第一条查询也必须成功"

    # 写路径同样要能自愈
    await execute("UPDATE products SET stock = 20 WHERE id = 'RES1'")
    await execute("ALTER TABLE products DROP COLUMN IF EXISTS _tmp_probe")
    assert (await query_one("SELECT stock FROM products WHERE id = 'RES1'"))["stock"] == 20
