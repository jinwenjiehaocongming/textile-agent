"""异步数据库层 — SQLAlchemy 2.0 async + asyncpg（PostgreSQL）

企业级演进（2026-08）
====================
存储：SQLite（嵌入式，单写者）→ PostgreSQL（独立服务：MVCC 并发写、
      连接池、跨进程/多实例、角色权限与审计）
访问：手写 sqlite3 封装 → SQLAlchemy 2.0 async（连接池 + 命名参数 +
      方言抽象：换库只改 DATABASE_URL）

约定：
- SQL 占位符用 SQLAlchemy 命名式 ``:name``，参数传 dict
- conversations/profile 增加 ``user_id`` 列 —— 多租户从"每用户一个文件"
  升级为"单库 + 行级隔离 + 索引"（企业级标准做法）
"""
import os
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# 开发默认：本机 PostgreSQL（superuser = 当前系统用户）；生产用 .env 覆盖
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://rain@localhost:5432/study1")

_engine: Optional[AsyncEngine] = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            DATABASE_URL,
            pool_size=5,        # 常规连接数
            max_overflow=10,    # 峰值超额连接
            pool_pre_ping=True,  # 取用时先 ping，断线自动重连
        )
    return _engine


def set_engine(engine: Optional[AsyncEngine]) -> None:
    """替换全局引擎（测试注入临时库 / 资源释放用）。"""
    global _engine
    _engine = engine


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


# ---------------------------------------------------------------------------
# 通用查询
# ---------------------------------------------------------------------------


async def query_all(sql: str, params: Optional[dict] = None) -> list[dict[str, Any]]:
    """查询多行，返回 dict 列表。"""
    async with get_engine().connect() as conn:
        result = await conn.execute(text(sql), params or {})
        return [dict(r) for r in result.mappings()]


async def query_one(sql: str, params: Optional[dict] = None) -> Optional[dict[str, Any]]:
    """查询单行。"""
    rows = await query_all(sql, params)
    return rows[0] if rows else None


async def execute(sql: str, params: Optional[dict] = None) -> None:
    """执行写操作（自动事务）。"""
    async with get_engine().begin() as conn:
        await conn.execute(text(sql), params or {})


# ---------------------------------------------------------------------------
# 建表（幂等）
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS products (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    category      TEXT,
    color         TEXT,
    width         INTEGER,
    weight        TEXT,
    stock         INTEGER,
    moq           INTEGER,
    price         REAL,
    delivery_days INTEGER
);

CREATE TABLE IF NOT EXISTS orders (
    id            SERIAL PRIMARY KEY,
    order_no      TEXT UNIQUE NOT NULL,
    customer_id   TEXT NOT NULL,
    product_id    TEXT NOT NULL,
    product_name  TEXT NOT NULL,
    color         TEXT,
    quantity      INTEGER NOT NULL,
    unit_price    REAL NOT NULL,
    total         REAL NOT NULL,
    status        TEXT DEFAULT '待付款',
    created_at    TEXT NOT NULL,
    paid_at       TEXT,
    shipped_at    TEXT,
    phone         TEXT,
    address       TEXT,
    delivery_date TEXT
);

CREATE TABLE IF NOT EXISTS refunds (
    id         SERIAL PRIMARY KEY,
    order_no   TEXT NOT NULL,
    reason     TEXT NOT NULL,
    status     TEXT DEFAULT '待审核',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id         SERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations (user_id, id);

CREATE TABLE IF NOT EXISTS profile (
    user_id    TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
"""


async def ensure_schema() -> None:
    """初始化所有业务表（幂等，启动/测试前调用）。"""
    async with get_engine().begin() as conn:
        for stmt in SCHEMA_SQL.split(";"):
            if stmt.strip():
                await conn.execute(text(stmt.strip()))


async def reset_schema() -> None:
    """清空全部业务表（测试隔离用）。"""
    async with get_engine().begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS products, orders, refunds, conversations, profile CASCADE"))
    await ensure_schema()