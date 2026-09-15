"""分析取数层 —— **只读** SQL 执行（四层防护）
==============================================
LLM 生成的 SQL 是**不可信输入**，不能直接丢给业务库执行。参考实现只做了一层
前缀白名单（``startswith('select'/'with')``），而 ``WITH x AS (DELETE FROM orders
RETURNING *) SELECT * FROM x`` 的前缀正好是 ``with`` —— 能过校验、能删数据。
这里做四层：

| 层 | 手段 | 挡什么 |
|---|---|---|
| ① 账号 | 独立只读 DSN（``ANALYTICS_DATABASE_URL``，PG 角色只有 SELECT 权限） | 就算语句被绕过，数据库也不让写 |
| ② 连接 | ``default_transaction_read_only=on`` + ``statement_timeout`` | 兜底防写 + 防慢查询拖垮库 |
| ③ 语句 | 单语句、SELECT/WITH 开头、关键字黑名单、禁系统表、**外层强制 LIMIT** | 挡住危险/超量查询 |
| ④ 结果 | 行数上限 + 单元格截断 + 耗时统计 | 防"查了 50 万行打爆内存" |

降级说明：本地没配 ``ANALYTICS_DATABASE_URL`` 时用业务 DSN，但②③④仍然生效；
生产**必须**配只读角色（``python scripts/create_analytics_role.py`` 可生成）。
"""

import asyncio
import os
import re
import time
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.logging_config import get_logger

logger = get_logger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        logger.warning("环境变量 %s 不是整数，回退默认值 %s", name, default)
        return default


def analytics_dsn() -> str:
    """只读 DSN：生产指向只有 SELECT 权限的角色；未配置则回退业务 DSN（仍有②③④兜底）。

    **惰性读取**（不在 import 时定死）：否则环境变量的加载顺序一变（比如 MCP 子进程
    还没 load_dotenv 就 import 了本模块）就会拿到空串，报 "Could not parse SQLAlchemy URL"。
    """
    return os.getenv("ANALYTICS_DATABASE_URL", "") or os.getenv("DATABASE_URL", "")
MAX_ROWS = max(1, _env_int("ANALYTICS_MAX_ROWS", 2000))
TIMEOUT_MS = max(1000, _env_int("ANALYTICS_TIMEOUT_MS", 15000))
CELL_MAX_LEN = max(32, _env_int("ANALYTICS_CELL_MAX_LEN", 200))

# 只读语句必须以此开头（大小写不敏感）
_ALLOWED_START = ("select", "with")
# 关键字黑名单：即使写在子查询/CTE 里也不允许
_FORBIDDEN = (
    "insert", "update", "delete", "drop", "alter", "create", "truncate", "grant", "revoke",
    "comment", "copy", "vacuum", "analyze", "reindex", "cluster", "refresh",
    "call", "do", "execute", "prepare", "deallocate", "listen", "notify", "lock",
    "set", "reset", "begin", "commit", "rollback", "savepoint", "discard",
    "pg_read_file", "pg_ls_dir", "pg_sleep", "lo_import", "lo_export", "dblink",
)
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN) + r")\b", re.IGNORECASE)
_SYSTEM_TABLE_RE = re.compile(r"\b(pg_[a-z_]+|information_schema)\b", re.IGNORECASE)


class SqlRejected(Exception):
    """语句被安全策略拒绝（未执行）。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_engine: Optional[AsyncEngine] = None


def get_engine() -> AsyncEngine:
    """惰性建只读 engine：连接级就设成只读事务 + 语句超时。"""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            analytics_dsn(),
            pool_size=2,
            max_overflow=2,
            pool_pre_ping=True,
            connect_args={"server_settings": {
                "default_transaction_read_only": "on",
                "statement_timeout": str(TIMEOUT_MS),
                "application_name": "study1-analytics",
            }},
        )
    return _engine


def set_engine(engine) -> None:
    """测试注入用。"""
    global _engine
    _engine = engine


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


def validate(sql: str) -> str:
    """语句层校验（③）。返回清洗后的 SQL；不合规抛 ``SqlRejected``。"""
    q = (sql or "").strip().rstrip(";").strip()
    if not q:
        raise SqlRejected("SQL 为空")
    if not q.lower().startswith(_ALLOWED_START):
        raise SqlRejected("只允许只读查询（必须以 SELECT 或 WITH 开头）")
    # 单语句：去掉结尾分号后，正文里不允许再出现分号（挡 "select 1; drop table x"）
    if ";" in q:
        raise SqlRejected("只允许单条语句（检测到分号）")
    if "--" in q or "/*" in q:
        raise SqlRejected("不允许 SQL 注释（可能用于绕过关键字检查）")
    hit = _FORBIDDEN_RE.search(q)
    if hit:
        raise SqlRejected(f"语句包含被禁止的关键字：{hit.group(0).upper()}")
    sys_tbl = _SYSTEM_TABLE_RE.search(q)
    if sys_tbl:
        raise SqlRejected(f"不允许访问系统表/视图：{sys_tbl.group(0)}（表结构请用 describe_table）")
    return q


def wrap_with_limit(sql: str, limit: int = MAX_ROWS) -> str:
    """外层强制包一层 LIMIT：即便 LLM 写了无 LIMIT 的查询，也不会拉全表。"""
    return f"SELECT * FROM (\n{sql}\n) AS _analytics_sub LIMIT {int(limit)}"


def _cell(value: Any) -> Any:
    """结果单元格转成可 JSON 序列化的类型，并做长度截断。"""
    if value is None or isinstance(value, (int, float, bool, str)):
        s = value
    elif hasattr(value, "isoformat"):
        s = value.isoformat()
    else:
        s = str(value)           # Decimal / UUID / bytes ...
        try:
            s = float(value)     # Decimal → float（图表友好）
        except (TypeError, ValueError):
            pass
    if isinstance(s, str) and len(s) > CELL_MAX_LEN:
        return s[:CELL_MAX_LEN] + "…"
    return s


async def run_readonly_sql(sql: str, limit: int = MAX_ROWS) -> dict:
    """执行只读查询。

    返回 ``{ok, columns, rows, row_count, truncated, elapsed_ms, sql, error}``；
    **不抛异常**（把失败当结果返回，方便上层喂回 LLM 让它自纠错）。
    """
    try:
        clean = validate(sql)
    except SqlRejected as e:
        return {"ok": False, "error": f"安全策略拒绝：{e.reason}", "columns": [], "rows": [],
                "row_count": 0, "truncated": False, "elapsed_ms": 0, "sql": sql}

    final_limit = max(1, min(int(limit or MAX_ROWS), MAX_ROWS))
    # 包 limit+1 行：多出来的那一行专门用来判断"是否被截断"（否则 DB 只返回 limit 行，
    # 永远看不出还有更多数据）
    wrapped = wrap_with_limit(clean, final_limit + 1)
    started = time.perf_counter()
    try:
        async with get_engine().connect() as conn:
            result = await conn.execute(text(wrapped))
            cols = list(result.keys())
            raw = result.fetchmany(final_limit + 1)      # 多取一行用于判断是否被截断
            truncated = len(raw) > final_limit
            rows = [[_cell(v) for v in row] for row in raw[:final_limit]]
    except Exception as e:  # noqa: BLE001
        elapsed = int((time.perf_counter() - started) * 1000)
        msg = str(e).split("\n")[0][:300]
        logger.warning("[分析] SQL 执行失败（%sms）：%s", elapsed, msg)
        return {"ok": False, "error": f"SQL 执行失败: {msg}", "columns": [], "rows": [],
                "row_count": 0, "truncated": False, "elapsed_ms": elapsed, "sql": clean}
    elapsed = int((time.perf_counter() - started) * 1000)
    return {"ok": True, "columns": cols, "rows": rows, "row_count": len(rows),
            "truncated": truncated, "elapsed_ms": elapsed, "sql": clean}


def to_text_table(result: dict, max_rows: int = 50) -> str:
    """把结果转成紧凑文本（喂给 LLM 做结论 / 也方便人看）。"""
    if not result.get("ok"):
        return f"（执行失败）{result.get('error')}"
    cols, rows = result["columns"], result["rows"]
    if not rows:
        return "（空结果）"
    lines = [" | ".join(cols)]
    for row in rows[:max_rows]:
        lines.append(" | ".join("" if v is None else str(v) for v in row))
    if len(rows) > max_rows:
        lines.append(f"...（共 {len(rows)} 行，已截断显示前 {max_rows} 行）")
    if result.get("truncated"):
        lines.append(f"⚠️ 结果超过上限 {MAX_ROWS} 行，已截断（建议加聚合或缩小范围）")
    return "\n".join(lines)


# ── 表结构自省（供 describe_table / prompt 用）────────────────

async def list_tables() -> list[dict]:
    """列出业务表 + 行数（不含 系统表）。"""
    async def _op():
        async with get_engine().connect() as conn:
            res = await conn.execute(text("""
                SELECT c.relname AS table_name, c.reltuples::bigint AS approx_rows
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind = 'r'
                ORDER BY c.relname"""))
            return [dict(r) for r in res.mappings()]

    rows = await _op()
    # reltuples 是估算值；小表用真实 count 更准
    out = []
    for r in rows:
        try:
            async with get_engine().connect() as conn:
                res = await conn.execute(text(f'SELECT count(*) AS n FROM "{r["table_name"]}"'))
                r["rows"] = res.scalar()
        except Exception:  # noqa: BLE001
            r["rows"] = r["approx_rows"]
        out.append({"table": r["table_name"], "rows": r["rows"]})
    return out


async def describe_table(table: str) -> dict:
    """某张表的列/类型/可空性（table 名走白名单校验，防注入）。"""
    names = {t["table"] for t in await list_tables()}
    if table not in names:
        return {"ok": False, "error": f"表不存在：{table!r}（可用：{', '.join(sorted(names))}）"}
    async with get_engine().connect() as conn:
        res = await conn.execute(text("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=:t ORDER BY ordinal_position"""),
            {"t": table})
        cols = [dict(r) for r in res.mappings()]
    return {"ok": True, "table": table, "columns": cols}
