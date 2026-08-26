"""
pytest 全局 fixtures（PostgreSQL + Qdrant 版）
==============================================
企业级演进（2026-08）：
- 业务库 fixture：独立测试库 study1_test（与开发库 study1 隔离，不污染真实数据）
- 向量库 fixture：Qdrant 集合清空重建（LocalMode，零外部依赖）
- 原"每用户文件 + chroma collection"的隔离方式被单库 + user_id 行级隔离取代

注意：conftest 在 import src.db 之前设置 DATABASE_URL 指向测试库。
"""
import asyncio
import os

import pytest

from pathlib import Path

# ── 测试库（与开发库隔离）──
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://rain@localhost:5432/study1_test",
)
os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)

PROJECT_ROOT = Path(__file__).parent.parent

# 种子产品（与真实库风格一致）
SEED_PRODUCTS = [
    dict(id="P001", name="T400 复合弹力布", category="弹力布", color="黑色",
         width=150, weight="100D", stock=5000, moq=1000, price=13.2, delivery_days=7),
    dict(id="P002", name="380T 尼丝纺", category="尼丝纺", color="白色",
         width=150, weight="380T", stock=3000, moq=800, price=11.9, delivery_days=5),
]


@pytest.fixture(scope="session", autouse=True)
def ensure_test_db():
    """幂等创建测试库（不存在才创建）。

    凭据从 TEST_DATABASE_URL 解析，兼容本机（rain 无密码）与 CI（postgres/postgres）。
    """
    import asyncpg
    from urllib.parse import urlparse

    _u = urlparse(TEST_DATABASE_URL)
    _dbname = _u.path.lstrip("/") or "study1_test"

    async def _ensure():
        conn = await asyncpg.connect(
            host=_u.hostname or "localhost",
            port=_u.port or 5432,
            user=_u.username or "postgres",
            password=_u.password,
            database="postgres",
        )
        try:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", _dbname)
            if not exists:
                await conn.execute(
                    f'CREATE DATABASE "{_dbname}"')
        finally:
            await conn.close()

    asyncio.run(_ensure())


@pytest.fixture(autouse=True)
async def _reset_db_engine():
    """每个测试前后释放全局 engine —— asyncpg 连接池绑定事件循环，
    而 pytest-asyncio 每个测试使用独立的 loop，不释放会跨 loop 报错。"""
    from src.db import dispose_engine
    await dispose_engine()
    yield
    await dispose_engine()


@pytest.fixture
async def pg_db():
    """空业务库（清表重建 + 种子数据），测试间隔离。"""
    from src.db import execute, reset_schema
    await reset_schema()
    for p in SEED_PRODUCTS:
        await execute(
            """INSERT INTO products (id, name, category, color, width, weight, stock, moq, price, delivery_days)
               VALUES (:id, :name, :category, :color, :width, :weight, :stock, :moq, :price, :delivery_days)""",
            p,
        )
    return None


@pytest.fixture
async def pg_memory(pg_db):
    """记忆表区隔（conversations/profile 已含在 reset_schema 中）。"""
    return pg_db


@pytest.fixture
def clean_qdrant():
    """Qdrant 集合清空（向量相关测试用）。"""
    from src.vector_store import reset_collections
    reset_collections()
    return None