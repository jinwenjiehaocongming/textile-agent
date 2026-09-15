"""退单联动（订单状态 ↔ 退款工单）测试（2026-09）
==================================================
改之前的故障：退款工单和订单状态**互不相干** —— 售后 Agent 建工单只写 refunds 表，
管理端审核只改工单自己的 status。实测后果：19 张"已通过"的工单，对应订单一动不动，
¥197,500 的退款在系统里查不到；分析模块的"净成交额 = GMV − 已退款"没有数据可减。

本文件把三条联动规则逐条钉死：
1. 建单 → 订单「退款中」+ 记下退款前状态；
2. 通过 → 订单「已退款」（**退款前是待付款则「已取消」**：没付过钱的不叫退款）；
3. 驳回 → 退回退款前状态（**有其他待审工单时不退**：流程还在进行）。

外加几条"别把数据改坏"的护栏：重复建单不覆盖原始状态、历史工单没记 before 时不瞎猜、
订单已终态时只登记工单不动订单、并发审核只有一个生效。
"""
import os

os.environ["DEV_MODE"] = "1"
os.environ["JWT_SECRET"] = "test-secret-only-0123456789abcdef0123456789abcdef"

import httpx  # noqa: E402
import pytest  # noqa: E402

from src.auth import create_token  # noqa: E402
from src.db import execute, query_all, query_one  # noqa: E402
from src.order_flow import (  # noqa: E402
    OrderActionError, apply_refund_decision, check_refund_invariants, create_refund_ticket,
)
from src.users import ensure_user_row  # noqa: E402
from app import app  # noqa: E402

ADMIN = "boss_admin"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {create_token(ADMIN, role='admin')}"}


async def _seed_order(order_no: str = "ORD-RF1", status: str = "已发货",
                      total: float = 1000.0, customer_id: str = "cust_rf") -> None:
    await ensure_user_row(customer_id, "退款客户")
    await execute(
        """INSERT INTO orders (order_no, customer_id, product_id, product_name, color,
                               quantity, unit_price, total, status, created_at,
                               phone, address, delivery_date)
           VALUES (:no, :cid, 'P001', 'T400 复合弹力布', '黑色', 100, 10.0, :total, :st,
                   '2026-09-01T10:00:00', '13800000000', '浙江省绍兴市', '2026-09-20')""",
        {"no": order_no, "cid": customer_id, "total": total, "st": status},
    )


async def _order_status(order_no: str) -> dict:
    return await query_one("SELECT status, paid_at, shipped_at, refunded_at FROM orders "
                           "WHERE order_no = :no", {"no": order_no})


# ══════════════════════════════════════════════════════════════
# 规则 1：建单 → 订单进「退款中」+ 记下退款前状态
# ══════════════════════════════════════════════════════════════

async def test_ticket_creation_moves_order_to_refunding(pg_db):
    """建工单必须把订单标记为「退款中」，并记下**退款前**的状态（驳回要原样退回）。"""
    await _seed_order("ORD-RF1", status="已发货")
    out = await create_refund_ticket("ORD-RF1", "纬斜超标")
    assert out["ok"] and out["order_status"] == "退款中"
    assert out["order_status_before"] == "已发货"

    row = await _order_status("ORD-RF1")
    assert row["status"] == "退款中"
    ticket = await query_one("SELECT status, order_status_before FROM refunds WHERE id = :i",
                             {"i": out["ticket_id"]})
    assert ticket["status"] == "待审核" and ticket["order_status_before"] == "已发货"
    # 审计要能看出"谁把订单推进了退款中"
    logs = await query_all("SELECT detail FROM audit_log WHERE thread_id = 'ORD-RF1' "
                           "AND action = 'order_status_change'")
    assert any("已发货 → 退款中" in x["detail"] for x in logs)


async def test_second_ticket_keeps_original_before_status(pg_db):
    """重复建单不能把「退款中」记成退回目标 —— 否则驳回后订单卡死在退款中。"""
    await _seed_order("ORD-RF2", status="已付款")
    await create_refund_ticket("ORD-RF2", "第一次申请")
    out2 = await create_refund_ticket("ORD-RF2", "又来一次")

    assert out2["moved"] is False, "已经在退款中，不该再流转一次"
    assert out2["order_status_before"] == "已付款", "必须沿用最早的退款前状态"
    assert (await _order_status("ORD-RF2"))["status"] == "退款中"


async def test_ticket_on_terminal_order_only_registers(pg_db):
    """订单已是终态（已退款/已取消）→ 工单照建（留痕），但订单不动，并如实说明。"""
    await _seed_order("ORD-RF3", status="已退款")
    out = await create_refund_ticket("ORD-RF3", "再退一次")
    assert out["ok"] is False and "已退款" in out["reason"]
    assert (await _order_status("ORD-RF3"))["status"] == "已退款"
    assert await query_one("SELECT count(*) AS n FROM refunds WHERE order_no = 'ORD-RF3'") == {"n": 1}


async def test_ticket_for_missing_order_404(pg_db):
    with pytest.raises(OrderActionError) as e:
        await create_refund_ticket("ORD-NOPE", "不存在")
    assert e.value.status == 404


# ══════════════════════════════════════════════════════════════
# 规则 2：审核通过 → 已退款（未付款则已取消）
# ══════════════════════════════════════════════════════════════

async def test_approve_refund_marks_order_refunded(pg_db):
    """通过退款 → 订单「已退款」+ `refunded_at`（退款时效分析靠它）。"""
    await _seed_order("ORD-RF4", status="已发货", total=8600.0)
    t = await create_refund_ticket("ORD-RF4", "色差超标")

    out = await apply_refund_decision(t["ticket_id"], True, ADMIN, note="同意退款")
    assert out["order_status"] == "已退款" and out["order_changed"] is True

    row = await _order_status("ORD-RF4")
    assert row["status"] == "已退款" and row["refunded_at"], "退款必须落 refunded_at"
    log = await query_one("SELECT detail FROM audit_log WHERE action = 'refund_decision' "
                          "AND thread_id = 'ORD-RF4'")
    assert "已通过" in log["detail"] and "已退款" in log["detail"]


async def test_approve_on_unpaid_order_cancels_instead(pg_db):
    """退款前是「待付款」→ 通过后是「已取消」，不是「已退款」。

    没付过钱的订单没有钱可退；记成"已退款"会让财务对不上账（凭空多一笔退款流水）。
    """
    await _seed_order("ORD-RF5", status="待付款")
    t = await create_refund_ticket("ORD-RF5", "不想买了")
    out = await apply_refund_decision(t["ticket_id"], True, ADMIN)
    assert out["order_status"] == "已取消"
    row = await _order_status("ORD-RF5")
    assert row["status"] == "已取消"


async def test_approve_is_idempotent_when_order_already_refunded(pg_db):
    """订单已经是「已退款」（例如先前人工处理过）→ 审核不再报错，标记未变更。"""
    await _seed_order("ORD-RF6", status="已退款")
    # 手工造一张挂在已退款订单上的待审工单（模拟历史数据/人工干预）
    await execute("INSERT INTO refunds (order_no, reason, status, created_at, order_status_before) "
                  "VALUES ('ORD-RF6', '历史工单', '待审核', '2026-09-01T10:00:00', '已发货')")
    rid = (await query_one("SELECT max(id) AS i FROM refunds"))["i"]
    out = await apply_refund_decision(rid, True, ADMIN)
    assert out["order_changed"] is False and "已是" in out["order_note"]


# ══════════════════════════════════════════════════════════════
# 规则 3：审核驳回 → 退回退款前状态（有别的待审工单则不退）
# ══════════════════════════════════════════════════════════════

async def test_reject_restores_previous_status(pg_db):
    """驳回 = 撤销退款流程，订单**原样退回**（这里是 已发货）。"""
    await _seed_order("ORD-RF7", status="已发货")
    t = await create_refund_ticket("ORD-RF7", "理由不充分")
    out = await apply_refund_decision(t["ticket_id"], False, ADMIN, note="超过 7 天")
    assert out["order_status"] == "已发货" and out["order_changed"] is True
    assert (await _order_status("ORD-RF7"))["status"] == "已发货"
    assert "已驳回" in (await query_one(
        "SELECT detail FROM audit_log WHERE action = 'refund_decision' "
        "AND thread_id = 'ORD-RF7'"))["detail"]


async def test_reject_keeps_refunding_when_other_tickets_pending(pg_db):
    """同一订单还有别的待审工单 → 驳回这一张**不能**把订单放回原状态（流程仍在进行）。"""
    await _seed_order("ORD-RF8", status="已收货")
    t1 = await create_refund_ticket("ORD-RF8", "第一张")
    t2 = await create_refund_ticket("ORD-RF8", "第二张")
    out = await apply_refund_decision(t1["ticket_id"], False, ADMIN, note="驳回第一张")
    assert out["order_changed"] is False
    assert out["order_status"] == "退款中" and "还有 1 张" in out["order_note"]
    assert (await _order_status("ORD-RF8"))["status"] == "退款中"

    # 第二张也驳回 → 这时才退回原状态
    out2 = await apply_refund_decision(t2["ticket_id"], False, ADMIN)
    assert out2["order_status"] == "已收货"


async def test_reject_without_before_status_does_not_guess(pg_db):
    """历史工单没记退款前状态 → **不许猜**，订单留在退款中并显式说明，交人工处理。

    猜错的方向是随机的（"退回已付款？还是已发货？"），而订单状态是业务事实，
    宁可不改也不能改错。
    """
    await _seed_order("ORD-RF9", status="退款中")
    await execute("INSERT INTO refunds (order_no, reason, status, created_at, order_status_before) "
                  "VALUES ('ORD-RF9', '老工单', '待审核', '2026-09-01T10:00:00', NULL)")
    rid = (await query_one("SELECT max(id) AS i FROM refunds"))["i"]
    out = await apply_refund_decision(rid, False, ADMIN)
    assert out["order_changed"] is False and "未记录" in out["order_note"]
    assert (await _order_status("ORD-RF9"))["status"] == "退款中"


# ══════════════════════════════════════════════════════════════
# 并发与接口
# ══════════════════════════════════════════════════════════════

async def test_second_decision_is_rejected(pg_db):
    """CAS：同一张工单只能被审一次（两个管理员同时点是真实现场）。"""
    await _seed_order("ORD-RF10", status="已付款")
    t = await create_refund_ticket("ORD-RF10", "并发测试")
    await apply_refund_decision(t["ticket_id"], True, ADMIN)
    with pytest.raises(OrderActionError) as e:
        await apply_refund_decision(t["ticket_id"], False, ADMIN)
    assert "已被处理过" in e.value.detail
    assert (await _order_status("ORD-RF10"))["status"] == "已退款"


async def test_admin_endpoint_returns_linkage(pg_db, clean_auth_store):
    """接口层要把订单联动结果一起返回（前端提示里要能显示"订单已变为已退款"）。"""
    await _seed_order("ORD-RF11", status="已发货")
    t = await create_refund_ticket("ORD-RF11", "接口测试")
    async with _client() as c:
        r = await c.post(f"/admin/refunds/{t['ticket_id']}/decide",
                         json={"approve": True, "note": "同意"}, headers=_admin_headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["order_status"] == "已退款" and body["order_changed"] is True

        listed = (await c.get("/admin/refunds", params={"status": "已通过"},
                              headers=_admin_headers())).json()["refunds"]
        hit = next(x for x in listed if x["id"] == t["ticket_id"])
        assert hit["order_status"] == "已退款"
        assert hit["order_status_before"] == "已发货", "列表要带上退款前状态"

        summary = (await c.get("/admin/orders/summary", headers=_admin_headers())).json()
        assert summary["refunding"] == 0, "审完就不该再有退款中的单"


async def test_summary_counts_refunding_exposure(pg_db, clean_auth_store):
    """工作台要能看到"退款中的敞口"（钱还没退出去的单）。"""
    await _seed_order("ORD-RF12", status="已发货", total=5000.0)
    await _seed_order("ORD-RF13", status="已收货", total=2500.0, customer_id="cust_rf2")
    await create_refund_ticket("ORD-RF12", "退货")
    async with _client() as c:
        s = (await c.get("/admin/orders/summary", headers=_admin_headers())).json()
    assert s["refunding"] == 1
    assert float(s["refunding_amount"]) == pytest.approx(5000.0)


async def test_invariant_holds_through_the_whole_flow(pg_db):
    """不变量：**有「待审核」工单的订单必须处于「退款中」**。

    这条不变量是整套联动的地基 —— 破坏它就等于回到改之前的状态
    （客户以为在退款，系统里没有任何标记）。我自己手工回滚 E2E 误操作时
    正好造出过一次违反（订单退回原状态、工单留在待审核），所以专门钉一条。
    """
    await _seed_order("ORD-RF14", status="已发货")
    await _seed_order("ORD-RF15", status="已付款")
    t1 = await create_refund_ticket("ORD-RF14", "单据A")
    t2 = await create_refund_ticket("ORD-RF15", "单据B")
    assert await check_refund_invariants() == [], "建单后订单必须都在退款中"

    # 通过一张、驳回一张，全部处理完 → 不变量仍然成立
    await apply_refund_decision(t1["ticket_id"], True, ADMIN)
    await apply_refund_decision(t2["ticket_id"], False, ADMIN)
    assert await check_refund_invariants() == []
    assert (await _order_status("ORD-RF14"))["status"] == "已退款"
    assert (await _order_status("ORD-RF15"))["status"] == "已付款"

    # 终态订单上的留痕工单不算违反（工单只是记录，订单已经是终态）
    await _seed_order("ORD-RF16", status="已取消")
    await create_refund_ticket("ORD-RF16", "取消后的申请")
    assert await check_refund_invariants() == []
