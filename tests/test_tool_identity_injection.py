"""工具调度点的身份注入守卫（2026-09）
======================================
为什么单独一个文件：`src/agent.py::tool_executor` 里用 `with_trusted_identity(...)`
注入可信身份，但**漏了 import** —— 而当时那条"已补 import"的断言写的是
`assert "with_trusted_identity" in s`，被**调用点**的字符串骗过（调用点里也有这个名字）。
结果：单元测试 251 条全绿、CI 绿，而**客户每问一次产品就 NameError**
（售前 Agent 的每次工具调用都走这条路）。

抓到它的是端到端评测（5 条售前用例同时挂，报 `name 'with_trusted_identity' is not defined`），
不是测试套件 —— 因为单测把 LLM 与工具循环都 mock 掉了，这段代码从来没被执行过。

本文件补上这个盲区：用**假 MCP 客户端**真的执行一次 `tool_executor`，于是
① 模块级名字有没有绑定、② 身份有没有被覆盖注入，两条都被钉住。
（教训：`assert "某个字符串在文件里"` 是假断言；要断言**行为**。）
"""
import os

os.environ.setdefault("DEV_MODE", "1")
os.environ.setdefault("JWT_SECRET", "test-secret-only-0123456789abcdef0123456789abcdef")

from langchain_core.messages import AIMessage  # noqa: E402

from src import agent as A  # noqa: E402
from src.order_access import with_trusted_identity  # noqa: E402


class _FakeMCP:
    """替身：把调用原样记下来，不真的起子进程。"""

    def __init__(self):
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return "FAKE 结果"


async def test_tool_executor_binds_identity_helper(monkeypatch):
    """真实执行一次工具节点：既验"名字有绑定"，也验"身份被覆盖注入"。"""
    fake = _FakeMCP()
    monkeypatch.setattr(A, "get_mcp", lambda: fake)

    msg = AIMessage(content="", tool_calls=[{
        "name": "query_order_status",
        "args": {"order_no": "ORD-X", "caller_id": "victim_user"},   # LLM 自报的身份
        "id": "call_1",
    }])
    state = {"messages": [msg], "user_id": "attacker_user"}

    out = await A.tool_executor(state)      # 漏 import 时这里会抛 NameError

    assert fake.calls, "工具应该被调用一次"
    name, args = fake.calls[0]
    assert name == "query_order_status"
    assert args["caller_id"] == "attacker_user", "服务端身份必须覆盖 LLM 自报的值"
    assert out["messages"][-1].content == "FAKE 结果"


async def test_tool_executor_without_identity_strips_llm_identity(monkeypatch):
    """没有可信身份（未登录/guest）时，不能把 LLM 自报的身份传下去。"""
    fake = _FakeMCP()
    monkeypatch.setattr(A, "get_mcp", lambda: fake)

    msg = AIMessage(content="", tool_calls=[{
        "name": "query_order_status",
        "args": {"order_no": "ORD-VICTIM", "caller_id": "victim_user"},
        "id": "call_2",
    }])
    await A.tool_executor({"messages": [msg]})      # 没有 user_id

    _, args = fake.calls[0]
    assert "caller_id" not in args, "身份为空时必须清掉，让工具侧 fail-closed 拒绝"


def test_every_dispatch_site_imports_the_helper():
    """三个调度点都必须真的 import 了这个名字（防止再犯同一个错）。

    比"文件里出现过这个名字"强：这里解析 AST，只看 **import 语句**。
    """
    import ast
    import pathlib

    sites = ["src/agent.py", "src/order_agent.py", "src/after_sales_agent.py"]
    for rel in sites:
        tree = ast.parse(pathlib.Path(rel).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported |= {a.asname or a.name for a in node.names}
        assert "with_trusted_identity" in imported, f"{rel} 用了却没 import（NameError 隐患）"
