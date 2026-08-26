"""MCP 客户端测试（官方 SDK · 异步）

黑盒协议测试：用 stdio_client 真实拉起 FastMCP server 子进程，
验证 initialize / tools/list / tools/call 全链路（官方 SDK 语义）。

每个测试在自身 async with 内完成连接与调用（同 task，避开
pytest-asyncio fixture 跨 task 退出 cancel scope 的问题）。
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
SERVER_CMD = [sys.executable, "src/mcp_servers/product_server.py"]


async def _session_call(query: str = None):
    """一次完整的 stdio 连接 + 调用（返回 session 内结果）。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=SERVER_CMD[0], args=SERVER_CMD[1:],
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if query is None:
                tools = await session.list_tools()
                return [t.name for t in tools.tools]
            return await session.call_tool("search_product", {"query": query})


async def test_tools_list_discovery():
    names = await _session_call()
    assert "search_product" in names


async def test_call_tool_roundtrip(pg_db):
    result = await _session_call("T400")
    text = "".join(c.text for c in result.content if getattr(c, "type", "") == "text")
    assert "T400 复合弹力布" in text


async def test_call_tool_validation_error():
    """FastMCP 内建参数校验：缺必填字段 → isError。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=SERVER_CMD[0], args=SERVER_CMD[1:],
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("search_product", {})
            assert getattr(result, "isError", False) is True
