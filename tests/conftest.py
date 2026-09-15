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

# 分析层（src/analytics/sql.py）用的是 ANALYTICS_DATABASE_URL（生产=只读角色 DSN）。
# 测试必须把它也指到**测试库**，否则会连到开发库跑断言（真实踩过：加了 .env 变量后
# 分析测试开始查开发库，断言全空）。保持角色不变、只换库名，这样测试也覆盖权限模型。
_analytics_dsn = os.environ.get("ANALYTICS_DATABASE_URL", "")
if _analytics_dsn:
    _test_db = TEST_DATABASE_URL.rsplit("/", 1)[-1]
    os.environ["ANALYTICS_DATABASE_URL"] = _analytics_dsn.rsplit("/", 1)[0] + "/" + _test_db
else:
    os.environ["ANALYTICS_DATABASE_URL"] = TEST_DATABASE_URL

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


@pytest.fixture(autouse=True)
async def _reset_l1_cache():
    """每个测试前后清空 L1 热缓存（Redis + 进程内 LRU）。

    ``reset_schema`` 只清 PG；若不一起清缓存，上一个用例/上一轮运行的缓存
    就成了"第二个真相来源" —— 表现为 ``test_user_isolation`` 这类用例
    在缓存热的时候随机失败（旧代码实测复现过）。
    """
    from src.analytics.graph import reset_semantics_cache
    from src.memory import reset_cache
    from src.users import reset_user_cache
    reset_user_cache()          # 影子账号缓存也要清（表被重建后缓存会失真）
    reset_semantics_cache()     # 语义层里的外键关系缓存同理
    await reset_cache()
    yield
    await reset_cache()


@pytest.fixture(scope="session")
def auth_store():
    """鉴权会话存储（Redis）必须可用，否则跳过相关用例。

    登录/刷新是 **fail-closed** 的（Redis 挂了就 503，绝不放行），所以"没有 Redis"
    的环境下这些用例必然失败 —— 那是设计，不是 bug。本地：``brew install redis``
    或 ``docker compose up redis``；CI 里由 services.redis 提供。
    """
    import asyncio
    from src import auth_sessions
    if not asyncio.run(auth_sessions.ping()):
        pytest.skip("鉴权会话需要 Redis（本机未启动 / REDIS_ENABLED=0）")
    return True


@pytest.fixture
async def clean_auth_store(auth_store):
    """每个用例前后清空 auth 命名空间（``study1:auth:*``），避免会话跨用例串台。"""
    from src import auth_sessions, redis_client

    async def _flush():
        client = redis_client.get_client()
        if client is None:
            return
        try:
            async for key in client.scan_iter(match=f"{auth_sessions.AUTH_PREFIX}:auth:*", count=200):
                await client.delete(key)
        except Exception:  # noqa: BLE001  （fail-closed 用例会把 Redis 指向死端口）
            pass

    await _flush()
    yield
    await _flush()


@pytest.fixture(autouse=True)
async def _reset_rate_limits():
    """每个用例前后清空限流桶（``study1:rl:*``）并清进程内版本缓存。

    限流是**跨用例共享状态**（按 IP 计数）：不清理的话，前面用例攒下的登录失败次数
    会把后面的用例判成"账号已锁定"，出现"单独跑绿、整跑红"的经典假故障。
    """
    from src import auth_sessions, rate_limit
    await rate_limit.clear_all()
    auth_sessions.reset_version_cache()
    yield
    await rate_limit.clear_all()
    auth_sessions.reset_version_cache()


@pytest.fixture(autouse=True)
async def _reset_analytics_engine():
    """每个用例前后释放分析层 engine —— 原因同 db engine：连接池绑定事件循环，
    而 pytest-asyncio 每个用例一个新 loop，不释放会跨 loop 报错。"""
    from src.analytics import sql as analytics_sql
    await analytics_sql.dispose_engine()
    yield
    await analytics_sql.dispose_engine()


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