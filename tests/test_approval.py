"""订单人工审批（HITL）测试

- **PG 状态机**（2026-09 方案 B）：登记/读取/超时/并发抢占/幂等清理
- **重启不丢单**：模拟"内存状态全没、只剩数据库"时仍能审批并生成订单
- **静默假成功回归**：图 resume 没拿到订单号时，接口必须报错而不是返回 ok=true
- 合成 interrupt 图：验证本项目使用的 0.6.x 语义
  （invoke 返回 __interrupt__ → get_state 可见 → Command(resume=...) 恢复）
- build_graph 冒烟：编译带 checkpointer，thread 配置形状正确
（不触发真实 LLM）
"""
import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from src.approval import (
    claim, expire_overdue, find_pending_draft, get_pending, list_pending,
    pending_reply_text, register_pending, remove_pending,
)


# ── PG 状态机：登记 / 读取 / 幂等 ─────────────────────────

async def test_registry_roundtrip(pg_db):
    row = await register_pending("u1", "u1", {"product_name": "T400", "quantity": 200},
                                 args={"product_id": "P0075", "quantity": 200},
                                 session_id="s1")
    assert row["id"] and row["status"] == "pending"
    assert row["session_id"] == "s1"

    got = await get_pending("u1")
    assert got["draft"]["quantity"] == 200
    assert got["args"]["product_id"] == "P0075"      # 兜底直接写单要靠它
    items = await list_pending()
    assert items and items[0]["thread_id"] == "u1"
    assert await get_pending("nope") is None

    await remove_pending(row["id"])
    assert await get_pending("u1") is None


async def test_register_is_idempotent_per_thread(pg_db):
    """同一线程重复登记 → 只有一条 pending（后到的更新内容，不会堆单）。"""
    a = await register_pending("u2", "u2", {"quantity": 100})
    b = await register_pending("u2", "u2", {"quantity": 300})
    assert a["id"] == b["id"], "同一 thread 只应有一条 pending"
    assert (await get_pending("u2"))["draft"]["quantity"] == 300
    assert len(await list_pending()) == 1


async def test_resume_replay_reuses_decided_row(pg_db):
    """resume 重放（图会重跑 interrupt 前的代码）→ 复用刚判定的行，不产生幽灵待审批。"""
    row = await register_pending("u3", "u3", {"quantity": 100})
    claimed = await claim(row["id"], "admin", approved=True)
    assert claimed["ok"] is True

    again = await register_pending("u3", "u3", {"quantity": 100})   # 重放
    assert again["id"] == row["id"], "重放应复用原行"
    assert again["status"] == "approved"
    assert await list_pending() == [], "不该出现新的待审批"


# ── PG 状态机：并发抢占 / 超时 ─────────────────────────────

async def test_claim_is_cas_only_one_winner(pg_db):
    """两个管理员同时点"通过" → 只有一个拿到，另一个明确知道已被处理。"""
    row = await register_pending("u4", "u4", {"quantity": 100})
    first = await claim(row["id"], "admin_a", approved=True, reason="ok")
    second = await claim(row["id"], "admin_b", approved=True, reason="me too")
    assert first["ok"] is True and first["code"] == "claimed"
    assert second["ok"] is False and second["code"] == "already_decided"


async def test_expired_approval_cannot_be_claimed(pg_db):
    """超过审批时限 → 不能批、也不再出现在待审批列表（幽灵订单防护）。"""
    from src.db import execute
    row = await register_pending("u5", "u5", {"quantity": 100})
    await execute("UPDATE pending_approvals SET expires_at = now() - interval '1 minute' "
                  "WHERE id = :id", {"id": row["id"]})

    assert await get_pending("u5") is None
    assert await list_pending() == []
    assert await expire_overdue() >= 1
    result = await claim(row["id"], "admin", approved=True)
    assert result["ok"] is False and result["code"] == "expired"


async def test_remove_pending_only_touches_pending(pg_db):
    """按 id 清理只删仍 pending 的行；已判定的行保留为审计记录。"""
    decided = await register_pending("u6", "u6", {"quantity": 1})
    await claim(decided["id"], "admin", approved=True)
    await remove_pending(decided["id"])
    from src.approval import get_by_id
    assert (await get_by_id(decided["id"]))["status"] == "approved", "已判定的行不该被删"

    fresh = await register_pending("u7", "u7", {"quantity": 2})
    await remove_pending(fresh["id"])
    assert await get_by_id(fresh["id"]) is None


def test_find_pending_draft():
    class Fake:
        def __init__(self, v):
            self.value = v

    assert find_pending_draft(None) is None
    assert find_pending_draft([Fake({"type": "other"})]) is None
    draft = find_pending_draft([
        Fake({"type": "other"}),
        {"type": "order_approval", "draft": {"qty": 5}},
    ])
    assert draft == {"qty": 5}


def test_pending_reply_text():
    text = pending_reply_text({
        "product_name": "T400", "product_id": "P0075", "color": "黑色",
        "quantity": 200, "unit_price": "12.7", "total": "2540.0",
        "phone": "138", "address": "杭州", "delivery_date": "下周",
    })
    assert "已提交人工审批" in text
    assert "T400" in text and "2540.0" in text


# ── 合成 interrupt 图（语义与本项目下单路径一致）────────

def _make_hitl_graph():
    class S(dict):
        msgs: list

    def node_appr(s):
        decision = interrupt({"type": "order_approval", "draft": {"qty": 100}})
        return {"msgs": s["msgs"] + [f"decided={decision}"]}

    g = StateGraph(S)
    g.add_node("appr", node_appr)
    g.set_entry_point("appr")
    g.add_edge("appr", END)
    return g.compile(checkpointer=MemorySaver())


async def test_hitl_invoke_interrupt_then_resume():
    graph = _make_hitl_graph()
    cfg = {"configurable": {"thread_id": "t-hitl"}}

    # 首次：invoke 返回 partial state，带 __interrupt__，不抛异常
    first = await graph.ainvoke({"msgs": []}, cfg)
    draft = find_pending_draft(first.get("__interrupt__"))
    assert draft == {"qty": 100}

    # 挂起态可见
    snap = graph.get_state(cfg)
    assert snap.next  # 有节点在等 resume
    assert find_pending_draft(snap.interrupts) == {"qty": 100}

    # 恢复：Command(resume=...) 走完
    resume = await graph.ainvoke(Command(resume={"approved": True}), cfg)
    assert resume["msgs"] == ["decided={'approved': True}"]
    assert not graph.get_state(cfg).next  # 已结束


async def test_hitl_reject_path():
    graph = _make_hitl_graph()
    cfg = {"configurable": {"thread_id": "t-hitl-rej"}}
    await graph.ainvoke({"msgs": []}, cfg)
    resume = await graph.ainvoke(Command(resume={"approved": False, "reason": "库存不足"}), cfg)
    assert resume["msgs"] == ["decided={'approved': False, 'reason': '库存不足'}"]


# ── build_graph 冒烟（不触发 LLM）────────────────────────

def test_build_graph_compiles_with_checkpointer():
    from src.agent import build_graph, thread_config
    graph = build_graph()
    snap = graph.get_state(thread_config("smoke_user"))
    assert graph is not None
    assert not snap.next  # 新线程无中断

# ══════════════════════════════════════════════════════════════
# 重启不丢单（本次修复的核心验收）
# ══════════════════════════════════════════════════════════════

class _FakeMCP:
    """假的 MCP 客户端：记录调用并返回成功文案（含订单号）。"""

    def __init__(self, reply="✅ 订单已生成！\n订单号：ORD-20260914-1234567890123\n"):
        self.reply, self.calls = reply, []

    async def call_tool(self, name, args):
        self.calls.append((name, dict(args)))
        return self.reply


async def _fake_no_checkpoint(*args, **kwargs):
    """模拟"服务重启过"：图线程没有任何 checkpoint → resume 必然失败。"""
    raise RuntimeError("no checkpoint for thread (simulated restart)")


async def test_approve_after_restart_still_creates_order(pg_db, monkeypatch):
    """**核心验收**：挂起后服务重启（内存状态全丢）→ 管理员照样能批 → 订单真的生成。

    这就是修复前必丢单的场景：客户收到"已提交人工审批"，管理员列表空了，
    订单永远不生成且无痕迹。现在列表与状态都在 PG，兜底路径直接用登记的参数写单。
    """
    import app as appmod

    row = await register_pending("u_restart", "u_restart", {"product_name": "T400", "quantity": 500},
                                 args={"customer_id": "u_restart", "product_id": "P0075",
                                       "product_name": "T400", "color": "黑色",
                                       "quantity": 500, "unit_price": 16.5},
                                 session_id="sess-r")
    fake = _FakeMCP()
    monkeypatch.setattr(appmod, "agent_graph", type("G", (), {"ainvoke": _fake_no_checkpoint})())
    monkeypatch.setattr(appmod, "get_mcp", lambda: fake)

    result = await appmod._decide_approval(row["id"], "", approved=True, actor="admin")

    assert result["ok"] is True, result
    assert result["order_no"].startswith("ORD-"), "必须真拿到订单号才算成功"
    assert result["resumed"] is False, "走的是兜底直接写单（图状态已丢）"
    assert fake.calls and fake.calls[0][0] == "create_order"
    assert fake.calls[0][1]["client_request_id"] == row["id"], "兜底写单必须带幂等键"

    # 记账：审计上能看到订单号，且这单不再挂在待审批列表里
    from src.approval import get_by_id
    saved = await get_by_id(row["id"])
    assert saved["status"] == "approved" and saved["order_no"] == result["order_no"]
    assert await list_pending() == []


async def test_approve_not_silently_successful_when_order_missing(pg_db, monkeypatch):
    """**静默假成功回归**：拿不到订单号就必须如实报错，而不是 ok=true 却没订单。

    修复前把"图没抛异常"当成功：注册表在、checkpoint 没了时，图会从头重跑再挂起，
    ainvoke 正常返回、reply 为空 → 接口回 ok=true，订单根本没写，谁也发现不了。
    """
    import app as appmod

    row = await register_pending("u_silent", "u_silent", {"quantity": 100},
                                 args={"customer_id": "u_silent", "product_id": "P1",
                                       "product_name": "T400", "color": "黑",
                                       "quantity": 100, "unit_price": 10})
    fake = _FakeMCP(reply="订单生成失败，请稍后重试。")     # 兜底也没拿到订单号
    monkeypatch.setattr(appmod, "agent_graph", type("G", (), {"ainvoke": _fake_no_checkpoint})())
    monkeypatch.setattr(appmod, "get_mcp", lambda: fake)

    result = await appmod._decide_approval(row["id"], "", approved=True, actor="admin")

    assert result["ok"] is False
    assert result["code"] == "order_not_created"
    from src.approval import get_by_id
    saved = await get_by_id(row["id"])
    assert saved["order_no"] in (None, ""), "没出单就不该记订单号"


async def test_approve_prefers_graph_resume_when_available(pg_db, monkeypatch):
    """图状态还在时优先走 resume（保留客户续轮上下文），且不会重复调用写单兜底。"""
    import app as appmod
    from langchain_core.messages import AIMessage

    row = await register_pending("u_resume", "u_resume", {"quantity": 100},
                                 args={"product_id": "P1"})
    fake = _FakeMCP()

    async def _resume(*args, **kwargs):
        return {"messages": [AIMessage(content="已下单 订单号 ORD-20260914-9999999999999")]}

    monkeypatch.setattr(appmod, "agent_graph", type("G", (), {"ainvoke": _resume})())
    monkeypatch.setattr(appmod, "get_mcp", lambda: fake)

    result = await appmod._decide_approval(row["id"], "", approved=True, actor="admin")
    assert result["ok"] is True and result["resumed"] is True
    assert fake.calls == [], "图已经出了订单号，不该再走兜底写单（否则会重复下单）"


async def test_second_approval_is_rejected(pg_db, monkeypatch):
    """并发/重复审批：第二个请求收到明确的"已被处理"，不会重复下单。"""
    import app as appmod

    row = await register_pending("u_race", "u_race", {"quantity": 1}, args={"product_id": "P1"})
    fake = _FakeMCP()
    monkeypatch.setattr(appmod, "agent_graph", type("G", (), {"ainvoke": _fake_no_checkpoint})())
    monkeypatch.setattr(appmod, "get_mcp", lambda: fake)

    first = await appmod._decide_approval(row["id"], "", approved=True, actor="admin_a")
    second = await appmod._decide_approval(row["id"], "", approved=True, actor="admin_b")
    assert first["ok"] is True
    assert second["ok"] is False and second["code"] == "already_decided"
    assert len(fake.calls) == 1, "只有一次真正写单"


async def test_chat_guard_uses_db_after_restart(pg_db):
    """客户在重启后再发消息：守卫读 PG，照样提示"有一单在审批中"（不再重新跑图）。

    修复前守卫依赖图 checkpoint，重启后 `snap.next` 为空 → 守卫失效 →
    客户的话会让图从头再跑一遍，可能重新生成一份 draft（把丢失悄悄掩盖掉）。
    """
    import httpx

    import app as appmod
    from src.auth import create_token

    row = await register_pending("guard_user", "guard_user",
                                 {"product_name": "T400", "quantity": 500})
    tok = create_token("guard_user", role="customer")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=appmod.app),
                                 base_url="http://test") as c:
        r = await c.post("/chat", headers={"Authorization": f"Bearer {tok}"},
                         json={"message": "我的单怎么样了？"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("pending") is True
    assert body.get("approval_id") == row["id"]
    assert "已提交人工审批" in body["reply"]


# ══════════════════════════════════════════════════════════════
# 写单幂等（MCP 工具层）：同一次审批重试不会生成第二笔订单
# ══════════════════════════════════════════════════════════════

async def test_create_order_is_idempotent_with_client_request_id(pg_db):
    """同一个 client_request_id 调两次 → 返回同一笔订单，库里也只有一行。

    为什么需要它：审批兜底路径可能重试（进程在"写单"与"记账"之间挂掉、网络重发），
    没有幂等键就会变成重复下单。
    """
    from src.db import query_all
    from src.mcp_servers.order_server import create_order

    args = dict(customer_id="u_idem", product_id="P001", product_name="T400",
                color="黑色", quantity=100, unit_price=16.5)
    first = await create_order(**args, client_request_id="appr-1")
    second = await create_order(**args, client_request_id="appr-1")

    assert "✅ 订单已生成" in first and "✅ 订单已生成" in second
    rows = await query_all("SELECT order_no, client_request_id FROM orders "
                           "WHERE client_request_id = 'appr-1'")
    assert len(rows) == 1, f"同一请求 id 只能有一笔订单，实际 {len(rows)}"
    assert rows[0]["order_no"] in first and rows[0]["order_no"] in second

    # 换个请求 id（真的是新的一单）→ 正常再下一笔
    third = await create_order(**args, client_request_id="appr-2")
    assert "✅ 订单已生成" in third and rows[0]["order_no"] not in third
