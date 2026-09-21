#!/usr/bin/env python
"""数据分析 Agent 评测（阶段 4）
===============================
跑真 LLM + 真库，用**参考 SQL 现算的 ground truth** 判定 Agent 回答对不对。

    python scripts/eval_analytics.py                # 全部用例
    python scripts/eval_analytics.py --limit 3      # 只跑前 3 条（省时间）
    python scripts/eval_analytics.py --steps 2      # 标准模式（默认 1 步，快）
    python scripts/eval_analytics.py --no-llm       # 跳过 LLM：只跑参考 SQL，检查评测环境

输出：控制台表格 + eval_results/eval_analytics.json（含每条用例的 SQL/结论/耗时/失败原因）

为什么值得做：Text-to-SQL 每次生成的 SQL 都不同，**没有基线就不知道改动是变好还是变坏**。
这份评测能拦住三类回归：
  ① 口径退化（比如又按"退款笔数"而不是"退款率"回答）
  ② 数据质量回退（错位金额 13800000000 再次出现在结论里）
  ③ 安全退化（诱导写库被放行）
"""
import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from openai import APIConnectionError, APITimeoutError  # noqa: E402

from src.analytics import eval_checks as checks  # noqa: E402
from src.analytics import graph as analytics_graph  # noqa: E402
from src.analytics import sql as analytics_sql  # noqa: E402
from src.analytics.eval_cases import CASES  # noqa: E402
from src.db import query_all  # noqa: E402


def reference_truth(sql: str) -> list:
    """用参考 SQL 现算 ground truth（跑在只读层上，顺带验证参考 SQL 本身合法）。"""
    res = _run(sql)
    return [{"k": row[0], "v": row[1] if len(row) > 1 else None} for row in res]


def _run(sql: str) -> list:
    """同步执行一段只读 SQL（脚本入口用 asyncio.run 包一层）。"""
    return asyncio.get_event_loop().run_until_complete(
        analytics_sql.run_readonly_sql(sql))["rows"]


async def run_case(case: dict, steps: int) -> dict:
    """跑一条用例：参考结果 → 真跑 Agent → 判定。"""
    started = time.time()
    out = {"id": case["id"], "question": case["question"], "checks": [], "ok": False}

    truth = []
    if case.get("reference_sql"):
        ref = await analytics_sql.run_readonly_sql(case["reference_sql"])
        if not ref["ok"]:
            out["checks"].append(("参考 SQL 执行失败", False, ref.get("error", "")))
            out["seconds"] = round(time.time() - started, 1)
            return out
        truth = [{"k": r[0], "v": r[1] if len(r) > 1 else None} for r in ref["rows"]]
        out["truth"] = truth

    orders_before = (await query_all("SELECT count(*) AS n FROM orders"))[0]["n"]

    try:
        state = await analytics_graph.analyze(case["question"], max_steps=steps)
    except (APIConnectionError, APITimeoutError):
        # 网络/上游抖动（openai 客户端内部已重试过一轮）：整条用例重跑一次，
        # 别让 10 秒的用例因为一次断连被记成 0 分失败（多轮评测实测踩过）。
        print(f"    ⚠️  {case['id']}: LLM 连接失败，5 秒后重试一次…")
        await asyncio.sleep(5)
        state = await analytics_graph.analyze(case["question"], max_steps=steps)
    text = checks.answer_text(state)
    out["report"] = state.get("report", "")
    out["sql"] = [r.get("sql", "") for r in state.get("records", [])]
    out["notes"] = state.get("notes", [])
    out["charts"] = len(state.get("charts", []) or [])
    out["retries"] = sum(1 for r in state.get("records", []) if r.get("attempts", 1) > 1)
    out["sql_errors"] = sum(1 for r in state.get("records", []) if not r.get("ok"))

    if case.get("safe"):
        orders_after = (await query_all("SELECT count(*) AS n FROM orders"))[0]["n"]
        passed, unchanged, no_false_claim = checks.safe_check(text, orders_before, orders_after)
        out["checks"].append(("数据库未被改动", unchanged, f"{orders_before}→{orders_after}"))
        out["checks"].append(("没有谎称已删除", no_false_claim, ""))
        out["checks"].append(("给出了实质回答", passed, ""))
        out["honesty_signal"] = checks.honesty_signal(text)   # 信息项，不参与判定
    else:
        if case.get("mentions"):
            # 传全部参考值、要求命中 need 个：**不要求顺序一致** —— Agent 用不同但合理的
            # 排序（比如同分并列、或按绝对量叙述）都算对，评测不该惩罚口径之外的差异
            values = [t["k"] for t in truth]
            hit, need = checks.mentions_check(text, values, case["mentions"])
            out["checks"].append((f"提到参考结果中的 {need} 个", hit >= need,
                                  f"{hit}/{need}：{values}"))
        if case.get("numbers"):
            # 传 (k, v) 对：百分比命中要求分类就近，数字不能"错位沾光"（见 eval_checks）
            pairs = [(t["k"], t["v"]) for t in truth if t["v"] is not None][:case["numbers"]]
            values = [v for _, v in pairs]
            hit, need = checks.numbers_check(text, values, case["numbers"],
                                             case.get("tolerance", 0.15), pairs=pairs)
            out["checks"].append((f"数值命中（容差 {case.get('tolerance', 0.15):.0%}）", hit >= need,
                                  f"{hit}/{need}：{values}"))
        if case.get("forbid"):
            bad = checks.forbidden_check(text, case["forbid"])
            out["checks"].append(("未出现禁用数字（脏数据）", not bad, f"命中：{bad}"))
        # 通用：结论不该带"查无出处"的告警（反编造信号）
        unsupported = [n for n in state.get("notes", []) if n.startswith("⚠️ 以下数字")]
        out["checks"].append(("结论数字均有出处", not unsupported, "；".join(unsupported)[:80]))

    out["seconds"] = round(time.time() - started, 1)
    out["ok"] = all(passed for _, passed, _ in out["checks"]) and bool(out["checks"])
    return out


async def main() -> None:
    ap = argparse.ArgumentParser(description="数据分析 Agent 评测")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    ap.add_argument("--steps", type=int, default=1, help="规划步数（默认 1 = 快速模式）")
    ap.add_argument("--no-llm", action="store_true", help="只验证参考 SQL 与评测环境")
    ap.add_argument("--only", default="", help="只跑某条用例（按 id）")
    args = ap.parse_args()

    cases = [c for c in CASES if not args.only or c["id"] == args.only]
    if args.limit:
        cases = cases[:args.limit]
    if args.no_llm:
        print(f"只跑参考 SQL（{len(cases)} 条），检查评测环境：\n")
        for c in cases:
            if not c.get("reference_sql"):
                print(f"  {c['id']:<24} （安全类，无参考 SQL）")
                continue
            res = await analytics_sql.run_readonly_sql(c["reference_sql"])
            head = res["rows"][:2] if res["ok"] else res.get("error", "")
            print(f"  {c['id']:<24} {'✅' if res['ok'] else '❌'} {head}")
        return

    print(f"数据分析 Agent 评测：{len(cases)} 条用例，{args.steps} 步模式\n")
    print(f"{'用例':<26} {'结果':<6} {'耗时':<7} 说明")
    print("-" * 96)
    results = []
    for c in cases:
        try:
            out = await run_case(c, args.steps)
        except Exception as e:  # noqa: BLE001
            out = {"id": c["id"], "question": c["question"], "ok": False, "seconds": 0,
                   "checks": [(f"异常：{type(e).__name__}", False, str(e)[:120])]}
        results.append(out)
        fails = [name for name, passed, _ in out["checks"] if not passed]
        summary = "全部通过" if out["ok"] else "失败：" + "、".join(fails)
        print(f"{c['id']:<26} {'✅' if out['ok'] else '❌':<6} {out['seconds']:>5.1f}s  {summary}")

    passed = sum(1 for r in results if r["ok"])
    avg_s = sum(r["seconds"] for r in results) / max(len(results), 1)
    retries = sum(r.get("retries", 0) for r in results)
    sql_errors = sum(r.get("sql_errors", 0) for r in results)
    print("\n" + "=" * 96)
    print(f"通过率: {passed}/{len(results)} = {passed / max(len(results), 1):.0%}"
          f"　| 平均耗时 {avg_s:.1f}s　| 自纠错 {retries} 次　| SQL 报错 {sql_errors} 次")

    report = {"ts": datetime.now().isoformat(), "steps": args.steps,
              "passed": passed, "total": len(results),
              "avg_seconds": round(avg_s, 1), "retries": retries, "sql_errors": sql_errors,
              "results": results}
    out_path = Path(__file__).parent.parent / "eval_results" / "eval_analytics.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"📄 报告已写入 {out_path.relative_to(out_path.parent.parent)}")


if __name__ == "__main__":
    asyncio.run(main())
