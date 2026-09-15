"""数据分析 MCP Server（FastMCP · 只读）
======================================
给「管理员数据分析 Agent」提供取数能力，与 product/order/refund 三个业务 Server 并列：

- ``list_tables`` / ``describe_table``：让 LLM 知道有什么表、什么列（本库**没有外键**，
  表关系写在 ``analytics_dict`` 里）
- ``analytics_dict``：业务字典（表关系、状态枚举、GMV/退款率口径、必须避开的坑）
- ``run_sql``：**只读**执行（四层防护见 src/analytics/sql.py）

为什么单独进程：① 权限隔离——这个子进程可以只拿只读 DSN，主进程仍可写；
② 与现有 MCP 工具层叙事一致（客服 Agent 用 product/order/refund，分析 Agent 用 analytics）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# 自己加载 .env：本进程可能被独立拉起（或由没加载过 .env 的父进程 spawn），
# 不加载就会拿到空 DSN → "Could not parse SQLAlchemy URL from given URL string"
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).parent.parent.parent / ".env")

from mcp.server.fastmcp import FastMCP  # noqa: E402

from src.analytics import schema_hints, sql  # noqa: E402
from src.logging_config import get_logger  # noqa: E402

_logger = get_logger(__name__)

mcp = FastMCP("analytics-server")


@mcp.tool()
async def analytics_dict() -> str:
    """业务字典：表关系、状态枚举、指标口径（GMV/退款率/转化率）、以及必须避开的坑。

    **生成 SQL 前必须先看这个** —— 表关系（从数据库外键自动读取）、业务口径、
    时间列类型、必须避开的坑都在这里。
    """
    fk_text = await schema_hints.load_fk_text()
    return schema_hints.semantic_hints(fk_text)


@mcp.tool()
async def list_tables() -> str:
    """列出可查询的业务表及其行数。"""
    try:
        tables = await sql.list_tables()
    except Exception as e:  # noqa: BLE001
        return f"读取表清单失败: {str(e)[:200]}"
    return "\n".join(f"{t['table']}（{t['rows']} 行）" for t in tables) or "（无表）"


@mcp.tool()
async def describe_table(table: str) -> str:
    """查看某张表的列与类型（表名需精确匹配，见 list_tables）。"""
    info = await sql.describe_table(table)
    if not info.get("ok"):
        return info.get("error", "查询失败")
    lines = [f"表 {table} 的列："]
    lines += [f"  - {c['column_name']} ({c['data_type']}{'' if c['is_nullable'] == 'YES' else ', NOT NULL'})"
              for c in info["columns"]]
    return "\n".join(lines)


@mcp.tool()
async def run_sql(query: str) -> str:
    """执行**只读** SQL（必须是单条 SELECT / WITH 查询）并返回文本结果。

    安全限制（违反会被直接拒绝，不会执行）：只允许单条 SELECT/WITH、禁止
    INSERT/UPDATE/DELETE/DDL/系统表/注释，结果行数有上限（超出会提示截断）。
    """
    result = await sql.run_readonly_sql(query)
    if not result.get("ok"):
        return result.get("error", "执行失败")
    head = f"（{result['row_count']} 行，{result['elapsed_ms']}ms）"
    return head + "\n" + sql.to_text_table(result)


if __name__ == "__main__":
    mcp.run()
