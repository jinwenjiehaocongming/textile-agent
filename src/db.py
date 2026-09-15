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
from contextlib import asynccontextmanager
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.logging_config import get_logger

logger = get_logger(__name__)

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
# 所有访问都过这层：外部 DDL（迁移脚本、别的实例建表）会让**连接池里缓存的
# prepared statement 失效**，紧接着的查询会抛 InvalidCachedStatementError。
# 实测过：跑完迁移脚本后，运行中的服务第一条查询 500、第二条才正常 ——
# 在滚动发版里就是"旧实例突然吐一批 500"。所以这里统一"识别到即重建连接池 + 重试一次"。
_STALE_STMT_HINT = "cached statement plan is invalid"


async def _with_stale_stmt_retry(op, what: str):
    try:
        return await op()
    except DBAPIError as e:
        if _STALE_STMT_HINT not in str(e):
            raise
        logger.warning("[db] prepared statement 缓存失效（外部 DDL？），重建连接池后重试一次：%s", what)
        await dispose_engine()
        return await op()


async def query_all(sql: str, params: Optional[dict] = None) -> list[dict[str, Any]]:
    """查询多行，返回 dict 列表。"""

    async def _op():
        async with get_engine().connect() as conn:
            result = await conn.execute(text(sql), params or {})
            return [dict(r) for r in result.mappings()]

    return await _with_stale_stmt_retry(_op, sql.strip().split()[0])


async def query_one(sql: str, params: Optional[dict] = None) -> Optional[dict[str, Any]]:
    """查询单行。"""
    rows = await query_all(sql, params)
    return rows[0] if rows else None


async def execute(sql: str, params: Optional[dict] = None) -> None:
    """执行写操作（自动事务）。"""

    async def _op():
        async with get_engine().begin() as conn:
            await conn.execute(text(sql), params or {})

    await _with_stale_stmt_retry(_op, sql.strip().split()[0])


@asynccontextmanager
async def transaction():
    """多语句单事务（"要么都成"的写操作用）。

    为什么需要：退款工单与订单状态必须一起变（`src/order_flow.py`）——
    用工单自己的事务 + 订单自己的事务，中间崩一次就会留下
    "工单已建、订单没动"（审完什么都没发生的老问题）或
    "订单进了退款中、却没有工单可审"（订单被永久冻结）。

    用法：``async with transaction() as conn: await conn.execute(text(...), {...})``
    """
    async with get_engine().begin() as conn:
        yield conn


async def execute_many(sql: str, params_list: list[dict[str, Any]]) -> None:
    """批量写（executemany，单事务）。

    一条 INSERT N 次 → N 条参数一次提交：把「每条消息一个事务」压成 1 个，
    减少往返与锁持有时间（save_messages 批量存档用）。
    """
    if not params_list:
        return

    async def _op():
        async with get_engine().begin() as conn:
            await conn.execute(text(sql), params_list)

    await _with_stale_stmt_retry(_op, sql.strip().split()[0])


async def execute_returning(sql: str, params: Optional[dict] = None) -> Optional[dict[str, Any]]:
    """需要 ``RETURNING`` 的写操作（走 ``begin()`` 提交，返回第一行或 None）。

    ⚠️ 别用 ``query_one`` 干这件事：它走的是 ``connect()``，**事务不会提交**
    （SQLAlchemy 2.0 commit-as-you-go，连接关闭即回滚）—— 结果是"UPDATE 执行了、
    接口返回 200、数据库其实没变"。这个坑真实踩过：改密码/封号的版本号自增被静默回滚，
    封号形同虚设。
    """
    async def _op():
        async with get_engine().begin() as conn:
            result = await conn.execute(text(sql), params or {})
            row = result.mappings().first()
            return dict(row) if row else None

    return await _with_stale_stmt_retry(_op, sql.strip().split()[0])


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
    delivery_date TEXT,
    -- 退款到账时间（2026-09 退单联动）：分析里的"退款时效"靠它，之前退款跟订单毫无关联
    refunded_at   TEXT
);

-- 老库补列（幂等）。⚠️ 顺序：必须在 CREATE 之后，空库初始化时表还不存在
ALTER TABLE orders ADD COLUMN IF NOT EXISTS refunded_at TEXT;

CREATE TABLE IF NOT EXISTS refunds (
    id         SERIAL PRIMARY KEY,
    order_no   TEXT NOT NULL,
    reason     TEXT NOT NULL,
    status     TEXT DEFAULT '待审核',
    created_at TEXT NOT NULL,
    -- 审核字段（2026-09 管理端）：工单由退款 Agent 创建，之前**没有任何接口能审它**，
    -- 所以连"谁审的、什么时候审的、为什么这么审"都没地方存。
    decided_at TEXT,
    decided_by TEXT,
    note       TEXT,
    -- 发起退款时订单的状态（2026-09 退单联动）：驳回时要**原样退回**，
    -- 不记就只能猜，猜错就是把业务数据改坏（详见 src/order_flow.py）
    order_status_before TEXT
);

-- 老库补列（幂等）。⚠️ 必须放在 CREATE TABLE **之后**：放前面时新库首次初始化会
-- "relation refunds does not exist" 直接失败（开发库因表已存在而掩盖了这个顺序错误）。
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS decided_at TEXT;
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS decided_by TEXT;
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS note TEXT;
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS order_status_before TEXT;

CREATE TABLE IF NOT EXISTS conversations (
    id         SERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT 'default',  -- 多会话隔离（2026-09）
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations (user_id, id);

-- 老库升级（幂等）：无 session_id 列的 conversations 补列（存量数据归入 'default'）
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS session_id TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_conversations_session ON conversations (user_id, session_id, id);

CREATE TABLE IF NOT EXISTS profile (
    user_id    TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         SERIAL PRIMARY KEY,
    actor      TEXT NOT NULL,      -- 谁：admin["sub"] / 用户标识
    action     TEXT NOT NULL,      -- approve / reject / login / ...
    thread_id  TEXT,               -- 关联的会话（订单审批 thread_id）
    detail     TEXT,               -- 理由、摘要
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log (actor, id);

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,      -- 对外 user_id（uuid4 hex，无横线）
    username      TEXT UNIQUE NOT NULL,  -- 登录名（小写归一化）
    password_hash TEXT NOT NULL,         -- bcrypt 哈希，绝不存明文
    display_name  TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL DEFAULT 'customer',  -- customer | admin
    status        TEXT NOT NULL DEFAULT 'active',    -- active | disabled
    created_at    TEXT NOT NULL
);

-- 凭证版本（2026-09）：改密码/封号时 +1 → 之前签发的 access token 立刻失效。
-- 这是"秒级吊销"的落点：access 无状态本来不可撤，靠"版本对不上"来间接判定失效。
-- 老库升级（幂等）
ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0;

-- 多会话（2026-09）：每个用户可新建多个对话，历史按 session 隔离
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,      -- session_id（uuid4 hex）
    user_id    TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '新对话',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id, updated_at);

-- ═══════════════════════════════════════════════════════════════
-- HITL 待审批订单（2026-09 方案 B：从进程内存迁到 PG）
-- ═══════════════════════════════════════════════════════════════
-- 为什么要有这张表：在此之前"谁在等审批"只活在 approval.py 的进程内 dict +
-- LangGraph MemorySaver 里，而"审批通过 → 真正写订单"又完全依赖那份内存状态。
-- 于是一次发版/重启 = 客户拿到"已提交人工审批"的承诺，管理员列表却空了，
-- 订单永远不会生成，且**全链路无痕迹**（dict 消失没有日志、没有审计）。
--
-- 这张表是业务真相来源：列表、状态机、超时、审计、幂等全部以它为准，
-- 图 checkpoint 只决定"能不能保留客户续轮的上下文"，不再决定订单能不能生成。
CREATE TABLE IF NOT EXISTS pending_approvals (
    id         TEXT PRIMARY KEY,                     -- uuid4 hex（也是写单幂等键）
    thread_id  TEXT NOT NULL,                        -- = user_id（图的 thread）
    user_id    TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    draft      JSONB NOT NULL,                       -- 给管理员看的确认单
    args       JSONB NOT NULL DEFAULT '{}'::jsonb,    -- create_order 的原始入参（重启后据此直接写单）
    status     TEXT NOT NULL DEFAULT 'pending',       -- pending|approved|rejected|expired
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    decided_at TIMESTAMPTZ,
    decided_by TEXT,
    reason     TEXT,
    order_no   TEXT,
    resumed    BOOLEAN NOT NULL DEFAULT FALSE         -- 是否由图 resume 路径完成（否则为兜底直接写单）
);
-- 同一线程只允许一条 pending（并发确认也不会堆单）；过期/已判定的行不参与冲突
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_approvals_thread
    ON pending_approvals (thread_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_pending_approvals_status
    ON pending_approvals (status, created_at);
CREATE INDEX IF NOT EXISTS idx_pending_approvals_user
    ON pending_approvals (user_id, created_at DESC);

-- 订单幂等键：审批兜底路径可能重试（进程在写单与记账之间挂掉），
-- 靠它保证"同一次审批最多生成一笔订单"（部分唯一索引，历史订单不受影响）
ALTER TABLE orders ADD COLUMN IF NOT EXISTS client_request_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_client_request
    ON orders (client_request_id) WHERE client_request_id IS NOT NULL;

-- ── 业务索引（2026-09 数据完整性治理）──
-- orders 之前除主键/唯一键没有任何索引："我的订单"按 customer_id 查是全表扫描；
-- 而且外键要求在引用列上有索引，否则删父行会全表扫子表。
CREATE INDEX IF NOT EXISTS idx_orders_customer   ON orders (customer_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_orders_product    ON orders (product_id);
CREATE INDEX IF NOT EXISTS idx_orders_created    ON orders (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status     ON orders (status);
CREATE INDEX IF NOT EXISTS idx_refunds_order     ON refunds (order_no);
CREATE INDEX IF NOT EXISTS idx_conversations_user_only ON conversations (user_id);
CREATE INDEX IF NOT EXISTS idx_profile_user      ON profile (user_id);
CREATE INDEX IF NOT EXISTS idx_pending_approvals_thread ON pending_approvals (thread_id);
"""


# ── 外键（2026-09 数据完整性治理）──────────────────────────────
# 为什么现在才加：在此之前 63 个 user_id 在业务数据里出现、users 里却没有行
# （guest 兜底、/dev/login 的 mock、HITL 演示账号、历史自由文本标识）。
# 加约束前先立起"所有被接受的身份都有账号行"（users.ensure_user_row），再清历史孤儿。
#
# 删除语义是刻意选的：
#   CASCADE  —— 子数据本就从属于父（聊天消息属于会话、退款属于订单、影子身份的业务数据
#               属于该身份）。注意 orders 用 RESTRICT 而不是 CASCADE：**删用户不能连订单一起删**，
#               订单是对账与审计对象，必须逼人先归档。
#   RESTRICT —— 有下游数据就不许删（产品、客户），把"误删"变成显式操作。
# audit_log **刻意不加外键**：它记录"谁做了什么"，必须能写 system_data_fix / unknown /
#   已删除账号——加了外键会让审计在最需要它的时候写不进去。
_FOREIGN_KEYS = [
    # (子表, 子列, 父表, 父列, 删除动作)
    ("refunds", "order_no", "orders", "order_no", "CASCADE"),
    ("orders", "product_id", "products", "id", "RESTRICT"),
    ("orders", "customer_id", "users", "id", "RESTRICT"),
    ("sessions", "user_id", "users", "id", "CASCADE"),
    # 刻意不加 conversations.session_id → sessions.id：项目设计里 'default' 是**跨用户的
    # 历史遗留桶**（sessions.py 明确写了它不属于 sessions 表），外键表达不了这个语义。
    # 教训：外键要编码"真实不变式"，不能编码"我们希望的不变式"——否则会跟既有设计打架。
    ("conversations", "user_id", "users", "id", "CASCADE"),
    ("profile", "user_id", "users", "id", "CASCADE"),
    ("pending_approvals", "user_id", "users", "id", "CASCADE"),
]


async def _apply_foreign_keys() -> None:
    """幂等加外键：先 ADD ... NOT VALID（不扫全表、不长时间持锁），再 VALIDATE。

    两步法是生产常规做法：NOT VALID 立刻生效于**新写入**，VALIDATE 单独校验存量数据，
    中间那段时间旧数据仍可读，不会把写服务卡住。
    """
    added = []
    for child, col, parent, ref_col, on_delete in _FOREIGN_KEYS:
        name = f"fk_{child}_{col}"
        row = await query_one(
            "SELECT convalidated FROM pg_constraint WHERE conname = :n", {"n": name})
        if row and row["convalidated"]:
            continue
        if row:
            # 约束已存在但没通过校验（上次加的时候存量还有孤儿）→ 补一次 VALIDATE。
            # ⚠️ 初版这里直接 continue，结果清完孤儿也永远校验不上（外键一直挂着 NOT VALID）。
            try:
                await execute(f'ALTER TABLE {child} VALIDATE CONSTRAINT "{name}"')
                logger.info("[db] 外键校验完成：%s", name)
                added.append(name)
            except Exception as e:  # noqa: BLE001
                logger.error("[db] 外键 %s 仍校验不过（存量还有孤儿）：%s", name,
                             str(e).split("\n")[0][:140])
            continue
        try:
            await execute(
                f'ALTER TABLE {child} ADD CONSTRAINT "{name}" FOREIGN KEY ({col}) '
                f'REFERENCES {parent} ({ref_col}) ON DELETE {on_delete} NOT VALID')
            await execute(f'ALTER TABLE {child} VALIDATE CONSTRAINT "{name}"')
            logger.info("[db] 外键已加：%s.%s → %s.%s (ON DELETE %s)",
                        child, col, parent, ref_col, on_delete)
            added.append(name)
        except Exception as e:  # noqa: BLE001
            # 存量数据里还有孤儿时 VALIDATE 会失败：把 NOT VALID 约束保留（新数据已受约束），
            # 并打出可执行的清理提示，而不是让服务起不来
            logger.error("[db] 外键 %s 校验未通过（存量数据有孤儿）：%s —— "
                         "执行 python scripts/cleanup_orphans.py --apply 后重启即可完成校验",
                         name, str(e).split("\n")[0][:160])
    if added:
        await dispose_engine()


async def ensure_schema() -> None:
    """初始化所有业务表（幂等，启动/测试前调用）。

    分两步：① 建表/加列（纯 SQL，用 ``;`` 拆分即可）；② **需要判断当前类型的迁移**
    走 Python 侧（见 ``_apply_type_migrations``）—— 因为 ``DO $$ ... $$`` 块里也带分号，
    用现在的拆分方式会被切碎。
    """
    async with get_engine().begin() as conn:
        for stmt in SCHEMA_SQL.split(";"):
            if stmt.strip():
                await conn.execute(text(stmt.strip()))
    await _apply_type_migrations()
    await _apply_foreign_keys()


# 金额列：REAL(=float4) → numeric。为什么必须改：
# float4 只有约 7 位有效数字，实测 14.2 存进去变成 14.199999809265137、
# 13800000001 变成 13800000512 —— 订单金额是对外报价与对账依据，容不得这种误差；
# 而且分析类查询 SUM(total) 会把这些噪声放大成"看起来像真数字的假数字"。
# USING round(x::numeric, 2)：迁移的同时把已存在的浮点噪声洗干净。
_TYPE_MIGRATIONS = [
    ("products", "price", "numeric(12,2)"),
    ("orders", "unit_price", "numeric(12,2)"),
    ("orders", "total", "numeric(14,2)"),
]


async def _apply_type_migrations() -> None:
    """按需执行列类型迁移（幂等：只在当前类型不是目标时动手）。

    ⚠️ 两个真实细节：
    1. DDL 会让**连接池里已缓存的 prepared statement 失效**，紧接着的第一条查询会抛
       ``InvalidCachedStatementError``（实测踩过）。所以迁移后要 dispose 连接池，
       否则生产里表现就是"改完表结构第一个请求 500"。
    2. 迁移失败不该让服务起不来：记 ERROR 后继续（没改成只是精度问题，
       比整站起不来轻）。
    """
    changed = False
    for table, column, target in _TYPE_MIGRATIONS:
        row = await query_one(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c",
            {"t": table, "c": column},
        )
        if not row or row["data_type"] == "numeric":
            continue
        try:
            await execute(
                f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {target} "
                f"USING round({column}::numeric, 2)"
            )
            logger.info("[db] 列类型迁移：%s.%s %s → %s",
                        table, column, row["data_type"], target)
            changed = True
        except Exception as e:  # noqa: BLE001
            logger.error("[db] 列类型迁移失败 %s.%s → %s: %s", table, column, target, e)
    if changed:
        await dispose_engine()      # 丢掉带着失效 prepared statement 的连接


async def reset_schema() -> None:
    """清空全部业务表（测试隔离用）。"""
    async with get_engine().begin() as conn:
        await conn.execute(text(
            "DROP TABLE IF EXISTS pending_approvals, sessions, users, products, "
            "orders, refunds, conversations, profile CASCADE"))
    from src.users import reset_user_cache   # 懒加载：db ↔ users 互相引用
    reset_user_cache()                       # 表都重建了，影子账号缓存必须一起失效
    await ensure_schema()