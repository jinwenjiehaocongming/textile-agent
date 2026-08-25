"""
产品查询 MCP Server
===================
独立的工具服务器，通过 JSON-RPC over stdio 与 Agent 通信。
可以被任何 MCP Client 连接——agent.py、order_agent.py、甚至外部系统。

启动方式（由 MCP Client 自动启动子进程，不需要手动运行）：
  python src/mcp_servers/product_server.py

协议：从 stdin 读 JSON-RPC 请求，向 stdout 写 JSON-RPC 响应。
"""

import json
import sys
from pathlib import Path

try:
    from sqlite_utils import query_all  # 纯脚本运行（sys.path[0] = src/mcp_servers/）
except ImportError:
    from src.mcp_servers.sqlite_utils import query_all  # 包方式导入（测试/项目内）

DB_PATH = Path(__file__).parent.parent.parent / "data" / "products.db"


def search_product(query: str) -> str:
    """查询产品库，按关键词匹配数排序"""
    keywords = [kw.strip().lower() for kw in
                query.replace("，", " ").replace(",", " ").split() if kw.strip()]
    if not keywords:
        return "请输入产品名、颜色或品类关键词。"

    try:
        where_parts = []
        params = []
        for kw in keywords:
            p = f"%{kw}%"
            where_parts.append("(name LIKE ? OR color LIKE ? OR category LIKE ?)")
            params.extend([p, p, p])

        rows = query_all(
            DB_PATH,
            f"SELECT * FROM products WHERE {' OR '.join(where_parts)} LIMIT 50",
            params,
        )

        if not rows:
            # Bigram 回退
            fragments = set()
            for kw in keywords:
                if len(kw) > 2:
                    for i in range(len(kw) - 1):
                        fragments.add(kw[i:i + 2])
            if fragments:
                where_parts = []
                params = []
                for fg in fragments:
                    p = f"%{fg}%"
                    where_parts.append("(name LIKE ? OR color LIKE ? OR category LIKE ?)")
                    params.extend([p, p, p])
                rows = query_all(
                    DB_PATH,
                    f"SELECT * FROM products WHERE {' OR '.join(where_parts)} LIMIT 50",
                    params,
                )

        if not rows:
            return "未找到匹配产品。请尝试直接用面料名称（如 T400、牛津布、春亚纺、尼丝纺）搜索。"

        scored = []
        for r in rows:
            text = f"{r['name']} {r['color']} {r['category']} {r['weight']} {r['id']}".lower()
            score = sum(1 for kw in keywords if kw in text)
            scored.append((score, r))

        scored.sort(key=lambda x: x[0], reverse=True)
        top10 = [r for _, r in scored[:10]]

        lines = []
        for r in top10:
            lines.append(
                f"货号:{r['id']} | {r['name']} | {r['color']} | 门幅:{r['width']}cm "
                f"| {r['weight']} | 库存:{r['stock']}米 | MOQ:{r['moq']}米 "
                f"| ¥{r['price']}/米 | 交期:{r['delivery_days']}天"
            )
        return "\n".join(lines)
    except Exception:
        return "产品查询暂时不可用，请稍后重试或联系销售经理。"


# ============================================================
# JSON Schema 参数校验 — 不依赖外部库，自己实现轻量版
# ============================================================

# 工具 Schema 注册表（和 tools/list 返回的保持一致）
TOOL_SCHEMAS = {
    "search_product": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    },
}


def _validate_args(tool_name, args):
    """
    校验工具参数是否符合 Schema。返回错误信息，校验通过返回 None。

    检查项：
    1. 必填字段是否存在
    2. 字段类型是否匹配
    3. 不允许未声明的字段（避免 LLM 幻觉传多余参数）
    """
    schema = TOOL_SCHEMAS.get(tool_name)
    if schema is None:
        return None  # 没有 Schema 定义就放行

    props = schema.get("properties", {})
    required = schema.get("required", [])

    # 1. 检查必填字段
    for field in required:
        if field not in args or args[field] is None:
            return f"参数校验失败: 缺少必填字段 '{field}'"

    # 2. 检查类型
    type_map = {"string": str, "integer": int, "number": (int, float), "boolean": bool}
    for field, value in args.items():
        if field not in props:
            return f"参数校验失败: 未声明的字段 '{field}'"
        expected = props[field].get("type")
        if expected and expected in type_map:
            if not isinstance(value, type_map[expected]):
                return (
                    f"参数校验失败: 字段 '{field}' 期望 {expected} 类型，"
                    f"实际收到 {type(value).__name__} 类型 (值: {repr(value)})"
                )

    return None


# ============================================================
# MCP Server 主循环 — JSON-RPC over stdio
# ============================================================

def main():
    """从 stdin 读请求，处理后写到 stdout。死循环，直到 stdin 关闭。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = request.get("method", "")
        req_id = request.get("id")
        params = request.get("params", {})

        if method == "initialize":
            _respond(req_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "product-server", "version": "1.0.0"},
            })

        elif method == "tools/list":
            _respond(req_id, {
                "tools": [{
                    "name": "search_product",
                    "description": "查询产品库存和报价。用于客户询问价格、库存、MOQ、交期，"
                                   "按名称/颜色/品类搜面料，对比不同产品。",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "产品名、颜色或品类关键词，如 'T400 黑色'、'牛津布'、'里料'",
                            },
                        },
                        "required": ["query"],
                    },
                }],
            })

        elif method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            if tool_name == "search_product":
                error = _validate_args(tool_name, tool_args)
                if error:
                    result_text = error
                else:
                    result_text = search_product(**tool_args)
            else:
                result_text = f"未知工具: {tool_name}"
            _respond(req_id, {
                "content": [{"type": "text", "text": result_text}],
            })

        # notifications/initialized 没有 id，不需要回应


def _respond(req_id, result):
    """发送 JSON-RPC 响应到 stdout"""
    response = {"jsonrpc": "2.0", "result": result, "id": req_id}
    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
