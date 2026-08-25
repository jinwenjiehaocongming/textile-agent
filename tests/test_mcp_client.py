"""MCP Client 线程安全测试（P0-2 验收）

- 并发调用：请求/响应一一对应，不串线
- 崩溃自愈：子进程被杀后下一次调用自动重启 + 重试成功

用独立 MCPSyncClient 实例（不碰全局单例），避免影响 app 运行。
"""
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.mcp_client import MCPSyncClient

PROJECT_ROOT = Path(__file__).parent.parent
PRODUCT_CMD = [sys.executable, "src/mcp_servers/product_server.py"]
ORDER_CMD = [sys.executable, "src/mcp_servers/order_server.py"]
REFUND_CMD = [sys.executable, "src/mcp_servers/refund_server.py"]

KEYWORD_CASES = [
    ("T400", "T400"),
    ("尼丝纺", "尼丝纺"),
    ("牛津布", "牛津布"),
    ("春亚纺", "春亚纺"),
]


def _new_client() -> MCPSyncClient:
    c = MCPSyncClient()
    c._commands = {"product": PRODUCT_CMD, "order": ORDER_CMD, "refund": REFUND_CMD}
    c.connect_server("product", PRODUCT_CMD)
    c.connect_server("order", ORDER_CMD)
    c.connect_server("refund", REFUND_CMD)
    return c


def test_concurrent_calls_no_cross_wiring():
    """20 线程 × 每线程查不同关键词：结果必须包含自己的关键词（无串线）。"""
    client = _new_client()
    try:
        def worker(i: int) -> tuple:
            kw, expect = KEYWORD_CASES[i % len(KEYWORD_CASES)]
            for _ in range(10):
                result = client.call_tool("search_product", {"query": kw})
                if expect not in result:
                    return (i, kw, result[:80])
            return (i, kw, "OK")

        with ThreadPoolExecutor(max_workers=20) as ex:
            results = list(ex.map(worker, range(200)))

        bad = [r for r in results if r[2] != "OK"]
        assert bad == [], f"{len(bad)} 次调用串线: {bad[:3]}"
    finally:
        client.shutdown()


def test_crash_auto_restart_and_retry():
    """杀掉 product 子进程后，下一次调用应自动重启并成功返回。"""
    client = _new_client()
    try:
        proc = client.servers["product"]
        proc.terminate()
        proc.wait(timeout=5)
        assert proc.poll() is not None  # 确认已死

        # 下一次调用：应检测到死进程 → 重启 → 重试 → 成功
        result = client.call_tool("search_product", {"query": "T400"})
        assert "T400" in result, f"重启后调用失败: {result[:80]}"
        assert client.servers["product"].poll() is None  # 新进程活着
    finally:
        client.shutdown()


def test_dead_process_clean_error_not_hang():
    """进程死掉时调用应在超时内返回错误文案，而不是永久阻塞。"""
    client = _new_client()
    try:
        # 把重启也禁用（模拟重启失败场景），验证不发生永久挂起
        proc = client.servers["product"]
        proc.terminate()
        proc.wait(timeout=5)
        client._commands["product"] = [sys.executable, "-c", "import sys; sys.exit(3)"]

        import time
        t0 = time.time()
        result = client.call_tool("search_product", {"query": "T400"}, timeout=5)
        elapsed = time.time() - t0
        assert elapsed < 20, f"调用挂起 {elapsed:.1f}s"
        assert "不可用" in result or "失败" in result or "重启失败" in result
    finally:
        client.shutdown()