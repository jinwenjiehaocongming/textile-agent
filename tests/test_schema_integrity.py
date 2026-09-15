"""数据完整性测试（2026-09 加外键）
==================================
这一组用例是"表结构与语义层不漂移"的保险：

1. **外键真的存在且已校验**：7 条核心关系，`convalidated = true`；
2. **外键真的会拦人**：写"没有账号行的订单"必须被拒；
3. **RESTRICT/CASCADE 语义正确**：删用户被拦（订单不能跟着消失）、删订单带走退款；
4. **语义层完整性**：public 下每张表都在 TABLES 里登记、RELATIONS 里提到的表/列都真实存在
   —— 这条正是之前"改 schema 忘了改语义层"这个短板的补丁；
5. **关系能从数据库自动读出来**（不再依赖手写），且读到的行数 = 外键数。
"""
import pytest

from src.analytics import schema_hints as H
from src.db import execute, query_all, query_one


# ── 1. 外键存在且已校验 ─────────────────────────────────────

EXPECTED_FKS = {
    "fk_refunds_order_no", "fk_orders_product_id", "fk_orders_customer_id",
    "fk_sessions_user_id", "fk_conversations_user_id", "fk_profile_user_id",
    "fk_pending_approvals_user_id",
}


async def test_foreign_keys_exist_and_validated(pg_db):
    rows = await query_all(
        "SELECT conname, convalidated FROM pg_constraint WHERE contype = 'f'")
    names = {r["conname"] for r in rows}
    assert EXPECTED_FKS <= names, f"缺少外键：{EXPECTED_FKS - names}"
    assert all(r["convalidated"] for r in rows), "外键必须通过 VALIDATE（否则只是 NOT VALID）"
    # audit_log 刻意没有外键：它要能记录 system/unknown/已删除账号
    audit_fk = [r for r in rows if "audit" in r["conname"]]
    assert audit_fk == [], "audit_log 不应有外键（审计主体可以是系统或已删除账号）"


# ── 2. 外键真的会拦人 ───────────────────────────────────────

async def test_fk_rejects_order_without_account(pg_db):
    from src.users import ensure_user_row
    await ensure_user_row("fk_ok_user")
    with pytest.raises(Exception) as ei:
        await execute(
            "INSERT INTO orders (order_no, customer_id, product_id, product_name, color, quantity, "
            "unit_price, total, status, created_at) VALUES "
            "('ORD-NOUSER', 'no_such_user', 'P001', '布', '黑', 1, 1.0, 1.0, '待付款', '2026-01-01T00:00:00')")
    assert "fk_orders_customer_id" in str(ei.value)
    # 有账号行就能写（影子账号机制）
    await execute(
        "INSERT INTO orders (order_no, customer_id, product_id, product_name, color, quantity, "
        "unit_price, total, status, created_at) VALUES "
        "('ORD-OKUSER', 'fk_ok_user', 'P001', '布', '黑', 1, 1.0, 1.0, '待付款', '2026-01-01T00:00:00')")
    assert (await query_one("SELECT count(*) AS n FROM orders WHERE order_no = 'ORD-OKUSER'"))["n"] == 1


async def test_delete_semantics_restrict_vs_cascade(pg_db):
    """RESTRICT：删用户不许带走订单；CASCADE：删订单带走它的退款。"""
    from src.users import ensure_user_row
    await ensure_user_row("fk_del_user")
    await execute(
        "INSERT INTO orders (order_no, customer_id, product_id, product_name, color, quantity, "
        "unit_price, total, status, created_at) VALUES "
        "('ORD-DEL', 'fk_del_user', 'P001', '布', '黑', 1, 1.0, 1.0, '已发货', '2026-01-01T00:00:00')")
    await execute("INSERT INTO refunds (order_no, reason, status, created_at) "
                  "VALUES ('ORD-DEL', '色差', '待审核', '2026-01-02T00:00:00')")

    with pytest.raises(Exception) as ei:
        await execute("DELETE FROM users WHERE id = 'fk_del_user'")
    assert "fk_orders_customer_id" in str(ei.value), "删用户必须被 RESTRICT 拦住"

    await execute("DELETE FROM orders WHERE order_no = 'ORD-DEL'")
    assert (await query_one("SELECT count(*) AS n FROM refunds WHERE order_no = 'ORD-DEL'"))["n"] == 0, \
        "退款应随订单 CASCADE 删除"


# ── 3. 语义层完整性（防"改 schema 忘改语义层"）──────────────

async def test_semantic_layer_covers_every_table(pg_db):
    """public 下每张业务表都必须在语义层 TABLES 里登记。

    这是补上之前的短板：手写语义层时没有测试守着，新增表忘了登记，
    Agent 就永远不知道那张表存在（而且不会报错）。
    """
    rows = await query_all(
        "SELECT c.relname AS t FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY 1")
    actual = {r["t"] for r in rows}
    documented = set(H.TABLES)
    assert actual <= documented, f"这些表没在语义层登记：{actual - documented}"
    assert documented <= actual, f"语义层登记了不存在的表：{documented - actual}"


async def test_semantic_relations_reference_real_columns(pg_db):
    """RELATIONS 里写的每一处 表.列 都必须真实存在（拼错就是给模型喂假关系）。"""
    cols = {(r["table_name"], r["column_name"]) for r in await query_all(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public'")}
    import re
    bad = []
    for line in H.RELATIONS.strip().splitlines():
        line = line.split("--")[0]
        for token in re.findall(r"([a-z_]+)\.([a-z_]+)", line):
            if token not in cols:
                bad.append(f"{token[0]}.{token[1]}")
    assert bad == [], f"语义层里的关系指向了不存在的列：{bad}"


async def test_relations_are_read_from_foreign_keys(pg_db):
    """表关系能从数据库自动读出来（加外键的收益：不用手写、不会漂移）。"""
    fk_text = await H.load_fk_text()
    assert fk_text, "应能从 pg_constraint 读到外键关系"
    assert fk_text.count("\n") + 1 == len(EXPECTED_FKS), \
        f"读到的关系数应与外键数一致（读到 {fk_text.count(chr(10)) + 1} 条）"
    assert "orders.customer_id = users.id" in fk_text
    hints = H.semantic_hints(fk_text)
    assert "数据库外键自动读取" in hints and "外键管不到的语义关系" in hints


async def test_no_orphans_anywhere(pg_db):
    """兜底：库内不应存在任何孤儿（外键保证的前提）。"""
    checks = {
        "orders.customer_id": "SELECT count(*) c FROM orders o WHERE NOT EXISTS "
                              "(SELECT 1 FROM users u WHERE u.id = o.customer_id)",
        "orders.product_id": "SELECT count(*) c FROM orders o WHERE NOT EXISTS "
                             "(SELECT 1 FROM products p WHERE p.id = o.product_id)",
        "conversations.user_id": "SELECT count(*) c FROM conversations c WHERE NOT EXISTS "
                                 "(SELECT 1 FROM users u WHERE u.id = c.user_id)",
        "refunds.order_no": "SELECT count(*) c FROM refunds r WHERE NOT EXISTS "
                            "(SELECT 1 FROM orders o WHERE o.order_no = r.order_no)",
    }
    for label, sql in checks.items():
        assert (await query_one(sql))["c"] == 0, f"{label} 存在孤儿数据"
