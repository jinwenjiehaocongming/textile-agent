"""
轻量 MCP Client — 基于 subprocess + JSON-RPC over stdio
========================================================
不依赖 mcp 包，纯标准库实现。通过子进程启动 MCP Server，
用 JSON-RPC 2.0 协议通过标准输入输出通信。

设计要点：
  用标准库实现轻量 MCP Client，通过 subprocess + JSON-RPC over stdio 通信，
  完成协议发现（initialize → tools/list → tools/call）的完整握手流程。
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from src.logging_config import get_logger
logger = get_logger(__name__)


class MCPSyncClient:
    """
    MCP 同步客户端。
    启动 MCP Server 子进程，通过 stdin/stdout 发送 JSON-RPC 请求。
    所有方法都是同步的，可以直接在 LangGraph 节点里调用。
    """

    def __init__(self):
        self.servers: dict[str, subprocess.Popen] = {}
        self.tools: list[dict] = []
        self._req_id = 0

    # ── 连接 Server ──────────────────────────────────────

    def connect_server(self, name: str, command: list[str]) -> None:
        """
        启动 MCP Server 子进程，完成握手 + 工具发现。

        Args:
            name:    Server 别名（如 "product"、"order"）
            command: 启动命令，如 ["python", "src/mcp_servers/product_server.py"]
        """
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            bufsize=1,          # 行缓冲，readline 不会死等
        )

        # 1. initialize 握手
        init_result = self._request(proc, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "textile-agent", "version": "1.0.0"},
        })

        # 2. initialized 通知
        self._notify(proc, "notifications/initialized", {})

        # 3. tools/list 发现工具
        tools_result = self._request(proc, "tools/list", {})
        for tool in tools_result.get("tools", []):
            self.tools.append({
                "name": tool["name"],
                "description": tool.get("description", ""),
                "inputSchema": tool.get("inputSchema", {}),
                "_proc": proc,
                "_server": name,
            })

        self.servers[name] = proc
        logger.info(f"[MCP] {name} 已连接 ({len(tools_result.get('tools', []))} 个工具)")

    # ── JSON-RPC 通信 ────────────────────────────────────

    def _request(self, proc: subprocess.Popen, method: str, params: dict) -> dict:
        """发送 JSON-RPC 请求，等待并解析响应"""
        self._req_id += 1
        request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self._req_id,
        }
        self._send(proc, request)
        return self._recv(proc)

    def _notify(self, proc: subprocess.Popen, method: str, params: dict) -> None:
        """发送 JSON-RPC 通知（无 id，不等待响应）"""
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._send(proc, notification)

    def _send(self, proc: subprocess.Popen, msg: dict) -> None:
        """写一行 JSON 到子进程 stdin"""
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        proc.stdin.write(line)
        proc.stdin.flush()

    def _recv(self, proc: subprocess.Popen) -> dict:
        """从子进程 stdout 读一行 JSON"""
        line = proc.stdout.readline()
        try:
            return json.loads(line).get("result", {})
        except (json.JSONDecodeError, AttributeError):
            return {}

    # ── 工具调用 ─────────────────────────────────────────

    def call_tool(self, name: str, args: dict) -> str:
        """
        同步调用工具，返回纯文本结果。

        这是替代原来 tool_executor 里 if/elif 分发的统一入口。
        不需要知道工具在哪个 Server——Client 自动路由。
        """
        for tool in self.tools:
            if tool["name"] == name:
                result = self._request(tool["_proc"], "tools/call", {
                    "name": name,
                    "arguments": args,
                })
                # MCP 返回 content: [{type: "text", text: "..."}]
                return self._extract_text(result)
        return f"Error: 未找到工具 '{name}'"

    def _extract_text(self, result: dict) -> str:
        """从 MCP content 数组中提取文本"""
        texts = []
        for item in result.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(item.get("text", ""))
            elif isinstance(item, str):
                texts.append(item)
        return "\n".join(texts) if texts else str(result)

    # ── 工具格式转换 ─────────────────────────────────────

    def get_tools_for_langchain(self, names=None) -> list:
        """
        返回 LangChain bind_tools 格式的工具列表。
        替代原来手写的 SEARCH_PRODUCT_SCHEMA / QUERY_ORDER_SCHEMA。

        Args:
            names: 工具名白名单，如 ['search_product', 'query_order_status']。
                   不传则返回全部工具。
        """
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

    # ── 生命周期 ─────────────────────────────────────────

    def shutdown(self) -> None:
        """关闭所有 MCP Server 子进程"""
        for name, proc in self.servers.items():
            try:
                proc.stdin.close()
                proc.stdout.close()
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        self.servers.clear()
        self.tools.clear()
        logger.info("[MCP] 所有连接已关闭")


# ── 全局单例 ─────────────────────────────────────────────

_client: Optional[MCPSyncClient] = None


def init_mcp(servers: dict[str, list[str]]) -> MCPSyncClient:
    """
    初始化全局 MCP Client，连接所有 Server。

    Usage:
        init_mcp({
            "product": ["python", "src/mcp_servers/product_server.py"],
            "order":   ["python", "src/mcp_servers/order_server.py"],
            "refund":  ["python", "src/mcp_servers/refund_server.py"],
        })
    """
    global _client
    if _client is not None:
        _client.shutdown()
    _client = MCPSyncClient()
    for name, command in servers.items():
        _client.connect_server(name, command)
    return _client


def get_mcp() -> MCPSyncClient:
    """获取全局 MCP Client"""
    global _client
    if _client is None:
        raise RuntimeError("MCP Client 未初始化，请先调用 init_mcp()")
    return _client
