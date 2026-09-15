"""分析取数层测试（只读安全 + 结果封顶 + 结构自省）
=================================================
这一层是数据分析 Agent 的**安全命门**：它执行的是 LLM 生成的 SQL（不可信输入）。
所以测试重点不是"能不能查"，而是**危险语句一条都不能执行**。

覆盖：
1. 语句层校验：非 SELECT/WITH、多语句、注释绕过、关键字黑名单、系统表 —— 全部拒绝
2. 结果层：强制 LIMIT 兜底、截断标记、聚合查询不受影响
3. 结构自省：list_tables / describe_table（表名白名单，防注入）
4. 账号层（可选）：配了 ANALYTICS_DATABASE_URL 时，只读角色必须写不进去
"""
import os

import pytest

from src.analytics import sql as A


# ── 1. 语句层校验（纯函数，不碰库）──────────────────────────

@pytest.mark.parametrize("bad,why", [
    ("DELETE FROM orders", "非 SELECT 开头"),
    ("UPDATE orders SET total = 0", "非 SELECT 开头"),
    ("DROP TABLE orders", "非 SELECT 开头"),
    ("INSERT INTO orders VALUES (1)", "非 SELECT 开头"),
    ("WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x", "CTE 里藏 DML"),
    ("SELECT 1; DROP TABLE orders", "多语句"),
    ("SELECT * FROM pg_shadow", "系统表"),
    ("SELECT * FROM information_schema.tables", "系统视图"),
    ("SELECT pg_sleep(30)", "危险函数"),
    ("SELECT 1 -- t", "注释绕过"),
    ("SELECT 1 /* x */", "块注释绕过"),
    ("", "空语句"),
])
def test_validate_rejects(bad, why):
    with pytest.raises(A.SqlRejected):
        A.validate(bad)


@pytest.mark.parametrize("good", [
    "SELECT 1",
    "select count(*) from orders",
    "WITH t AS (SELECT 1 AS a) SELECT * FROM t",
    "SELECT status, count(*) FROM orders GROUP BY status",
    "SELECT * FROM orders;",                       # 结尾分号允许（会被去掉）
])
def test_validate_accepts(good):
    assert A.validate(good)


def test_wrap_forces_limit():
    wrapped = A.wrap_with_limit("SELECT * FROM orders", 10)
    assert "LIMIT 10" in wrapped and "_analytics_sub" in wrapped


# ── 2. 结果层：跑真库（测试库）──────────────────────────────

async def test_run_sql_select_works(pg_db):
    from src.db import execute
    await execute("INSERT INTO products (id, name, category, color, width, weight, stock, moq, price, delivery_days) "
                  "VALUES ('AN1', '分析测试布', '化纤面料', '黑色', 150, '100D', 10, 5, 12.34, 7)")
    r = await A.run_readonly_sql("SELECT name, price FROM products WHERE id = 'AN1'")
    assert r["ok"] and r["columns"] == ["name", "price"]
    assert r["rows"][0][0] == "分析测试布"
    assert float(r["rows"][0][1]) == 12.34


async def test_run_sql_rejects_and_reports(pg_db):
    r = await A.run_readonly_sql("DELETE FROM orders")
    assert r["ok"] is False and "安全策略拒绝" in r["error"]
    assert r["rows"] == [] and r["row_count"] == 0


async def test_run_sql_enforces_and_flags_truncation(pg_db):
    from src.db import execute_many
    from src.users import ensure_user_row
    for i in range(12):
        await ensure_user_row(f"u{i}")     # conversations.user_id 有外键
    await execute_many(
        "INSERT INTO conversations (user_id, session_id, role, content, created_at) "
        "VALUES (:u, 's', 'human', 'x', :t)",
        [{"u": f"u{i}", "t": "2026-01-01T00:00:00"} for i in range(12)])
    r = await A.run_readonly_sql("SELECT * FROM conversations", limit=5)
    assert r["ok"] and r["row_count"] == 5 and r["truncated"] is True
    r = await A.run_readonly_sql("SELECT count(*) AS n FROM conversations")
    assert r["rows"][0][0] == 12 and r["truncated"] is False


async def test_run_sql_reports_sql_errors_without_raising(pg_db):
    """SQL 写错 → 返回错误字符串（方便喂回 LLM 自纠错），不抛异常炸掉整个分析流程。"""
    r = await A.run_readonly_sql("SELECT no_such_column FROM orders")
    assert r["ok"] is False and "SQL 执行失败" in r["error"]


# ── 3. 结构自省 ──────────────────────────────────────────────

async def test_list_tables_and_describe(pg_db):
    tables = {t["table"] for t in await A.list_tables()}
    assert {"orders", "products", "refunds", "pending_approvals"} <= tables
    info = await A.describe_table("orders")
    assert info["ok"] and any(c["column_name"] == "total" for c in info["columns"])
    bad = await A.describe_table("orders; DROP TABLE users")
    assert bad["ok"] is False and "表不存在" in bad["error"]


def test_semantic_hints_contains_key_facts():
    """语义层必须包含"没有外键时的 JOIN 依据"和几个关键口径——LLM 全靠它。"""
    text = A_import_hints()
    for needle in ["orders.customer_id", "退款率", "TEXT（ISO 字符串）", "已取消", "演示数据",
                   # 这两条是踩坑后加的：时间范围口径（同一问题两个数字）+ 退款金额口径
                   "时间范围", "refunds 表没有金额列"]:
        assert needle in text, f"语义层缺少关键信息：{needle}"


def A_import_hints():
    from src.analytics import schema_hints
    return schema_hints.semantic_hints()


# ── 4. 账号层：只读角色真的写不进去 ─────────────────────────
# ⚠️ 这里必须**显式从 .env 读**只读角色 DSN，不能用 ANALYTICS_DATABASE_URL：
# conftest 在 .env 加载之前就把它设成了测试库的 owner DSN（load_dotenv 默认不覆盖
# 已存在的环境变量），拿它测会连上"有写权限的 owner"，用例等于什么都没验证
# （真实踩过：CREATE TABLE 没报错，测试假绿）。

def _readonly_role_dsn() -> str:
    from dotenv import dotenv_values
    from pathlib import Path
    env_path = Path(__file__).parent.parent / ".env"
    return os.getenv("ANALYTICS_RO_URL", "") or (dotenv_values(env_path).get("ANALYTICS_DATABASE_URL") or "")


async def test_readonly_role_cannot_write(pg_db):
    """只读角色：能 SELECT，不能写（CREATE/INSERT/UPDATE 全被数据库拒绝）。"""
    # 先清掉历史遗留（早期版本这条断言失败过，可能在库里留下 _probe 表，
    # 而 reset_schema 只清已知表 → 会被 test_schema_integrity 的"表清单"用例抓到）
    from src.db import execute as _exec
    await _exec("DROP TABLE IF EXISTS _probe")
    dsn = _readonly_role_dsn()
    if not dsn:
        pytest.skip("未配只读角色 DSN（scripts/create_analytics_role.py 可生成）")
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    test_dsn = dsn.rsplit("/", 1)[0] + "/" + os.environ["DATABASE_URL"].rsplit("/", 1)[-1]
    eng = create_async_engine(test_dsn)
    try:
        async with eng.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM orders"))).scalar() is not None, "只读角色应能读"

        async def _should_fail(sql):
            with pytest.raises(Exception):
                async with eng.begin() as conn:
                    await conn.execute(text(sql))

        await _should_fail("CREATE TABLE _probe (id int)")
        await _should_fail("INSERT INTO products (id, name) VALUES ('X', 'X')")
        await _should_fail("UPDATE orders SET total = 0")
        await _should_fail("DELETE FROM orders")
        await _should_fail("DROP TABLE orders")
    finally:
        await eng.dispose()
