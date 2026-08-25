"""
轻量 MCP Client — 基于 subprocess + JSON-RPC over stdio（线程安全版）
====================================================================
不依赖 mcp 包，纯标准库实现。通过子进程启动 MCP Server，
用 JSON-RPC 2.0 协议通过标准输入输出通信。

线程安全设计（P0-2 改造）：
1. 请求串行化 — 全局 RLock 保证同一时刻只有一个 in-flight 请求，
   JSON-RPC 响应与请求一一对应，绝不串线（工具是毫秒级本地 SQLite，
   串行代价可忽略；真要并发吞吐需换 HTTP 传输，属后续演进）。
2. 响应超时 — _recv 用「读线程 + join(timeout)」实现，子进程僵死
   不会永久阻塞请求，超时抛 ConnectionError。
3. 崩溃自愈 — call_tool 发现子进程已死（poll() 非 None）或超时后，
   自动重启该 Server（重做 initialize/tools/list 握手）并重试一次。
"""

import json
import subprocess
import sys
import threading
from typing import Any, Optional

from src.logging_config import get_logger
logger = get_logger(__name__)

# 单次工具调用的响应超时（秒）。工具是本地 SQLite，正常几十毫秒；
# 该值只防「子进程僵死/死锁」场景，不设太短以免误伤。
DEFAULT_RESPONSE_TIMEOUT = 30.0


class MCPSyncClient:
    """MCP 同步客户端（线程安全）。"""

    def __init__(self):
        self.servers: dict[str, subprocess.Popen] = {}
        self.tools: list[dict] = []
        self._commands: dict[str, list[str]] = {}  # Server 启动命令（崩溃重启用）
        self._req_id = 0
        self._io_lock = threading.RLock()  # 串行化 requests/responses

    # ── 连接 / 重启 Server ──────────────────────────────

    def connect_server(self, name: str, command: list[str]) -> None:
        """启动 MCP Server 子进程，完成握手 + 工具发现。"""
        proc = self._spawn(command)
        with self._io_lock:
            self._handshake(proc, name)
        self.servers[name] = proc
        logger.info(f"[MCP] {name} 已连接 ({len([t for t in self.tools if t['_server'] == name])} 个工具)")

    def _spawn(self, command: list[str]) -> subprocess.Popen:
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            bufsize=1,  # 行缓冲，readline 不会死等
        )

    def _handshake(self, proc: subprocess.Popen, name: str) -> None:
        """initialize → initialized 通知 → tools/list，并登记工具。"""
        init_result = self._request(proc, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "textile-agent", "version": "1.0.0"},
        }, timeout=10)
        self._notify(proc, "notifications/initialized", {})

        tools_result = self._request(proc, "tools/list", {}, timeout=10)
        # 移除该 Server 旧的工具登记，再重新登记（重启场景）
        self.tools = [t for t in self.tools if t["_server"] != name]
        for tool in tools_result.get("tools", []):
            self.tools.append({
                "name": tool["name"],
                "description": tool.get("description", ""),
                "inputSchema": tool.get("inputSchema", {}),
                "_proc": proc,
                "_server": name,
            })

    def _restart_server(self, name: str, command: list[str]) -> bool:
        """重启一个死掉的 Server：kill 旧进程 → 重新 spawn → 握手。成功返回 True。"""
        old = self.servers.get(name)
        if old is not None and old.poll() is None:
            try:
                old.terminate()
                old.wait(timeout=3)
            except Exception:
                old.kill()
        try:
            proc = self._spawn(command)
            with self._io_lock:
                self._handshake(proc, name)
            self.servers[name] = proc
            logger.warning(f"[MCP] {name} 子进程已重启")
            return True
        except Exception as e:
            logger.error(f"[MCP] {name} 重启失败: {e}")
            return False

    # ── JSON-RPC 通信（全部在 _io_lock 下串行）──────────

    def _request(self, proc: subprocess.Popen, method: str, params: dict,
                 timeout: float = DEFAULT_RESPONSE_TIMEOUT) -> dict:
        """发送 JSON-RPC 请求，等待并解析响应。超时/进程死亡抛 ConnectionError。"""
        self._req_id += 1
        request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self._req_id,
        }
        self._send(proc, request)
        return self._recv(proc, timeout=timeout)

    def _notify(self, proc: subprocess.Popen, method: str, params: dict) -> None:
        """发送 JSON-RPC 通知（无 id，不等待响应）。"""
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._send(proc, notification)

    def _send(self, proc: subprocess.Popen, msg: dict) -> None:
        """写一行 JSON 到子进程 stdin。进程已死 → 抛 ConnectionError。"""
        if proc.poll() is not None:
            raise ConnectionError("MCP 子进程已退出")
        try:
            line = json.dumps(msg, ensure_ascii=False) + "\n"
            proc.stdin.write(line)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise ConnectionError(f"MCP 子进程不可写: {e}")

    def _recv(self, proc: subprocess.Popen, timeout: float) -> dict:
        """
        从子进程 stdout 读一行 JSON。
        读线程 + join(timeout)：超时抛 ConnectionError，绝不永久阻塞。
        """
        box: list = []

        def _read() -> None:
            box.append(proc.stdout.readline())

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise ConnectionError(f"MCP 子进程响应超时（>{timeout}s）")

        line = box[0] if box else ""
        try:
            return json.loads(line).get("result", {})
        except (json.JSONDecodeError, AttributeError) as e:
            raise ConnectionError(f"MCP 子进程返回异常: {line[:100]!r} ({e})")

    # ── 工具调用 ─────────────────────────────────────────

    def call_tool(self, name: str, args: dict,
                  timeout: float = DEFAULT_RESPONSE_TIMEOUT) -> str:
        """
        同步调用工具，返回纯文本结果。
        线程安全 + 崩溃自愈：进程死亡/超时 → 自动重启 Server 并重试一次。
        """
        for attempt in range(2):
            tool = self._find_tool(name)
            if tool is None:
                return f"Error: 未找到工具 '{name}'"
            proc = tool["_proc"]
            server_name = tool["_server"]

            # 子进程已死 → 先尝试重启
            if proc.poll() is not None:
                if not self._restart_server(server_name, self._commands[server_name]):
                    return f"Error: 工具服务 '{server_name}' 不可用，请稍后重试"
                tool = self._find_tool(name)
                if tool is None:
                    return f"Error: 工具 '{name}' 在重启后未发现"
                proc = tool["_proc"]

            try:
                with self._io_lock:
                    result = self._request(proc, "tools/call", {
                        "name": name,
                        "arguments": args,
                    }, timeout=timeout)
                return self._extract_text(result)
            except ConnectionError as e:
                logger.warning(f"[MCP] 调用 {name} 失败(第{attempt + 1}次): {e}")
                if attempt == 0:
                    self._restart_server(server_name, self._commands[server_name])
                    continue
                return f"Error: 工具 '{name}' 调用超时或服务不可用，请稍后重试"
        return f"Error: 工具 '{name}' 调用失败"  # 理论不可达

    def _find_tool(self, name: str) -> Optional[dict]:
        for tool in self.tools:
            if tool["name"] == name:
                return tool
        return None

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
        with self._io_lock:
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
    _client._commands = dict(servers)  # 记住启动命令，供崩溃重启
    for name, command in servers.items():
        _client.connect_server(name, command)
    return _client


def get_mcp() -> MCPSyncClient:
    """获取全局 MCP Client"""
    global _client
    if _client is None:
        raise RuntimeError("MCP Client 未初始化，请先调用 init_mcp()")
    return _client