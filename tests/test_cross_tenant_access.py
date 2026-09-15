"""跨租户越权（IDOR）回归测试（2026-09）
==========================================
这一组用例守着一条**真实可复现**的越权链：订单级工具的 `order_no` 是 LLM 从对话里拿的，
而工具自己只按订单号查库。改之前，一个普通客户账号就能：

| 攻击 | 改之前 | 现在 |
|---|---|---|
| 「查一下订单 ORD-别人的单号」 | 返回**别人的电话/地址** | 拒绝（且与"不存在"同一句话，不泄露存在性） |
| 「退掉订单 ORD-别人的单号」 | 在别人的单上建工单，并把订单推进「退款中」（跨租户写 + 冻结） | 拒绝 |
| 提示词注入让下单带上别人的 `customer_id` | 订单**记在别人名下**（且延迟到审批后才发生） | 被服务端注入的可信身份覆盖 |

根因是"身份只写在提示词里"（`ORDER_AGENT_PROMPT` 那句"customer_id 必须填系统提供的值"
是建议不是约束），所以修法也是两层缺一不可：
① `src/order_access.py` 在调用工具前**覆盖**注入可信身份（不是 `setdefault`）；
② MCP 工具按 `order_no + customer_id` 一起查，缺 `caller_id` 直接 fail-closed。

为什么"注入被覆盖"这条最重要：只做工具侧校验时，`caller_id` 本身就是 LLM args 里的值，
攻击者把它填成受害者的 id 就能绕过 —— 所以必须证明"LLM 塞进来的身份一定被冲掉"。
"""
import os

os.environ.setdefault("DEV_MODE", "1")
os.environ.setdefault("JWT_SECRET", "test-secret-only-0123456789abcdef0123456789abcdef")

import pytest  # noqa: E402

from src.db import execute, query_one  # noqa: E402
from src.order_access import with_trusted_identity  # noqa: E402
from src.users import ensure_user_row  # noqa: E402

ATTACKER = "attacker_user"
VICTIM = "victim_user"


async def _seed_order(order_no: str, customer_id: str, status: str = "已发货") -> None:
    await ensure_user_row(customer_id, customer_id)
    await execute(
        """INSERT INTO orders (order_no, customer_id, product_id, product_name, color,
                               quantity, unit_price, total, status, created_at,
                               phone, address, delivery_date)
           VALUES (:no, :cid, 'P001', 'T400 复合弹力布', '黑色', 100, 13.2, 1320, :st,
                   '2026-09-01T10:00:00', '13900000000', '杭州市余杭区', '7 天内')""",
        {"no": order_no, "cid": customer_id, "st": status},
    )


# ══════════════════════════════════════════════════════════════
# ① 身份注入：LLM 给的身份一律被覆盖
# ══════════════════════════════════════════════════════════════

def test_identity_is_overridden_not_merged():
    """身份注入必须**覆盖**：LLM（或攻击者）在 args 里塞的身份值要被冲掉。

    踩过的坑：审批兜底路径用了 `args.setdefault("customer_id", 可信值)` ——
    本意是"LLM 漏传时补齐"，实际是"LLM 传了就保留"，攻击者填的值反而赢。
    """
    malicious = {"order_no": "ORD-X", "caller_id": VICTIM}
    out = with_trusted_identity("query_order", malicious, ATTACKER)
    assert out["caller_id"] == ATTACKER, "必须覆盖成服务端可信身份"
    assert malicious["caller_id"] == VICTIM, "不能就地改坏调用方的 dict"

    # 写工具用 customer_id 这个键，同样覆盖
    out2 = with_trusted_identity("create_order", {"customer_id": VICTIM, "quantity": 10}, ATTACKER)
    assert out2["customer_id"] == ATTACKER

    # 与订单无关的工具不动参数
    out3 = with_trusted_identity("search_product", {"query": "T400"}, ATTACKER)
    assert out3 == {"query": "T400"}


def test_missing_identity_is_fail_closed():
    """没有可信身份（未登录/guest）时，不能拿 LLM 自报的身份去查别人的单。"""
    out = with_trusted_identity("query_order", {"order_no": "ORD-X", "caller_id": VICTIM}, "")
    assert "caller_id" not in out, "身份为空时必须清掉身份类参数，让工具 fail-closed 拒绝"


# ══════════════════════════════════════════════════════════════
# ② 读：别人的订单查不出内容
# ══════════════════════════════════════════════════════════════

async def test_cannot_read_other_customers_pii(pg_db):
    """`refund_server.query_order` 会返回电话/地址 —— 改之前任何调用者都能读。"""
    await _seed_order("ORD-VICTIM-1", VICTIM)
    from src.mcp_servers.refund_server import query_order

    leaked = await query_order("ORD-VICTIM-1", caller_id=ATTACKER)
    assert "13900000000" not in leaked and "杭州市余杭区" not in leaked
    assert "不属于您" in leaked

    assert "13900000000" not in await query_order("ORD-VICTIM-1")   # 无身份
    mine = await query_order("ORD-VICTIM-1", caller_id=VICTIM)
    assert "13900000000" in mine, "本人查询仍然正常"


async def test_cannot_read_other_order_status(pg_db):
    await _seed_order("ORD-VICTIM-2", VICTIM)
    from src.mcp_servers.order_server import query_order_status
    assert "不属于您" in await query_order_status("ORD-VICTIM-2", caller_id=ATTACKER)
    assert "T400" in await query_order_status("ORD-VICTIM-2", caller_id=VICTIM)


# ══════════════════════════════════════════════════════════════
# ③ 写：别人的订单不能被推进退款流程
# ══════════════════════════════════════════════════════════════

async def test_cannot_freeze_other_customers_order(pg_db):
    """最严重的一条：在别人的订单上建退款工单 → 该订单被推进「退款中」（跨租户写）。"""
    await _seed_order("ORD-VICTIM-3", VICTIM)
    from src.mcp_servers.refund_server import create_refund

    out = await create_refund("ORD-VICTIM-3", "我不想要了", caller_id=ATTACKER)
    assert "不属于您" in out
    assert (await query_one("SELECT status FROM orders WHERE order_no='ORD-VICTIM-3'"))["status"] \
        == "已发货", "受害者的订单状态不能被别人改动"
    assert await query_one("SELECT count(*) AS n FROM refunds") == {"n": 0}, "不该留下工单"

    # 本人申请则正常（联动照旧生效）
    ok = await create_refund("ORD-VICTIM-3", "色差", caller_id=VICTIM)
    assert "✅ 退款工单已生成" in ok
    assert (await query_one("SELECT status FROM orders WHERE order_no='ORD-VICTIM-3'"))["status"] \
        == "退款中"


async def test_refund_without_identity_is_refused(pg_db):
    await _seed_order("ORD-VICTIM-4", VICTIM)
    from src.mcp_servers.refund_server import create_refund
    assert "无法确认您的身份" in await create_refund("ORD-VICTIM-4", "无身份")
    assert await query_one("SELECT count(*) AS n FROM refunds") == {"n": 0}


# ══════════════════════════════════════════════════════════════
# ④ 下单身份：待审批表里存的 args 也必须是可信身份
# ══════════════════════════════════════════════════════════════

async def test_pending_approval_does_not_store_llm_identity(pg_db):
    """待审批表里存的 `args` 会被审批兜底路径直接拿去写单 —— 所以存的必须是**可信身份**。

    否则越权不会消失，只是从"下单时"推迟到"管理员点通过时"发生（更难追查）。
    """
    from src.approval import register_pending

    llm_args = {"product_id": "P001", "product_name": "T400", "color": "黑",
                "quantity": 10, "unit_price": 13.2, "customer_id": VICTIM}
    safe = with_trusted_identity("create_order", llm_args, ATTACKER)
    pending = await register_pending(ATTACKER, ATTACKER, {"product_name": "T400"}, args=safe)

    row = await query_one("SELECT args, user_id FROM pending_approvals WHERE id = :i",
                          {"i": pending["id"]})
    assert row["args"]["customer_id"] == ATTACKER, "存进待审批表的身份必须是可信身份"
    assert row["user_id"] == ATTACKER
