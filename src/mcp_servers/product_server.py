"""产品查询 MCP Server（FastMCP · async）

企业级演进（2026-08）
====================
- 协议：手写 subprocess + JSON-RPC → 官方 MCP SDK（FastMCP 自动完成
  initialize / tools/list / tools/call 握手与参数校验）
- 存储：SQLite 文件 → PostgreSQL（src/db 异步数据库层）
- 原自研 _validate_args 由 FastMCP 内建 schema 校验取代（拒绝未声明字段语义保留）
"""
import sys

from mcp.server.fastmcp import FastMCP

from src.db import query_all

mcp = FastMCP("product-server")


def _compose_search_sql(keywords: list[str]) -> tuple[str, dict]:
    """为关键词列表生成 OR 组合的 LIKE 查询 + 命名参数。"""
    where_parts, params = [], {}
    for i, kw in enumerate(keywords):
        n, c, g = f"n{i}", f"c{i}", f"g{i}"
        where_parts.append(f"(name LIKE :{n} OR color LIKE :{c} OR category LIKE :{g})")
        params[n] = params[c] = params[g] = f"%{kw}%"
    return f"SELECT * FROM products WHERE {' OR '.join(where_parts)} LIMIT 50", params


@mcp.tool()
async def search_product(query: str) -> str:
    """按关键词搜索面料产品（名称/颜色/品类），返回产品报价、库存、MOQ 与交期。"""
    keywords = [kw.strip().lower() for kw in
                query.replace("，", " ").replace(",", " ").split() if kw.strip()]
    if not keywords:
        return "请输入产品名、颜色或品类关键词。"

    # 1) 主查询：关键词直接 LIKE 匹配
    sql, params = _compose_search_sql(keywords)
    rows = await query_all(sql, params)

    # 2) Bigram 回退：整词没命中时拆 2 字片段再查（如"羽绒服"→羽绒/绒服）
    if not rows:
        fragments = {kw[i:i + 2] for kw in keywords if len(kw) > 2 for i in range(len(kw) - 1)}
        if fragments:
            sql2, params2 = _compose_search_sql(sorted(fragments))
            rows = await query_all(sql2, params2)

    if not rows:
        return "未找到匹配产品。请尝试直接用面料名称（如 T400、牛津布、春亚纺、尼丝纺）搜索。"

    # 3) 打分排序：命中关键词越多越靠前，取 Top10
    def _score(r: dict) -> int:
        text = f"{r['name']} {r['color']} {r['category']} {r['weight']} {r['id']}".lower()
        return sum(1 for kw in keywords if kw in text)

    rows.sort(key=_score, reverse=True)

    lines = []
    for r in rows[:10]:
        lines.append(
            f"货号:{r['id']} | {r['name']} | {r['color']} | 门幅:{r['width']}cm "
            f"| {r['weight']} | 库存:{r['stock']}米 | MOQ:{r['moq']}米 "
            f"| ¥{r['price']}/米 | 交期:{r['delivery_days']}天"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    # FastMCP stdio 传输：客户端（MCP Client）以 subprocess 拉起本文件
    sys.exit(mcp.run(transport="stdio"))