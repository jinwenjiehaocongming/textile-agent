"""管理端工作台（订单列表 / 状态机 / 退款审核）测试（2026-09）
==============================================================
这一批接口补的是"原来后端一个都没有"的能力，所以每个用例都对着一个真实缺口：

**A. 全站订单列表**
1. 管理员能看到**别人的**单（`/orders` 只看得到自己的 —— 这是管理端存在的理由）
2. 状态筛选 / 关键词筛选（订单号、客户 id、产品名）/ 分页（total 是筛选后的总数，不是页大小）
3. 非法状态筛选值 → 400（拼进 SQL 之前就被白名单挡掉）

**B. 订单状态机**
4. 合法流转：待付款 → 已付款 → 已发货 → 已收货，且**顺手补 paid_at / shipped_at**
   （不补的话分析模块的"付款时效/发货时效"永远是空的）
5. 非法流转 → 400：跳步（待付款 → 已发货）、终态再改（已取消 → 已付款）、原地打转
6. **失败必须不留痕**：被拒的流转不能改到库里的 status
7. 订单不存在 → 404（不是 500）

**C. 退款审核**
8. 待审核 → 已通过 / 已驳回，落 decided_at / decided_by / note
9. **只能审一次**：重复审核 → 400（CAS，防两个管理员同时点）
10. 工单不存在 → 404

**D. 权限**
11. 无 token 401 / 客户 403 —— 五个端点逐个验，避免"改了一个漏了另一个"

**E. 审计**
12. 每次状态流转 / 退款审核都写 `audit_log`（谁、何时、把哪张单从什么状态改成了什么）
"""
import os

os.environ["DEV_MODE"] = "1"
os.environ["JWT_SECRET"] = "test-secret-only-0123456789abcdef0123456789abcdef"

import httpx  # noqa: E402
import pytest  # noqa: E402

from src.auth import create_token  # noqa: E402
from src.db import execute, query_all, query_one  # noqa: E402
from src.users import ensure_user_row  # noqa: E402
from app import app  # noqa: E402

ADMIN = "boss_admin"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {create_token(ADMIN, role='admin')}"}


async def _seed_user(uid: str, name: str = "") -> None:
    await ensure_user_row(uid, display_name=name or uid)


async def _seed_order(order_no: str, status: str = "待付款", customer_id: str = "cust_1",
                      product_id: str = "P001", product_name: str = "T400 复合弹力布",
                      total: float = 1000.0, created_at: str = "2026-09-01T10:00:00",
                      quantity: int = 100, unit_price: float = 10.0) -> None:
    await _seed_user(customer_id, "客户甲")
    await execute(
        """INSERT INTO orders (order_no, customer_id, product_id, product_name, color,
                               quantity, unit_price, total, status, created_at,
                               phone, address, delivery_date)
           VALUES (:no, :cid, :pid, :pname, '黑色', :qty, :up, :total, :st, :ts,
                   '13800000000', '浙江省绍兴市柯桥区', '2026-09-20')""",
        {"no": order_no, "cid": customer_id, "pid": product_id, "pname": product_name,
         "qty": quantity, "up": unit_price, "total": total, "st": status, "ts": created_at},
    )


async def _audit_rows(action: str, thread_id: str = "") -> list:
    """按动作 + 订单号取审计行。

    ⚠️ `audit_log` **刻意不参与 `reset_schema`**（审计要能活过重置，含已删除账号的记录），
    所以用例之间（**甚至两次 pytest 运行之间**）它会累积 —— 断言必须按 thread_id 精确定位 +
    用"动作前后增量"而不是绝对条数，否则第二次跑同一份代码就会红。
    """
    if thread_id:
        return await query_all(
            "SELECT id, actor, action, thread_id, detail FROM audit_log "
            "WHERE action = :a AND thread_id = :t ORDER BY id", {"a": action, "t": thread_id})
    return await query_all("SELECT id, actor, action, thread_id, detail FROM audit_log "
                           "WHERE action = :a ORDER BY id", {"a": action})


async def _seed_refund(order_no: str, status: str = "待审核", reason: str = "尺寸不符") -> int:
    await execute(
        "INSERT INTO refunds (order_no, reason, status, created_at) "
        "VALUES (:no, :reason, :st, '2026-09-02T09:00:00')",
        {"no": order_no, "reason": reason, "st": status},
    )
    return (await query_one("SELECT max(id) AS i FROM refunds"))["i"]


# ══════════════════════════════════════════════════════════════
# A. 全站订单列表
# ══════════════════════════════════════════════════════════════

async def test_admin_sees_all_orders_not_just_own(pg_db, clean_auth_store):
    """管理员看到全部订单；同一批数据下 `/orders`（我的订单）只看到自己的。

    这是管理端订单页存在的**唯一理由**：客户视角的接口天然看不到别人的单。
    """
    await _seed_order("ORD-A1", customer_id="cust_a")
    await _seed_order("ORD-A2", customer_id="cust_b", status="已付款")

    async with _client() as c:
        r = await c.get("/admin/orders", headers=_admin_headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 2
        assert {o["order_no"] for o in body["orders"]} == {"ORD-A1", "ORD-A2"}

        # 管理员自己的"我的订单"反而是空的（他就是没有自己的单）
        mine = await c.get("/orders", headers=_admin_headers())
        assert mine.json()["orders"] == []


async def test_admin_order_filters_and_paging(pg_db, clean_auth_store):
    """状态 / 关键词筛选 + 分页；**total 是筛选后的总数**（前端要靠它算页数）。"""
    await _seed_order("ORD-B1", status="待付款", customer_id="cust_b1")
    await _seed_order("ORD-B2", status="已发货", customer_id="cust_b2",
                      product_name="380T 尼丝纺", product_id="P002")
    await _seed_order("ORD-B3", status="已发货", customer_id="cust_b3",
                      product_name="380T 尼丝纺", product_id="P002")

    async with _client() as c:
        h = _admin_headers()
        r = await c.get("/admin/orders", params={"status": "已发货"}, headers=h)
        assert {o["order_no"] for o in r.json()["orders"]} == {"ORD-B2", "ORD-B3"}
        assert r.json()["total"] == 2

        r = await c.get("/admin/orders", params={"keyword": "尼丝纺"}, headers=h)
        assert r.json()["total"] == 2

        r = await c.get("/admin/orders", params={"keyword": "cust_b1"}, headers=h)
        assert r.json()["total"] == 1 and r.json()["orders"][0]["order_no"] == "ORD-B1"

        # 产品编号（客服最常拿到的查找依据）和客户昵称也要能搜到 ——
        # 关键词里的 u.display_name 让 count 查询必须也带 LEFT JOIN，
        # 否则计数直接 SQL 报错（只给列表查询加 join 的经典漏改）
        r = await c.get("/admin/orders", params={"keyword": "P002"}, headers=h)
        assert r.json()["total"] == 2, r.text
        r = await c.get("/admin/orders", params={"keyword": "客户甲"}, headers=h)
        assert r.json()["total"] == 3, r.text
        # 搜不到时也要正常返回 0，而不是报错
        r = await c.get("/admin/orders", params={"keyword": "不存在的东西"}, headers=h)
        assert r.status_code == 200 and r.json()["total"] == 0

        r = await c.get("/admin/orders", params={"keyword": "ORD-B3"}, headers=h)
        assert r.json()["orders"][0]["order_no"] == "ORD-B3"

        # 分页：limit=1 offset=1 拿到的是"中间那一单"，而 total 仍是 3
        r = await c.get("/admin/orders", params={"limit": 1, "offset": 1}, headers=h)
        body = r.json()
        assert body["total"] == 3 and len(body["orders"]) == 1
        first = (await c.get("/admin/orders", params={"limit": 1}, headers=h)).json()
        assert first["orders"][0]["order_no"] != body["orders"][0]["order_no"]

        # limit 上限被夹住（防止一次拉全表）
        r = await c.get("/admin/orders", params={"limit": 99999}, headers=h)
        assert r.status_code == 200


async def test_admin_order_list_rejects_illegal_status(pg_db, clean_auth_store):
    """状态筛选值走白名单：注入式/拼错的值直接 400，不落到 SQL 里。"""
    await _seed_order("ORD-C1")
    async with _client() as c:
        r = await c.get("/admin/orders", params={"status": "已付款' OR '1'='1"}, headers=_admin_headers())
        assert r.status_code == 400
        assert "非法状态" in r.json()["detail"]


# ══════════════════════════════════════════════════════════════
# B. 状态机
# ══════════════════════════════════════════════════════════════

async def test_order_transition_happy_path_sets_timestamps(pg_db, clean_auth_store):
    """合法链路走通，并且**补上 paid_at / shipped_at**（分析模块的时效指标靠它）。"""
    await _seed_order("ORD-D1", status="待付款")
    async with _client() as c:
        h = _admin_headers()
        r = await c.post("/admin/orders/ORD-D1/status", json={"status": "已付款"}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "order_no": "ORD-D1", "from": "待付款", "to": "已付款"}

        row = await query_one("SELECT status, paid_at FROM orders WHERE order_no = 'ORD-D1'")
        assert row["status"] == "已付款" and row["paid_at"], "付款必须落 paid_at"

        r = await c.post("/admin/orders/ORD-D1/status",
                         json={"status": "已发货", "note": "顺丰 SF123"}, headers=h)
        assert r.status_code == 200
        row = await query_one("SELECT status, shipped_at FROM orders WHERE order_no = 'ORD-D1'")
        assert row["status"] == "已发货" and row["shipped_at"], "发货必须落 shipped_at"

        assert (await c.post("/admin/orders/ORD-D1/status",
                             json={"status": "已收货"}, headers=h)).status_code == 200
        assert (await query_one(
            "SELECT status FROM orders WHERE order_no='ORD-D1'"))["status"] == "已收货"


async def test_order_transition_illegal_rejected_and_db_untouched(pg_db, clean_auth_store):
    """跳步 / 原地打转 / 终态再改 → 400，且**库里状态一点没动**。

    "被拒了但数据还是被改了" 是状态机最危险的假成功，所以这里两种都断言。
    """
    await _seed_order("ORD-E1", status="待付款")
    await _seed_order("ORD-E2", status="已取消")
    await _seed_order("ORD-E3", status="已收货")
    before = {no: len(await _audit_rows("order_status_change", no))
              for no in ("ORD-E1", "ORD-E2", "ORD-E3")}

    async with _client() as c:
        h = _admin_headers()
        # 跳步：待付款 → 已发货
        r = await c.post("/admin/orders/ORD-E1/status", json={"status": "已发货"}, headers=h)
        assert r.status_code == 400 and "不允许从" in r.json()["detail"]

        # 原地打转
        r = await c.post("/admin/orders/ORD-E1/status", json={"status": "待付款"}, headers=h)
        assert r.status_code == 400 and "已经是" in r.json()["detail"]

        # 终态不可再变
        r = await c.post("/admin/orders/ORD-E2/status", json={"status": "已付款"}, headers=h)
        assert r.status_code == 400 and "终态" in r.json()["detail"]
        r = await c.post("/admin/orders/ORD-E3/status", json={"status": "已发货"}, headers=h)
        assert r.status_code == 400

        # 目标状态本身非法（不在枚举里）
        r = await c.post("/admin/orders/ORD-E1/status", json={"status": "已退款X"}, headers=h)
        assert r.status_code == 400 and "非法状态" in r.json()["detail"]

        # 「已退款」是合法枚举值，但**不能从管理端手工改到**：它只能由退款工单决定
        # （否则会出现"订单显示已退款、却没有一张审核过的工单"——钱和账对不上）
        r = await c.post("/admin/orders/ORD-E1/status", json={"status": "已退款"}, headers=h)
        assert r.status_code == 400 and "只能由退款工单决定" in r.json()["detail"]
        r = await c.post("/admin/orders/ORD-E1/status", json={"status": "退款中"}, headers=h)
        assert r.status_code == 400 and "只能由退款工单决定" in r.json()["detail"]

        # 订单不存在 → 404
        r = await c.post("/admin/orders/ORD-NOPE/status", json={"status": "已付款"}, headers=h)
        assert r.status_code == 404

    # 三次失败之后，三张单的状态原封不动
    rows = {r["order_no"]: r["status"] for r in
            await query_all("SELECT order_no, status FROM orders")}
    assert rows == {"ORD-E1": "待付款", "ORD-E2": "已取消", "ORD-E3": "已收货"}
    for no in ("ORD-E1", "ORD-E2", "ORD-E3"):
        assert len(await _audit_rows("order_status_change", no)) == before[no], \
            f"{no} 有被拒的流转却写了审计"


async def test_cancel_path_and_audit_trail(pg_db, clean_auth_store):
    """取消（待付款 → 已取消）可用，且**每次成功流转都留审计**。"""
    await _seed_order("ORD-F1", status="待付款")
    before = len(await _audit_rows("order_status_change", "ORD-F1"))
    async with _client() as c:
        h = _admin_headers()
        assert (await c.post("/admin/orders/ORD-F1/status",
                             json={"status": "已取消", "note": "客户改主意"},
                             headers=h)).status_code == 200
        logs = await _audit_rows("order_status_change", "ORD-F1")
    assert len(logs) == before + 1, "一次成功流转 = 一条审计"
    last = logs[-1]
    assert last["actor"] == ADMIN and last["thread_id"] == "ORD-F1"
    assert "待付款 → 已取消" in last["detail"] and "客户改主意" in last["detail"]


# ══════════════════════════════════════════════════════════════
# C. 退款审核
# ══════════════════════════════════════════════════════════════

async def test_refund_decide_approve_and_reject(pg_db, clean_auth_store):
    """退款工单**终于有人能审了**：通过 / 驳回，落审核人、时间、备注。"""
    await _seed_order("ORD-G1")
    await _seed_order("ORD-G2", customer_id="cust_2")
    rid_a = await _seed_refund("ORD-G1")
    rid_r = await _seed_refund("ORD-G2", reason="发错色号")
    before_a = len(await _audit_rows("refund_decision", "ORD-G1"))
    before_r = len(await _audit_rows("refund_decision", "ORD-G2"))

    async with _client() as c:
        h = _admin_headers()
        body = (await c.get("/admin/refunds",
                            params={"status": "待审核"}, headers=h)).json()
        assert len(body["refunds"]) == 2
        assert body["refunds"][0]["product_name"] == "T400 复合弹力布"   # 带订单信息

        r = await c.post(f"/admin/refunds/{rid_a}/decide",
                         json={"approve": True, "note": "同意退款"}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "已通过"

        r = await c.post(f"/admin/refunds/{rid_r}/decide",
                         json={"approve": False, "note": "已过 7 天"}, headers=h)
        assert r.json()["status"] == "已驳回"

    row = await query_one("SELECT status, decided_by, decided_at, note FROM refunds WHERE id = :i",
                          {"i": rid_a})
    assert row["status"] == "已通过" and row["decided_by"] == ADMIN
    assert row["decided_at"] and row["note"] == "同意退款"
    assert len(await _audit_rows("refund_decision", "ORD-G1")) == before_a + 1
    assert len(await _audit_rows("refund_decision", "ORD-G2")) == before_r + 1


async def test_refund_can_only_be_decided_once(pg_db, clean_auth_store):
    """重复审核 → 400（CAS）：两个管理员同时点"通过"，只有一个能落库。"""
    await _seed_order("ORD-H1")
    rid = await _seed_refund("ORD-H1")
    async with _client() as c:
        h = _admin_headers()
        assert (await c.post(f"/admin/refunds/{rid}/decide", json={"approve": True},
                             headers=h)).status_code == 200
        r = await c.post(f"/admin/refunds/{rid}/decide", json={"approve": False}, headers=h)
        assert r.status_code == 400
        assert "已被处理过" in r.json()["detail"]

        # 不存在的工单 → 404；非法状态筛选 → 400
        assert (await c.post("/admin/refunds/999999/decide", json={"approve": True},
                             headers=h)).status_code == 404
        assert (await c.get("/admin/refunds", params={"status": "已退款"},
                            headers=h)).status_code == 400

    # 第一次的决定没有被第二次覆盖
    assert (await query_one("SELECT status FROM refunds WHERE id = :i",
                            {"i": rid}))["status"] == "已通过"


# ══════════════════════════════════════════════════════════════
# D. 工作台指标
# ══════════════════════════════════════════════════════════════

async def test_summary_counts(pg_db, clean_auth_store):
    """工作台指标：待发货 / 待付款 / 退款待审 / 订单总数 / 本月 GMV。"""
    from datetime import datetime
    this_month = datetime.now().strftime("%Y-%m")
    await _seed_order("ORD-I1", status="待付款", created_at=f"{this_month}-01T10:00:00", total=100.0)
    await _seed_order("ORD-I2", status="已付款", created_at=f"{this_month}-02T10:00:00", total=250.5,
                      customer_id="cust_i2")
    await _seed_order("ORD-I3", status="已取消", created_at=f"{this_month}-03T10:00:00", total=999.0,
                      customer_id="cust_i3")
    await _seed_order("ORD-I4", status="已收货", created_at="2020-01-05T10:00:00", total=777.0,
                      customer_id="cust_i4")
    await _seed_refund("ORD-I4")

    async with _client() as c:
        r = await c.get("/admin/orders/summary", headers=_admin_headers())
        assert r.status_code == 200, r.text
        s = r.json()

    assert s["orders_total"] == 4
    assert s["to_ship"] == 1          # 已付款 = 待发货
    assert s["unpaid"] == 1
    assert s["refunds_to_review"] == 1
    # 本月 GMV 只算本月且排除已取消：100 + 250.5
    assert float(s["gmv_this_month"]) == pytest.approx(350.5)
    assert s["orders_this_month"] == 3
    assert s["pending_approvals"] == 0
    assert isinstance(s["trend_7d"], list)


async def test_summary_trend_is_exactly_7_days(pg_db, clean_auth_store):
    """`trend_7d` 必须正好覆盖 7 个自然日（含今天）—— 边界日回归。

    踩过的坑：`created_at >= now() - interval '7 days'` 会**多算一天**（8 个自然日），
    前端按 7 根柱子画，"近 7 天合计"和后端数据对不上账。所以第 7 天前的单不能进窗。
    """
    from datetime import datetime, timedelta
    now = datetime.now()
    day = lambda n: (now - timedelta(days=n)).strftime("%Y-%m-%d")  # noqa: E731
    await _seed_order("ORD-T0", created_at=f"{day(0)}T09:00:00", customer_id="cust_t0")
    await _seed_order("ORD-T6", created_at=f"{day(6)}T23:59:00", customer_id="cust_t6")
    await _seed_order("ORD-T7", created_at=f"{day(7)}T00:00:01", customer_id="cust_t7")

    async with _client() as c:
        s = (await c.get("/admin/orders/summary", headers=_admin_headers())).json()

    days = [t["day"] for t in s["trend_7d"]]
    assert days == [day(6), day(0)] or days == sorted(days), f"趋势日期应升序且只含窗内: {days}"
    assert day(6) in days and day(0) in days
    assert day(7) not in days, "第 7 天前的订单不该出现在近 7 天趋势里"
    assert sum(t["orders"] for t in s["trend_7d"]) == 2


# ══════════════════════════════════════════════════════════════
# E. 权限（五个端点逐个验）
# ══════════════════════════════════════════════════════════════

async def test_admin_endpoints_require_admin(pg_db, clean_auth_store):
    """无 token → 401；客户 token → 403。**写接口尤其不能漏**。"""
    await _seed_order("ORD-J1")
    rid = await _seed_refund("ORD-J1")
    cust = {"Authorization": f"Bearer {create_token('cust_j', role='customer')}"}

    calls = [
        ("get", "/admin/orders", None),
        ("get", "/admin/orders/summary", None),
        ("post", "/admin/orders/ORD-J1/status", {"status": "已付款"}),
        ("get", "/admin/refunds", None),
        ("post", f"/admin/refunds/{rid}/decide", {"approve": True}),
    ]
    async with _client() as c:
        for method, url, payload in calls:
            r = await c.request(method, url, json=payload)
            assert r.status_code == 401, f"{method} {url} 无 token 应 401，实际 {r.status_code}"
            r = await c.request(method, url, json=payload, headers=cust)
            assert r.status_code == 403, f"{method} {url} 客户应 403，实际 {r.status_code}"

    # 越权尝试没有产生任何副作用
    assert (await query_one("SELECT status FROM orders WHERE order_no='ORD-J1'"))["status"] == "待付款"
    assert (await query_one("SELECT status FROM refunds WHERE id = :i",
                            {"i": rid}))["status"] == "待审核"
