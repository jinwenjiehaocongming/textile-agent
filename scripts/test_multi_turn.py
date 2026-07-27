"""
LLM 模拟客户 — 多轮对话标准测试
================================
一个 LLM 扮客户，你的 Agent 应答。覆盖售前→下单→售后完整流程。

结果保存到: test_results/
运行: python scripts/test_multi_turn.py
"""

import sys, json, time
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).parent.parent))

from openai import OpenAI
from langchain_core.messages import HumanMessage
from src.agent import build_graph
from dotenv import load_dotenv
import os

load_dotenv()

customer_llm = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
)

app = build_graph()

RESULTS_DIR = Path(__file__).parent.parent / "eval_results"
RESULTS_DIR.mkdir(exist_ok=True)

# ============================================================
# 测试场景定义
# ============================================================
SCENARIOS = [
    # 场景1：完整售前 → 下单
    {
        "name": "售前_完整下单",
        "persona": "你是服装厂采购员。你要买2000米白色羽绒服面料，电话13857577360，地址杭州西湖区，7天内要货。",
        "rounds": 5,
        "success_if": ["订单已生成", "ORD-"],
    },
    # 场景2：售后投诉 + 退款
    {
        "name": "售后_色差退货",
        "persona": "你是服装厂采购员。你买的面料有色差要退货，订单号 ORD-20260715-002。客服问什么就答什么。",
        "rounds": 5,
        "success_if": ["退款工单已生成", "待审核"],
    },
    # 场景3：简单咨询
    {
        "name": "咨询_面料知识",
        "persona": "你是服装厂采购员。你想了解磨毛工艺对布料强度的影响，自然提问。",
        "rounds": 2,
        "success_if": ["磨毛", "强度", "15-30%"],
    },
    # 场景4：闲聊 + 转询价
    {
        "name": "闲聊转询价",
        "persona": "你是服装厂采购员。先打招呼说你好，然后自然转向问190T涤塔夫什么价格。",
        "rounds": 3,
        "success_if": ["涤塔夫", "价格"],
    },
    # 场景5：询价 + 讨价还价
    {
        "name": "询价_讨价还价",
        "persona": "你是服装厂采购员。问牛津布900D的价格，然后尝试还价说能不能便宜点。",
        "rounds": 3,
        "success_if": ["牛津布", "900D"],
    },
]


def run_scenario(persona: str, rounds: int, success_keywords: list) -> dict:
    """跑一个测试场景，返回对话记录"""
    msgs = ["你好"]  # 开场
    state = {"messages": [], "knowledge_chunks": [], "rewrite_query": "",
             "query_type": "chat", "user_id": "test_bot", "user_context": ""}
    log = []

    for turn in range(rounds):
        # Agent 回复
        state["messages"] = state["messages"] + [HumanMessage(content=msgs[-1])]
        state["knowledge_chunks"] = []
        state["rewrite_query"] = ""
        state = app.invoke(state)
        reply = state["messages"][-1]
        reply_text = reply.content if hasattr(reply, "content") else str(reply)

        log.append({"role": "customer", "content": msgs[-1]})
        log.append({"role": "agent", "content": reply_text[:500] if reply_text else ""})

        # 检查成功条件
        success = all(kw in reply_text for kw in success_keywords) if reply_text else False
        if success:
            log.append({"role": "system", "content": "✅ 测试通过"})
            return {"passed": True, "log": log, "turns": turn + 1}

        # 客户 LLM 决定下一句话
        history = "\n".join(
            f"{'客户' if m.get('role') == 'customer' else '客服'}: {m['content'][:80]}"
            for m in log[-6:]
        )
        try:
            resp = customer_llm.chat.completions.create(
                model="deepseek-v4-flash", temperature=0.7,
                messages=[{"role": "user", "content": f"""{persona}
根据客服回复自然对话。不要一次性把所有信息说完。不要重复已说过的话。
对话历史：
{history}
只输出你要说的话："""}])
            next_msg = resp.choices[0].message.content.strip()
            msgs.append(next_msg)
        except Exception as e:
            log.append({"role": "system", "content": f"❌ 客户 LLM 异常: {e}"})
            return {"passed": False, "log": log, "turns": turn + 1}

        time.sleep(0.3)

    log.append({"role": "system", "content": f"❌ 未在 {rounds} 轮内通过"})
    return {"passed": False, "log": log, "turns": rounds}


def main():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = {"timestamp": timestamp, "scenarios": [], "summary": {}}

    passed = 0
    for sc in SCENARIOS:
        print(f"\n🧪 {sc['name']} ...", end=" ", flush=True)
        result = run_scenario(sc["persona"], sc["rounds"], sc["success_if"])
        status = "✅" if result["passed"] else "❌"
        print(f"{status} ({result['turns']}轮)")

        sc_result = {"name": sc["name"], "passed": result["passed"], "turns": result["turns"]}
        report["scenarios"].append(sc_result)
        if result["passed"]:
            passed += 1

        # 保存对话记录
        log_file = RESULTS_DIR / f"{timestamp}_{sc['name']}.json"
        with open(log_file, "w", encoding="utf-8") as f:
            json.dump(result["log"], f, ensure_ascii=False, indent=2)

    report["summary"] = {
        "total": len(SCENARIOS),
        "passed": passed,
        "rate": f"{passed}/{len(SCENARIOS)}",
    }

    print(f"\n{'='*50}")
    print(f"结果: {passed}/{len(SCENARIOS)} 通过")
    print(f"报告: test_results/")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
