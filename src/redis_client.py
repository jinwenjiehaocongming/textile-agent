"""Redis 客户端工厂（单一事实来源）
=================================
两个用途的**失败语义完全相反**，所以这里只负责"拿一个配置正确、绑定当前
event loop 的客户端"，由调用方决定失败怎么办：

| 用途 | 模块 | Redis 挂了 |
|---|---|---|
| L1 热缓存 | ``src/memory.py`` | **fail-open**：回源 PG，业务继续（只降性能） |
| 鉴权会话 | ``src/auth_sessions.py`` | **fail-closed**：拒绝（HTTP 503），绝不放行 |

配置（环境变量，见 .env.example）
================================
``REDIS_URL``              连接串（含密码/库号），默认 redis://localhost:6379/0
``REDIS_ENABLED``          0=完全不使用 Redis
``REDIS_SOCKET_TIMEOUT``   单次连接/读写超时秒数，默认 1.5（防 Redis 卡住拖垮请求）
``REDIS_MAX_CONNECTIONS``  连接池上限，默认 20
"""

import asyncio
import os
from typing import Optional

from src.logging_config import get_logger

logger = get_logger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        logger.warning("环境变量 %s 不是整数，回退默认值 %s", name, default)
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        logger.warning("环境变量 %s 不是数字，回退默认值 %s", name, default)
        return default


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_ENABLED = (os.getenv("REDIS_ENABLED", "1") or "1").strip().lower() not in ("0", "false", "no")
SOCKET_TIMEOUT = _env_float("REDIS_SOCKET_TIMEOUT", 1.5)
REDIS_MAX_CONNECTIONS = max(2, _env_int("REDIS_MAX_CONNECTIONS", 20))

try:  # 未安装 redis 包 → 缓存走进程内 LRU；鉴权直接 503（不允许退化）
    import redis.asyncio as _redis_asyncio
except Exception:  # noqa: BLE001
    _redis_asyncio = None


class RedisUnavailable(RuntimeError):
    """Redis 不可用（未装客户端 / 未开启 / 连不上）。鉴权路径必须把它转成 503。"""


_client = None
_client_loop = None       # 连接池绑定在创建它的 event loop 上，换 loop 必须重建


def available() -> bool:
    """是否具备使用 Redis 的前提（装了库且未被显式关闭）。"""
    return REDIS_ENABLED and _redis_asyncio is not None


def get_client():
    """惰性获取客户端（含连接池）；不可用 → None。

    import 期绝不发起网络请求：旧实现在 import 时 ``ping()`` 且无连接超时，
    对不可达地址会挂到 TCP 超时（分钟级）才启动，还把"启动那一瞬的健康状况"
    冻结成永久配置。
    """
    global _client, _client_loop
    if not available():
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _client is not None and loop is not _client_loop:
        _client = None            # 旧 loop 已关闭：客户端作废（连接随之释放）
    if _client is None:
        try:
            _client = _redis_asyncio.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_timeout=SOCKET_TIMEOUT,
                socket_connect_timeout=SOCKET_TIMEOUT,
                health_check_interval=30,        # 空闲连接自动探活
                max_connections=REDIS_MAX_CONNECTIONS,
            )
            _client_loop = loop
        except Exception as e:  # noqa: BLE001
            logger.warning("Redis 客户端创建失败（%s）: %s", REDIS_URL, e)
            _client = None
    return _client


def require_client():
    """鉴权路径专用：拿不到客户端 → 抛 RedisUnavailable（调用方转 503，fail-closed）。"""
    client = get_client()
    if client is None:
        raise RedisUnavailable(
            "Redis 不可用（未安装 redis 客户端 / REDIS_ENABLED=0 / 连接串无效）"
        )
    return client


def drop_client() -> None:
    """丢弃当前客户端（测试注入新连接串时用；下次调用会按新配置重建）。"""
    global _client, _client_loop
    _client = None
    _client_loop = None
