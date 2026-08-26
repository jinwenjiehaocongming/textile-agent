"""MCP 客户端 — 官方 SDK（异步）

企业级演进（2026-08）
====================
- 自研 subprocess + JSON-RPC（同步）→ 官方 mcp SDK：ClientSession + stdio_client（异步）
- 协议握手（initialize / tools/list / tools/call）由 SDK 完成，工具动态发现语义保留
- 原自研版"日志走 stderr 防污染协议通道"由 SDK 内部处理，无需自管

叙事：手写版证明理解协议三步握手；迁移官方版证明异步生态下的工程取舍
（协议版本跟进、中间件、传输扩展都不再是自己要维护的成本）。
"""
import asyncio
import os
import sys
from typing import Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.logging_config import get_logger
logger = get_logger(__name__)


class AsyncMCPClient:
    """异步 MCP 客户端：管理多个 Server 会话，按工具名路由。"""

    def __init__(self):
        self._sessions: dict[str, ClientSession] = {}
        self._hold: list = []  # 持有 stdio/ClientSession 的 CM，防 GC 提前关闭
        self._tool_server: dict[str, str] = {}  # 工具名 → Server 名
        self.tools: list[dict] = []

    async def connect_all(self, servers: dict[str, list[str]]) -> None:
        """按注册表连接全部 MCP Server。

        命令形如 ["python3", "src/mcp_servers/xx.py"]：
        - 用当前解释器（venv）启动子进程，避免系统 python3 缺 mcp 依赖
        - 注入 PYTHONPATH=项目根，保证 server 内 import src.* 可解析
        """
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name, cmd in servers.items():
            params = StdioServerParameters(
                command=sys.executable,
                args=cmd[1:],
                env={**os.environ, "PYTHONPATH": project_root},
            )
            stdio_cm = stdio_client(params)
            read, write = await stdio_cm.__aenter__()
            session_cm = ClientSession(read, write)
            session = await session_cm.__aenter__()
            await session.initialize()
            listed = await session.list_tools()
            for t in listed.tools:
                self.tools.append({
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.inputSchema,
                    "_server": name,
                })
                self._tool_server[t.name] = name
            self._sessions[name] = session
            self._hold.extend([stdio_cm, session_cm])
            logger.info(f"[MCP] {name} 已连接 ({len(listed.tools)} 个工具)")

    async def call_tool(self, name: str, args: dict) -> str:
        """调用工具，返回文本结果。调用失败返回可读错误（不抛给业务层）。"""
        server = self._tool_server.get(name)
        if not server:
            return f"未知工具: {name}"
        try:
            result = await self._sessions[server].call_tool(name, arguments=args or {})
        except Exception as e:  # noqa: BLE001
            return f"工具 {name} 调用失败: {str(e)[:120]}"
        if getattr(result, "isError", False):
            return f"工具 {name} 执行错误"
        parts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
        return "\n".join(parts) if parts else str(result)

    def get_tools_for_langchain(self, names=None) -> list[dict]:
        """返回 LangChain bind_tools 格式的工具描述（OpenAI function 规范）。"""
        tools = self.tools
        if names is not None:
            tools = [t for t in self.tools if t["name"] in names]
        return [{
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["inputSchema"],
            },
        } for t in tools]

    async def shutdown(self) -> None:
        for name, session in list(self._sessions.items()):
            try:
                await session.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        self._sessions.clear()
        self.tools.clear()
        self._tool_server.clear()
        logger.info("[MCP] 所有连接已关闭")


# ── 全局单例 ─────────────────────────────────────────────

_client: Optional[AsyncMCPClient] = None


async def init_mcp(servers: dict[str, list[str]]) -> AsyncMCPClient:
    """初始化并连接全部 MCP Server（幂等：重复调用只补连新 Server）。"""
    global _client
    if _client is None:
        _client = AsyncMCPClient()
    await _client.connect_all(servers)
    return _client


def get_mcp() -> AsyncMCPClient:
    if _client is None:
        raise RuntimeError("MCP 未初始化：请先 await init_mcp(servers)")
    return _client