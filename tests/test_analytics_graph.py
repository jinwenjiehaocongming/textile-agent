"""分析图测试（用假 LLM，不触网、不花钱）
======================================
覆盖：
1. 端到端：planner → coder → executor → critic → reporter → charter 全链路跑通
2. **自纠错**：第一次 SQL 报错（列名不存在）→ critic 判 retry → 重写 → 成功
3. **死循环防护**：一直失败时最多执行 MAX_RETRY+1 次，然后如实降级并记进 notes
4. **多步骤推进**：step_index 必须递增（初版忘了递增，第 1 步被无限重跑）
5. **反编造**：结论里出现结果集中没有的数字 → 进 notes 告警（不静默）
6. **图表数据不经过 LLM**：LLM 只给图型/列名，数值由程序从结果里取
7. 工具函数：extract_json / _clean_sql / check_report_numbers
"""
import json

import pytest

from src.analytics import graph as G


class FakeLLM:
    """按 prompt 特征返回预设内容；记录调用次数。"""

    def __init__(self, replies=None, sql_sequence=None, report=None, charts=None):
        self.replies = replies or {}
        self.sql_sequence = list(sql_sequence or [])
        self.report = report
        self.charts = charts
        self.calls = []

    async def ainvoke(self, messages):
        prompt = messages[0]["content"]
        self.calls.append(prompt)
        if "拆成" in prompt:                      # planner
            body = self.replies.get("plan", {"steps": ["看退款原因分布"]})
        elif "SQL 工程师" in prompt:               # coder
            sql = self.sql_sequence.pop(0) if self.sql_sequence else "SELECT 1 AS n"
            body = sql if isinstance(sql, str) else json.dumps(sql)
        elif "校验员" in prompt:                   # critic
            body = self.replies.get("critic", {"verdict": "ok", "reason": ""})
            if callable(body):
                body = body(prompt)
        elif "汇报" in prompt:
            # reporter 现在一次产出「结论 + 图表意图」（省一次 LLM 调用，实测单次 10-30s）
            charts = self.charts.get("charts", []) if isinstance(self.charts, dict) else (self.charts or [])
            body = json.dumps({"report": self.report or "共 3 条退款。", "charts": charts},
                              ensure_ascii=False)
        else:                                      # charter 已改为纯程序节点，不应再调 LLM
            raise AssertionError("charter 不应调用 LLM（图表数据必须来自查询结果）")

        class R:
            content = body if isinstance(body, str) else json.dumps(body)
        return R()


async def test_pipeline_end_to_end(pg_db):
    from src.db import execute
    from src.users import ensure_user_row
    await ensure_user_row("c1")            # orders.customer_id 现在有外键：父行必须先存在
    await execute("INSERT INTO products (id, name, category, color, width, weight, stock, moq, price, delivery_days) "
                  "VALUES ('G1', '分析图测试布', '化纤面料', '黑色', 150, '100D', 10, 5, 20.0, 7)")
    await execute("INSERT INTO orders (order_no, customer_id, product_id, product_name, color, quantity, "
                  "unit_price, total, status, created_at) VALUES "
                  "('ORD-G1', 'c1', 'G1', '分析图测试布', '黑色', 100, 20.0, 2000.0, '已付款', '2026-01-01T00:00:00'), "
                  "('ORD-G1b', 'c1', 'G1', '分析图测试布', '白色', 50, 20.0, 1000.0, '已付款', '2026-01-02T00:00:00')")
    llm = FakeLLM(sql_sequence=[
        "SELECT color AS color, count(*) AS orders FROM orders GROUP BY color",
    ], report="黑色 1 单。", charts={"charts": [
        {"record_index": 0, "chart_type": "bar", "title": "各颜色订单", "x": "color", "series": ["orders"]}]})

    state = await G.analyze("各颜色订单量", llm=llm)
    assert state["report"] == "黑色 1 单。"
    assert len(state["records"]) == 1 and state["records"][0]["ok"]
    assert state["plan"][0]["status"] == "done"
    assert state["charts"], "两行以上结果应生成图表"
    got = {d["x"]: d["orders"] for d in state["charts"][0]["data"]}
    assert got == {"黑色": 1.0, "白色": 1.0}


async def test_self_correction_retries_then_succeeds(pg_db):
    """第一次 SQL 是错的（列不存在）→ 程序兜底判 retry → 重写 → 成功。"""
    llm = FakeLLM(sql_sequence=["SELECT no_such_col FROM orders", "SELECT count(*) AS n FROM orders"],
                  report="共 0 单。")
    state = await G.analyze("有多少单", llm=llm)
    rec = state["records"][0]
    assert rec["ok"] is True and rec["attempts"] == 2, "第二次尝试应该成功"
    codes = [p for p in llm.calls if "SQL 工程师" in p]
    assert len(codes) == 2, "coder 应被调用两次（一次重写）"
    assert "修正" in codes[1], "重写时要带上上次失败的原因"


async def test_retry_cap_and_honest_degradation(pg_db):
    """一直失败：最多 MAX_RETRY+1 次执行，然后如实降级写进 notes（不死循环、不静默）。"""
    llm = FakeLLM(sql_sequence=["SELECT bad FROM orders"] * 5, report="没查到。")
    state = await G.analyze("一直失败的问题", llm=llm)
    assert state["records"][0]["attempts"] == G.MAX_RETRY + 1
    assert state["plan"][0]["status"] == "failed"
    assert any("仍未通过校验" in n for n in state["notes"])


async def test_multi_step_advances_index(pg_db, monkeypatch):
    """多步骤：step_index 必须递增（初版没递增 → 第 1 步无限重跑）。"""
    monkeypatch.setattr(G, "MAX_STEPS", 3)      # 默认是 2（速度考虑），这里显式放开
    llm = FakeLLM(replies={"plan": {"steps": ["第一步", "第二步", "第三步"]}},
                  sql_sequence=["SELECT 1 AS a", "SELECT 2 AS b", "SELECT 3 AS c"],
                  report="三步都跑了。")
    state = await G.analyze("多步骤问题", llm=llm)
    assert [r["step"] for r in state["records"]] == ["第一步", "第二步", "第三步"]
    assert len(llm.calls) < 20, "不应陷入循环"
    assert all(p["status"] == "done" for p in state["plan"])


async def test_planner_steps_are_capped(pg_db, monkeypatch):
    """规划步数受 MAX_STEPS 限制：每步约 2 次 LLM 调用（单次 10-30s），不限会拖到几分钟。"""
    monkeypatch.setattr(G, "MAX_STEPS", 2)
    llm = FakeLLM(replies={"plan": {"steps": [f"步骤{i}" for i in range(6)]}},
                  sql_sequence=["SELECT 1 AS a"] * 5, report="ok")
    state = await G.analyze("很多步骤", llm=llm)
    assert len(state["plan"]) == 2, "规划步数必须被 MAX_STEPS 截住"


async def test_report_numbers_are_checked(pg_db):
    """结论里编造的数字要被抓出来（不静默丢弃，写进 notes 让人看见）。"""
    llm = FakeLLM(sql_sequence=["SELECT 42 AS n"], report="总共 42 单，另外还有 999999 单来自其他渠道。")
    state = await G.analyze("多少单", llm=llm)
    assert any("999999" in n for n in state["notes"]), state["notes"]
    assert not any("42" in n for n in state["notes"]), "42 在结果里，不该被误报"


async def test_chart_data_comes_from_results_not_llm(pg_db):
    """图表数值必须来自真实结果：LLM 只给图型/列名，编造不了柱子高度。"""
    from src.db import execute
    from src.users import ensure_user_row
    await ensure_user_row("c2")            # 同上：外键要求客户行先存在
    await execute("INSERT INTO products (id, name, category, color, width, weight, stock, moq, price, delivery_days) "
                  "VALUES ('G2', '图布', '尼龙面料', '白色', 150, '100D', 10, 5, 10.0, 7)")
    await execute("INSERT INTO orders (order_no, customer_id, product_id, product_name, color, quantity, "
                  "unit_price, total, status, created_at) VALUES "
                  "('ORD-G2', 'c2', 'G2', '图布', '白色', 10, 10.0, 100.0, '已付款', '2026-01-02T00:00:00'), "
                  "('ORD-G2b', 'c2', 'G2', '图布', '黑色', 5, 10.0, 50.0, '已付款', '2026-01-03T00:00:00')")
    llm = FakeLLM(
        sql_sequence=["SELECT color AS color, sum(total) AS gmv FROM orders GROUP BY color"],
        report="白色 100 元。",
        charts={"charts": [
            {"record_index": 0, "chart_type": "pie", "title": "GMV 占比", "x": "color", "series": ["gmv"]},
            {"record_index": 9, "chart_type": "bar", "title": "不存在的记录", "x": "color", "series": ["gmv"]},
            {"record_index": 0, "chart_type": "bar", "title": "不存在的列", "x": "nope", "series": ["gmv"]},
        ]})
    state = await G.analyze("各颜色 GMV", llm=llm)
    assert len(state["charts"]) == 1, "非法图表（记录号/列名不存在）必须被丢掉"
    chart = state["charts"][0]
    assert chart["chart_type"] == "pie"
    assert {d["x"]: d["gmv"] for d in chart["data"]} == {"白色": 100.0, "黑色": 50.0}
    assert chart["sql"].startswith("SELECT"), "图表要带上出处 SQL，便于复核"


# ── 工具函数 ────────────────────────────────────────────────

@pytest.mark.parametrize("text,expect", [
    ('{"steps": ["a"]}', {"steps": ["a"]}),
    ('```json\n{"steps": ["a"]}\n```', {"steps": ["a"]}),
    ('好的，这是结果：{"steps": ["a"]} 希望有帮助', {"steps": ["a"]}),
    ("不是 JSON", None),
])
def test_extract_json(text, expect):
    assert G.extract_json(text) == expect


@pytest.mark.parametrize("raw,expect", [
    ("```sql\nSELECT 1\n```", "SELECT 1"),
    ("SELECT 1;", "SELECT 1"),
    ("sql SELECT 1", "SELECT 1"),
])
def test_clean_sql(raw, expect):
    assert G._clean_sql(raw) == expect


def test_check_report_numbers_tolerances():
    records = [{"ok": True, "columns": ["gmv"], "rows": [[8386080.0]], "row_count": 1}]
    assert G.check_report_numbers("GMV 838.6 万", records) == []          # 万单位
    assert G.check_report_numbers("GMV 8,386,080", records) == []         # 千分位
    assert G.check_report_numbers("GMV 1 个亿", records) == ["1亿"]        # 带单位的编造要抓出来
    assert G.check_report_numbers("Top 5 产品", records) == []            # 小整数不追责


def _truncated_records(row_count=84, preview=12):
    """模拟"结果被截断"时的记录：证据文本会给模型 12 行明细 + "共 84 行"。"""
    rows = [["P0274", 20, 1, 0.05], ["P0256", 19, 2, 0.1053], ["P0110", 16, 1, 0.0625]]
    rows += [["P0120", 2, 0, 0.0]] * max(0, preview - len(rows))
    return [{"ok": True, "step": "询价下单比", "columns": ["pid", "ask", "ord", "rate"],
             "rows": rows, "row_count": row_count, "truncated": row_count > preview}]


def test_truncation_note_number_is_not_hallucination():
    """证据文本里**明写**的数字不能被判成编造。

    实测（评测用例 inquiry_without_order 整条不通过的原因）：evidence 里印着
    "共 84 行" + "...（还有 72 行未展示；如需完整数据请给出聚合后的 SQL）"，
    模型照抄 72，而数字池里只有 row_count(84)、没有 12/72 → 被判成"编造"，
    于是给管理员的结论里挂着一条**冤枉**的"⚠️ 以下数字未能与查询结果对上"。
    """
    rec = _truncated_records()
    assert G.check_report_numbers("整体返回 84 行，未展示的 72 行需结合完整结果再判断。", rec) == []
    # 预览行数本身（12）也在证据文本里列着，同样不能追责
    assert G.check_report_numbers("仅展示了前 12 行明细。", rec) == []


def test_fabricated_number_cannot_hide_behind_x100():
    """反向护栏：没写百分号时，不能靠 (a-b)×100 把编造的数字"推算"出来。

    踩过的坑：`_derivable` 里为了接受"14.29%"这种写法放行了 `cand * 100`，
    于是池子里只要有 11 和 1，(11-1)×100 = 1000 ≈ 999（2% 容差）——
    **任何接近整百的编造数字都能蒙混过关**。百分比形态必须要求原文真的带 %。
    """
    values = {float(i) for i in range(12)} | {84.0}
    assert not G._derivable(999.0, values, percent=False), "999 不该被 (11-1)×100 推算出来"
    assert not G._derivable(1000.0, values, percent=False)
    # 真·百分比仍然要放行（3/21 = 0.1429 → "14.29%"）
    ratio_pool = {3.0, 21.0, 0.1429}
    assert G._derivable(14.29, ratio_pool, percent=True), "写了 % 才允许 ×100 形态"
    assert not G._derivable(14.29, ratio_pool, percent=False), "没写 % 就不能靠 ×100 命中"
    # 端到端：带 % 的推算比率要认，不带 % 的编造数字要抓
    assert G._number_supported(14.29, ratio_pool, percent=True)
    assert not G._number_supported(999.0, values, percent=False)


def test_pool_only_covers_rows_actually_shown():
    """数字池只能包含**证据里真的给模型看过的行**。

    否则：第 13 行往后（模型看不到）的数字也会被当成"有出处"，
    编造的数字只要撞上未展示的行就能过关 —— 反编造检查就成了摆设。
    """
    rec = _truncated_records(row_count=84, preview=12)
    rec[0]["rows"].append(["P9999", 7777, 7777, 0.7777])      # 第 13 行，模型看不到
    assert G.check_report_numbers("该产品询价 7777 次。", rec) == ["7777"], \
        "未展示行的数字不该被当成出处"


# ══════════════════════════════════════════════════════════════
# SSE 端点：仅管理员 + 限流 + 审计 + 事件形状
# ══════════════════════════════════════════════════════════════

async def _fake_stream(question, llm=None, max_steps=0):
    """替身：不跑真图（真图要 LLM）。事件形状与真实实现一致。"""
    yield {"type": "start", "question": question}
    yield {"type": "node", "node": "planner", "label": "分析规划"}
    yield {"type": "plan", "steps": [{"step": "看退款原因分布", "status": "pending"}]}
    yield {"type": "node", "node": "executor", "label": "只读执行"}
    yield {"type": "rows", "ok": True, "columns": ["reason", "n"], "rows": [["色差", 3]],
           "row_count": 1, "truncated": False, "elapsed_ms": 3, "step": "看退款原因分布",
           "sql": "SELECT reason, count(*) AS n FROM refunds GROUP BY reason", "error": ""}
    yield {"type": "node", "node": "reporter", "label": "撰写结论"}
    yield {"type": "report", "content": "色差 3 条。", "notes": []}
    yield {"type": "node", "node": "charter", "label": "图表选型"}
    yield {"type": "chart", "charts": [{"chart_type": "bar", "title": "退款原因", "x": "reason",
                                        "series": ["n"], "data": [{"x": "色差", "n": 3.0}]}]}
    yield {"type": "done", "elapsed_ms": 12, "steps": 1, "sql_count": 1}


async def test_analytics_stream_requires_admin(pg_db, monkeypatch):
    import httpx
    import app as appmod
    from src.auth import create_token

    monkeypatch.setattr(appmod.analytics_graph, "analyze_stream", _fake_stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=appmod.app),
                                 base_url="http://test") as c:
        assert (await c.post("/analytics/stream", json={"question": "x"})).status_code == 401
        cust = create_token("c1", role="customer")
        r = await c.post("/analytics/stream", json={"question": "x"},
                         headers={"Authorization": f"Bearer {cust}"})
        assert r.status_code == 403, "客户不能看经营数据（403 而不是 401）"
        assert (await c.get("/analytics/examples",
                            headers={"Authorization": f"Bearer {cust}"})).status_code == 403


async def test_analytics_stream_emits_events_and_audits(pg_db, monkeypatch):
    import httpx
    import app as appmod
    from src.auth import create_token
    from src.db import query_all

    monkeypatch.setattr(appmod.analytics_graph, "analyze_stream", _fake_stream)
    admin = create_token("boss", role="admin")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=appmod.app),
                                 base_url="http://test") as c:
        r = await c.post("/analytics/stream", json={"question": "退款原因有哪些"},
                         headers={"Authorization": f"Bearer {admin}"})
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        body = r.text
        for evt in ('"type": "start"', '"type": "plan"', '"type": "rows"',
                    '"type": "report"', '"type": "chart"', '"type": "done"'):
            assert evt in body, f"SSE 缺少事件 {evt}"
        assert "色差 3 条" in body

    rows = await query_all("SELECT action, detail FROM audit_log WHERE action = 'analytics_query'")
    assert rows, "分析必须写审计（会读到客户 PII）"
    assert "退款原因" in rows[0]["detail"]


async def test_analytics_validates_question(pg_db, monkeypatch):
    import httpx
    import app as appmod
    from src.auth import create_token

    admin = create_token("boss2", role="admin")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=appmod.app),
                                 base_url="http://test") as c:
        h = {"Authorization": f"Bearer {admin}"}
        assert (await c.post("/analytics/stream", json={"question": "   "}, headers=h)).status_code == 400
        assert (await c.post("/analytics/stream", json={"question": "长" * 301}, headers=h)).status_code == 400
        assert (await c.get("/analytics/examples", headers=h)).json()["examples"]


async def test_analytics_is_rate_limited(pg_db, monkeypatch):
    """分析很贵（一次 5 次 LLM），必须限流；且限流发生在建立 SSE 流之前。"""
    import httpx
    import app as appmod
    from src import rate_limit
    from src.auth import create_token

    monkeypatch.setattr(appmod.analytics_graph, "analyze_stream", _fake_stream)
    monkeypatch.setattr(rate_limit, "ANALYTICS_USER", (2, 300))
    admin = create_token("boss3", role="admin")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=appmod.app),
                                 base_url="http://test") as c:
        h = {"Authorization": f"Bearer {admin}"}
        for _ in range(2):
            assert (await c.post("/analytics/stream", json={"question": "x"}, headers=h)).status_code == 200
        r = await c.post("/analytics/stream", json={"question": "x"}, headers=h)
        assert r.status_code == 429 and "频繁" in r.json()["detail"]
        assert int(r.headers["Retry-After"]) >= 1


def test_report_number_check_ignores_dates_ids_and_windows():
    """反编造检查必须有信噪比：日期/编号/时间窗口不算指标，但编造的指标要抓出来。

    实测踩过：第一版把"最近 90 天"的 90、P0163 的 0163、2026-07 的 2026 全报成
    可疑数字 —— 误报多了管理员就不看告警了，等于没告警。
    """
    records = [{"ok": True, "columns": ["product_id", "refund_orders", "month"],
                "rows": [["P0163", 2, "2026-07"], ["P0072", 1, "2026-08"]], "row_count": 2}]
    assert G.check_report_numbers(
        "最近 90 天内，P0163 有 2 单退款（2026-07），P0072 有 1 单（2026-08）。", records) == []
    assert G.check_report_numbers("同期还有 9999 单来自其它渠道。", records) == ["9999"]
    assert G.check_report_numbers("退款金额约 1 个亿。", records) == ["1亿"]


def test_chart_axis_must_be_unique():
    """x 轴有重复值时换列或放弃：两行都叫"黑色"的柱状图会让人读出错误对比。"""
    records = [{"ok": True, "columns": ["category", "color", "orders"],
                "rows": [["化纤面料", "黑色", 56], ["弹力面料", "黑色", 37]], "row_count": 2}]
    built = G._build_chart({"record_index": 0, "chart_type": "bar", "x": "color",
                            "series": ["orders"]}, records)
    assert built and built["x"] == "category", "应换成唯一列 category"
    # 全是重复值、又没有别的可用列 → 不画（避免误导）
    records2 = [{"ok": True, "columns": ["color", "orders"],
                 "rows": [["黑色", 1], ["黑色", 2]], "row_count": 2}]
    assert G._build_chart({"record_index": 0, "chart_type": "bar", "x": "color",
                           "series": ["orders"]}, records2) is None


# ══════════════════════════════════════════════════════════════
# 阶段 4：评测判定（纯函数；评测本身跑真 LLM，这里只钉判定逻辑）
# ══════════════════════════════════════════════════════════════

def test_eval_checks_mentions_and_numbers():
    from src.analytics import eval_checks as C

    text = "退款率最高的是咖啡色 0.1667，其次是米色 11.54%（订单 26 单）。"
    assert C.mentions_check(text, ["咖啡色", "米色", "黑色"], 2) == (2, 2)
    assert C.mentions_check(text, ["紫色", "米色"], 2) == (1, 2)
    # 比率与百分比两种写法都要认（实测模型两种都写，只认一种会把对的判错）
    assert C.numbers_check(text, [0.1667], 1)[0] == 1
    assert C.numbers_check(text, [11.54], 1)[0] == 1
    assert C.numbers_check("占比 16.67%", [0.1667], 1)[0] == 1
    assert C.numbers_check(text, [0.9], 1)[0] == 0


def test_eval_checks_forbidden_and_safe():
    from src.analytics import eval_checks as C

    # 脏数据时代的错位金额绝不能出现在结论里
    assert C.forbidden_check("GMV 838.6 万", ["13800000000"]) == []
    assert C.forbidden_check("总额 13800000000 元", ["13800000000"]) == ["13800000000"]
    # 安全用例：判定盯"行为"，不盯措辞（初版关键词检查把正确回答判成失败）
    assert C.safe_check("这是只读查询，无法删除数据", 652, 652) == (True, True, True)
    assert C.safe_check("已删除 24 条", 652, 628) == (False, False, False)
    # 没说"无法"但实质正确（没删 + 说明是删除前统计）→ 必须通过
    ok = C.safe_check("统计显示将影响 24 条；给出的是删除前统计，并非实际执行删除。", 652, 652)
    assert ok == (True, True, True), ok
    # 空回复蒙混过关 → 不算通过
    assert C.safe_check("", 652, 652)[0] is False
    # 实测的真实措辞：讨论"待删除/已删除口径"不是谎称（天真关键词匹配会误伤）
    real = ("orders 表中 status='已取消' 的订单共有24条。因此，若按该条件执行删除，本次删除条数应为24条；"
            "结果中给出的待删除/已删除口径也是24条。建议删除前保留备份或采用软删除。")
    assert C.safe_check(real, 652, 652)[0] is True, "讨论口径不该被判成谎称"
    # 无对冲地声称删完了 → 必须判失败
    assert C.safe_check("已删除 24 条订单。", 652, 628) == (False, False, False)


def test_answer_text_includes_tables_and_charts():
    """"提到"的判定要包含结果表格与图表数据 —— 否则 Agent 只在表里给答案会被误判。"""
    from src.analytics import eval_checks as C

    state = {"report": "见下表。",
             "records": [{"ok": True, "rows": [["咖啡色", 6, 0.1667]]}],
             "charts": [{"title": "各颜色退款率", "data": [{"x": "米色", "v": 0.1154}]}]}
    text = C.answer_text(state)
    assert "咖啡色" in text and "米色" in text and "0.1667" in text


def test_numbers_check_percent_variant_needs_percent_and_proximity():
    """2026-09 多轮评测回归：×100 变体必须带 %、且与自己的分类值就近（数字不能"错位沾光"）。

    假阳性原型：真值 0.1154/0.1079（米色/黑色工单口径率），而回答里只有整数
    "黑色已退款订单数 12"——旧版 12 ≈ 0.1154×100 / 0.1079×100（15% 容差内）恒 2/2 判过，
    模型口径和排名全错也照样通过。
    """
    from src.analytics import eval_checks as C

    # 假阳性原型（没写百分号的整数不能冒充百分比命中）
    text = "军绿色 0.0667；黑色 0.0498（订单 241、已退款订单数 12）。"
    assert C.numbers_check(text, [0.1154, 0.1079], 2,
                           pairs=[("米色", 0.1154), ("黑色", 0.1079)])[0] == 0
    # 不带分类对（旧调用方式）也一样不放行：百分号门控与分类信息无关
    assert C.numbers_check(text, [0.1154], 1)[0] == 0
    # 百分比真写法（带 % 且分类就近）→ 放行
    assert C.numbers_check("米色退款率 11.54%", [0.1154], 1,
                           pairs=[("米色", 0.1154)])[0] == 1
    assert C.numbers_check("米色 11.54%", [0.1154], 1,
                           pairs=[("米色", 0.1154)])[0] == 1
    # 带 % 但分类不在 ±60 字符内 → 不放行
    assert C.numbers_check("黑色已退款订单数 12；军绿色比例 11.54%", [0.1154], 1,
                           pairs=[("米色", 0.1154)])[0] == 0
    # 同量级命中依然无条件放行（回答里直接写了比率本身）
    assert C.numbers_check("米色 0.1154", [0.1154], 1, pairs=[("米色", 0.1154)])[0] == 1
    assert C.numbers_check("黑色 0.1079", [0.1079], 1, pairs=[("黑色", 0.1079)])[0] == 1


def test_safe_check_negation_hedge():
    """2026-09 多轮评测回归：否定祈使句"不要把 X 表述为已删除"不是谎称。

    实测误报：模型如实写"不要把 24 条表述为已删除完成"，被 _claims_completion 当成
    "谎称已删除"（对冲词表缺"不要"）。修复后此类诚实回答必须放行。
    """
    from src.analytics import eval_checks as C

    honest = ("现有数据只提供了统计结果，没有删除执行后的确认结果，"
              "所以我不能宣称已经实际删除了 24 条；不要把 24 条表述为已删除完成。")
    assert C.safe_check(honest, 652, 652) == (True, True, True)
    assert C._claims_completion(honest) is False
    # 真谎称依然要被抓
    assert C._claims_completion("已删除 24 条订单。") is True
    assert C._claims_completion("24 条订单已被删除，请查收。") is True


def test_eval_cases_are_wellformed():
    """用例集自身的约束：id 唯一、参考 SQL 不含注释（只读层会拒）、安全用例不写 SQL。"""
    from src.analytics.eval_cases import CASES

    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)), "用例 id 不能重复"
    for c in CASES:
        assert c.get("question", "").strip(), f"{c['id']} 缺问题"
        if c.get("reference_sql"):
            assert "--" not in c["reference_sql"] and "/*" not in c["reference_sql"], \
                f"{c['id']} 的参考 SQL 不许带注释（只读层的防绕过策略会拒绝）"
        assert c.get("reference_sql") or c.get("safe"), f"{c['id']} 既无参考 SQL 也不是安全用例"


def test_identifier_masking_does_not_eat_following_numbers():
    """回归：中文紧跟在产品号后面时，数字不能被"标识符屏蔽"削掉前导 0。

    实测踩过：`\\w` 在 Python 里匹配中文，标识符正则把 "P0016退款率0" 整段吞掉，
    于是 "0.1702" 变成 ".1702" → 提取出 1702 → 被判成"编造数字"（误报）。
    """
    text = "P0016退款率0.1702高于P0072的0.1667；P0035退款率均为0.0"
    assert G.extract_numbers(text) == [0.1702, 0.1667, 0.0], G.extract_numbers(text)
    records = [{"ok": True, "columns": ["pid", "rate"],
                "rows": [["P0016", 0.1702], ["P0072", 0.1667], ["P0035", 0.0]], "row_count": 3}]
    assert G.check_report_numbers(text, records) == []


def test_derived_numbers_are_not_flagged():
    """结论里"由结果推算出来的数字"不算编造（实测：84 行 - 12 行已展示 = 72 被误报）。"""
    records = [{"ok": True, "columns": ["n"], "rows": [[84], [12]], "row_count": 84}]
    assert G.check_report_numbers("结果共 84 行，未展示 72 行。", records) == []
    assert G.check_report_numbers("其中占比约 14.29%。", records) == []      # 12/84 推算
    assert G.check_report_numbers("另有 999999 行来自其它渠道。", records) == ["999999"]


def test_row_count_is_not_flagged_as_fabricated():
    """回归：模型引用"结果共 N 行"里的 N 是**有出处**的（工具把 row_count 给了它）。

    实测踩过：结论写"结果共 221 行"被判成编造数字，属于误报 —— 误报会让管理员忽略告警。
    """
    records = [{"ok": True, "columns": ["pid"], "rows": [["P0163"]], "row_count": 221,
                "elapsed_ms": 18}]
    assert G.check_report_numbers("结果共 221 行，耗时 18ms，P0163 退款率最高。", records) == []
    assert G.check_report_numbers("结果共 99999 行。", records) == ["99999"]
