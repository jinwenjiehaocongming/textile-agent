"""
Agent 全链路端到端评估
======================
走完整 LangGraph（改写 → 检索 → Supervisor → 分支 Agent → 审核），
覆盖 5 类场景：售前 / 下单 / 售后 / 闲聊 / 安全，用规则断言判断通过与否。

运行:
    python scripts/eval_agent.py                       # 跑全部 25 条
    python scripts/eval_agent.py --only 安全-拒绝改价   # 只跑匹配的用例（改一条验一条，别每次全跑）
    python scripts/eval_agent.py --only 改价 --repeat 3 # 同一条跑 3 次（LLM 有波动，看是否稳定）
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from langchain_core.messages import HumanMessage

from src.agent import build_graph, thread_config
from src.eval_cases import CASES
from src.mcp_client import init_mcp, get_mcp
from src.render_tools import extract_render_data


def visible_text(result: dict) -> tuple:
    """返回 (供断言的文本, 原始回复文本)。

    断言必须看**用户实际看到的东西**：报价/规格常常是通过结构化卡片渲染的
    （render 工具），文本里只剩一句"以上就是报价明细"。
    只断言文本，会把"已经报价了"判成"没报价" —— 实测踩过（售前-最低价查询）。
    """
    reply = (result.get("messages") or [None])[-1]
    reply = (getattr(reply, "content", "") or "")
    card = extract_render_data(result.get("messages") or [])
    if card:
        return reply + "\n[结构化卡片] " + json.dumps(card, ensure_ascii=False), reply
    return reply, reply



# ============================================================
# 测试用例：共享用例集 src/eval_cases.py（售前/下单/售后/闲聊/安全）
# ============================================================
async def run_case(graph, case: dict, user_id: str) -> tuple:
    state = {
        "messages": case["messages"],
        "knowledge_chunks": [],
        "rewrite_query": "",
        "query_type": case["qtype"],
        "user_id": user_id,
        "user_context": "",
    }
    cfg = thread_config(user_id)
    result = await graph.ainvoke(state, config=cfg)
    if "__interrupt__" in result:
        # HITL：下单会先挂起等人工审批。评测假设审批人放行，自动通过后
        # 再取最终回复（订单号只有审批通过后才生成）。
        from langgraph.types import Command
        result = await graph.ainvoke(Command(resume={"approved": True}), config=cfg)
    visible, reply = visible_text(result)
    passed = case["check"](visible)
    return passed, reply


async def main():
    import argparse
    ap = argparse.ArgumentParser(description="端到端评测（可只跑指定用例）")
    ap.add_argument("--only", default="", help="只跑名称包含该子串的用例（逗号分隔可多个）")
    ap.add_argument("--repeat", type=int, default=1, help="每条重复跑几次（看行为是否稳定）")
    args = ap.parse_args()

    cases = CASES
    if args.only:
        keys = [k.strip() for k in args.only.split(",") if k.strip()]
        cases = [c for c in CASES if any(k in c["name"] for k in keys)]
        if not cases:
            print(f"❌ 没有匹配的用例: {args.only}")
            return
        print(f"（只跑匹配 {args.only!r} 的 {len(cases)} 条用例 × {args.repeat} 次）")

    print("🔌 连接 MCP + 构建 graph...")
    await init_mcp({
        "product": ["python3", "src/mcp_servers/product_server.py"],
        "order":   ["python3", "src/mcp_servers/order_server.py"],
        "refund":  ["python3", "src/mcp_servers/refund_server.py"],
    })
    graph = build_graph()
    mcp = get_mcp()

    passed = 0
    details = []
    total_runs = len(cases) * args.repeat
    print(f"\n端到端评估 {len(cases)} 条用例 × {args.repeat} 次 = {total_runs} 次\n")
    print(f"{'用例':<20} {'结果':<6} 回复摘要")
    print("-" * 70)
    for case in cases:
        for i in range(args.repeat):
            label = case["name"] if args.repeat == 1 else f"{case['name']}#{i + 1}"
            try:
                # 每次换一个 user_id：同一条用例重复跑时，避免上下文/会话状态互相串
                ok, reply = await run_case(graph, case, user_id=f"eval_user_{abs(hash(label)) % 10000}")
            except Exception as e:
                ok, reply = False, f"异常: {str(e)[:80]}"
            passed += int(ok)
            details.append({"name": label, "passed": bool(ok), "reply": reply})
            summary = reply.replace("\n", " ")[:45]
            print(f"{label:<20} {'✅' if ok else '❌':<6} {summary}")

    rate = passed / total_runs
    print("\n" + "=" * 70)
    print(f"通过率: {passed}/{total_runs} = {rate:.0%}")

    # 写报告
    out = Path(__file__).parent.parent / "eval_results"
    out.mkdir(exist_ok=True)
    report = {"total": total_runs, "passed": passed, "pass_rate": f"{rate:.0%}",
              "only": args.only or None, "repeat": args.repeat, "details": details}
    # ⚠️ 单用例运行要写到**另一个文件**：否则一次 `--only` 调试就会把全量留档覆盖成
    # "1/1 通过"，看着像满分（本会话开头就真发生过：全量 7/7 的留档被单用例覆盖）。
    name = "eval_agent_only.json" if args.only else "eval_agent.json"
    report["partial"] = bool(args.only)
    (out / name).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"📄 报告已写入 eval_results/{name}" + ("（单用例运行，不覆盖全量留档）" if args.only else ""))
    await mcp.shutdown()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
