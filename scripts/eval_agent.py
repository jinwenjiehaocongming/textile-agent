"""
Agent 全链路端到端评估
======================
走完整 LangGraph（改写 → 检索 → Supervisor → 分支 Agent → 审核），
覆盖 5 类场景：售前 / 下单 / 售后 / 闲聊 / 安全，用规则断言判断通过与否。

运行: python scripts/eval_agent.py
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
    print(f"\n端到端评估 {len(CASES)} 条用例\n")
    print(f"{'用例':<20} {'结果':<6} 回复摘要")
    print("-" * 70)
    for case in CASES:
        try:
            ok, reply = await run_case(graph, case, user_id="eval_user")
        except Exception as e:
            ok, reply = False, f"异常: {str(e)[:80]}"
        passed += int(ok)
        details.append({"name": case["name"], "passed": bool(ok), "reply": reply})
        summary = reply.replace("\n", " ")[:45]
        print(f"{case['name']:<20} {'✅' if ok else '❌':<6} {summary}")

    rate = passed / len(CASES)
    print("\n" + "=" * 70)
    print(f"通过率: {passed}/{len(CASES)} = {rate:.0%}")

    # 写报告
    out = Path(__file__).parent.parent / "eval_results"
    out.mkdir(exist_ok=True)
    report = {"total": len(CASES), "passed": passed, "pass_rate": f"{rate:.0%}", "details": details}
    (out / "eval_agent.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"📄 报告已写入 eval_results/eval_agent.json")
    await mcp.shutdown()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
