"""用户记忆 — 三层存储（异步版）

企业级演进（2026-08）
====================
Layer 1: 热缓存 — Redis（毫秒级，``redis.asyncio`` + 连接池，不阻塞事件循环）
Layer 2: PostgreSQL（src/db）— 对话历史永久存档；多租户从"每用户一个文件"
         升级为单库 + user_id 行级隔离 + 索引
Layer 3: Qdrant（src/vector_store）— LLM 提取的长期偏好；单 collection +
         payload user_id 过滤（替代每用户一个 collection）

L1 缓存不变式（2026-09 修复，见 PRODUCTION_MIGRATION_CHECKLIST 第 8 条）
=====================================================================
缓存只能是**加速器**，不能变成"第二个真相来源"。四条硬规则：

1. **PG 是唯一真相来源。** 命中即返回是错的 —— 必须"装得下本次请求"
   （缓存条数 ≥ n）才短路，否则回源 PG 并按窗口重建缓存。否则先查 20 条
   再查 30 条时，用户只会拿到缓存里那 20 条，表现为模型"偶发失忆"。
2. **写路径先落库、再写缓存。** 缓存写失败只是少一次命中，绝不能连累落库
   （旧实现先写缓存，Redis 一抖就连消息都丢了）。
3. **删数据的动作必须同步失效缓存**（删会话 / 清历史），不能靠 TTL 兜 ——
   否则删掉的消息会在 TTL 内"复活"。
4. **任何 Redis 异常都 fail-open**（当 miss 处理 + 失效可疑 key + 熔断冷却），
   进程内 LRU 兜底，恢复后自动切回。降级可以慢，不能答错、不能抛。

配置（全部走环境变量，见 .env.example）
======================================
``REDIS_URL``             连接串（含密码/库号），默认 redis://localhost:6379/0
``REDIS_ENABLED``         0=完全不使用 Redis（强制进程内缓存）
``REDIS_CACHE_TTL``       key 过期秒数，默认 3600
``CHAT_CACHE_MAXLEN``     每会话缓存条数上限，默认 50（须 ≥ 调用方最大 n）
``REDIS_KEY_PREFIX``      key 命名空间前缀，默认 study1（多环境共用实例时防串数据）
``REDIS_SOCKET_TIMEOUT``  单次读写超时秒数，默认 1.5（防 Redis 卡住拖垮请求）
``REDIS_FAIL_COOLDOWN``   熔断后冷却秒数，默认 10（到点自动重试，可自愈）
"""

import asyncio
import json
import os
import time
from collections import OrderedDict
from datetime import datetime
from typing import List, Optional

from langchain_core.messages import AIMessage, HumanMessage

from src import redis_client, vector_store
from src.db import execute, execute_many, query_all, query_one

from dotenv import load_dotenv
load_dotenv()
from src.logging_config import get_logger
logger = get_logger(__name__)


# ============================================================
# Layer 1 — 热缓存（Redis，不可用则降级为进程内 LRU）
# ============================================================
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


REDIS_URL = redis_client.REDIS_URL          # 连接串/密码/库号（见 src/redis_client.py）
REDIS_ENABLED = redis_client.REDIS_ENABLED
CACHE_TTL = max(1, _env_int("REDIS_CACHE_TTL", 3600))
CACHE_MAXLEN = max(1, _env_int("CHAT_CACHE_MAXLEN", 50))
CACHE_PREFIX = (os.getenv("REDIS_KEY_PREFIX", "study1") or "study1").strip(":")
FAIL_COOLDOWN = _env_float("REDIS_FAIL_COOLDOWN", 10.0)
LOCAL_MAX_KEYS = max(1, _env_int("CHAT_CACHE_LOCAL_MAX_KEYS", 1000))

# 客户端来自共享工厂（src/redis_client.py）；这里只管缓存的失败语义：fail-open
_use_redis = False        # 当前是否真的在用 Redis（可观测/兼容旧文档引用）
_redis_ready_state: Optional[bool] = None   # None=未知，True=可用，False=熔断中
_redis_down_until = 0.0   # 熔断冷却截止（monotonic 秒）


class _LocalLRU:
    """进程内降级缓存：带上限 + 惰性过期的 LRU。

    旧实现是裸 dict：只截断单 key 长度，key 本身永不过期 → 长跑进程内存只涨不降。
    """

    def __init__(self, max_keys: int, ttl: int):
        self._max_keys = max_keys
        self._ttl = ttl
        self._data: "OrderedDict[str, tuple[float, list]]" = OrderedDict()

    def _alive(self, key: str) -> Optional[list]:
        item = self._data.get(key)
        if item is None:
            return None
        expire_at, value = item
        if expire_at < time.monotonic():     # 惰性过期：顺手清掉
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)          # LRU：命中即刷新热度
        return value

    def get(self, key: str) -> Optional[list]:
        value = self._alive(key)
        return list(value) if value is not None else None

    def set(self, key: str, values: list) -> None:
        self._data[key] = (time.monotonic() + self._ttl, list(values))
        self._data.move_to_end(key)
        while len(self._data) > self._max_keys:
            self._data.popitem(last=False)

    def append(self, key: str, values: list) -> None:
        current = self._alive(key) or []
        self.set(key, (current + list(values))[-CACHE_MAXLEN:])

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def delete_prefix(self, prefix: str) -> None:
        for key in [k for k in self._data if k.startswith(prefix)]:
            self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()


_local = _LocalLRU(LOCAL_MAX_KEYS, CACHE_TTL)


def _client():
    """惰性获取 Redis 客户端（共享工厂，带连接池与超时）；不可用 → None。

    缓存是 fail-open 的：拿不到客户端就老老实实走进程内 LRU。
    """
    return redis_client.get_client()


def _redis_conn():
    """拿到可用客户端；不可用或处于熔断冷却期 → None（走进程内 LRU）。"""
    client = _client()
    if client is None or time.monotonic() < _redis_down_until:
        return None
    return client


def _mark_ok() -> None:
    """一次成功读写：状态翻转时打日志（不在每次调用刷屏）。"""
    global _use_redis, _redis_ready_state
    _use_redis = True
    if _redis_ready_state is not True:
        _redis_ready_state = True
        logger.info("L1 热缓存：Redis 就绪（%s，prefix=%s，ttl=%ss，maxlen=%s）",
                    REDIS_URL, CACHE_PREFIX, CACHE_TTL, CACHE_MAXLEN)


def _trip(exc: Exception) -> None:
    """Redis 异常：熔断 + 冷却（到点自动重试），并降级到进程内 LRU。"""
    global _use_redis, _redis_ready_state, _redis_down_until
    _use_redis = False
    _redis_down_until = time.monotonic() + FAIL_COOLDOWN
    if _redis_ready_state is not False:
        _redis_ready_state = False
        logger.warning("L1 热缓存：Redis 不可用，降级进程内缓存，%ss 后自动重试（%s）",
                       int(FAIL_COOLDOWN), exc)


def cache_backend() -> str:
    """当前缓存后端（观测用）：redis | local。"""
    return "redis" if _use_redis else "local"


def _cache_key(user_id: str, session_id: str = "default") -> str:
    """命名空间化 key：多环境共用一个 Redis 实例时不会互相串数据。"""
    return f"{CACHE_PREFIX}:chat:{user_id}:{session_id}"


def _user_key_prefix(user_id: str) -> str:
    return f"{CACHE_PREFIX}:chat:{user_id}:"


def _dumps(msg: dict) -> str:
    return json.dumps(msg, ensure_ascii=False)


def _loads(raw: str) -> Optional[dict]:
    try:
        msg = json.loads(raw)
        return msg if isinstance(msg, dict) and "role" in msg and "content" in msg else None
    except (ValueError, TypeError):
        return None


# ── 缓存原语：一律 best-effort，绝不向上抛 ──────────────────────

async def _cache_get(user_id: str, n: int, session_id: str = "default") -> Optional[list]:
    """取最近 n 条。

    **命中条件：缓存条数 ≥ n**。只判"非空"是错的（旧 bug）：缓存里只有 4 条时
    请求 8 条会拿到 4 条。装不下 → 返回 None，让调用方回源 PG。
    """
    key = _cache_key(user_id, session_id)
    client = _redis_conn()
    if client is None:
        cached = _local.get(key)
    else:
        try:
            raw = await client.lrange(key, -n, -1)
            _mark_ok()
            cached = [m for m in (_loads(x) for x in raw) if m is not None]
        except Exception as e:  # noqa: BLE001
            _trip(e)
            cached = _local.get(key)
    if not cached or len(cached) < n:
        return None
    return cached[-n:]      # 两个后端统一语义：只返回"最近 n 条"


async def _cache_replace(user_id: str, msgs: list, session_id: str = "default") -> None:
    """用 PG 回源的窗口**重建**缓存（DEL + RPUSH，原子替换）。

    重建而非追加：追加会把 PG 里更早的消息塞到队尾导致乱序/重复。
    """
    if not msgs:
        return
    key = _cache_key(user_id, session_id)
    window = msgs[-CACHE_MAXLEN:]
    client = _redis_conn()
    if client is None:
        _local.set(key, window)
        return
    try:
        pipe = client.pipeline(transaction=False)
        pipe.delete(key)
        pipe.rpush(key, *[_dumps(m) for m in window])
        pipe.ltrim(key, -CACHE_MAXLEN, -1)
        pipe.expire(key, CACHE_TTL)
        await pipe.execute()
        _mark_ok()
    except Exception as e:  # noqa: BLE001
        _trip(e)
        _local.set(key, window)


async def _cache_append_many(user_id: str, msgs: list, session_id: str = "default") -> None:
    """追加消息（一次 pipeline：N 条消息 1 个往返，而不是 N 个）。"""
    if not msgs:
        return
    key = _cache_key(user_id, session_id)
    client = _redis_conn()
    if client is None:
        _local.append(key, msgs)
        return
    try:
        pipe = client.pipeline(transaction=False)
        pipe.rpush(key, *[_dumps(m) for m in msgs])
        pipe.ltrim(key, -CACHE_MAXLEN, -1)
        pipe.expire(key, CACHE_TTL)
        await pipe.execute()
        _mark_ok()
    except Exception as e:  # noqa: BLE001
        _trip(e)
        # 可能只写进去一半 → 失效这个 key（含进程内副本），下次从 PG 重建。
        # 宁可 miss（回源 PG）也不脏读。
        await _cache_clear(user_id, session_id)


async def _cache_clear(user_id: str, session_id: str = "default") -> None:
    """删除单个会话的缓存。"""
    key = _cache_key(user_id, session_id)
    _local.delete(key)
    client = _redis_conn()
    if client is not None:
        try:
            await client.delete(key)
            _mark_ok()
        except Exception as e:  # noqa: BLE001
            _trip(e)


async def _cache_clear_user(user_id: str) -> None:
    """删除该用户**所有会话**的缓存（清空历史 / 注销账号时用）。

    旧实现这里退化成只删了 'default' 一个 key，而 PG 删的是该用户全部行 →
    其他会话的缓存残留，"清空记录"看起来没生效。
    """
    prefix = _user_key_prefix(user_id)
    _local.delete_prefix(prefix)
    client = _redis_conn()
    if client is not None:
        try:
            async for key in client.scan_iter(match=f"{prefix}*", count=200):
                await client.delete(key)
            _mark_ok()
        except Exception as e:  # noqa: BLE001
            _trip(e)


async def invalidate_cache(user_id: str, session_id: Optional[str] = None) -> None:
    """对外暴露的缓存失效入口（删会话 / 清历史必须调用它）。

    session_id 为空 → 失效该用户全部会话。任何异常只记警告：
    缓存失效失败不该把"删数据"这个业务动作一起弄失败。
    """
    try:
        uid = sanitize_user_id(user_id)
        if session_id:
            await _cache_clear(uid, session_id)
        else:
            await _cache_clear_user(uid)
    except Exception as e:  # noqa: BLE001
        logger.warning("[记忆] 缓存失效失败（不影响业务，等 TTL 自然过期）: %s", e)


async def reset_cache() -> None:
    """清空本服务的 L1 命名空间（测试隔离用）。

    只扫自己的 prefix，**不做 FLUSHALL** —— 共用实例时不能删别人的数据。
    """
    _local.clear()
    client = _redis_conn()
    if client is not None:
        try:
            async for key in client.scan_iter(match=f"{CACHE_PREFIX}:*", count=200):
                await client.delete(key)
            _mark_ok()
        except Exception as e:  # noqa: BLE001
            _trip(e)


async def try_ping() -> bool:
    """探活（启动日志 / 健康检查用）；失败自动进入熔断降级。"""
    client = _redis_conn()
    if client is None:
        return False
    try:
        await client.ping()
        _mark_ok()
        return True
    except Exception as e:  # noqa: BLE001
        _trip(e)
        return False


def _to_message(msg: dict):
    """缓存/PG 行 → LangChain 消息（历史数据里非 human 一律按 ai 处理）。"""
    return HumanMessage(content=msg["content"]) if msg.get("role") == "human" \
        else AIMessage(content=msg["content"])


# ============================================================
# Layer 2 + 3 — 记忆对象
# ============================================================
class UserMemory:
    def __init__(self, user_id: str):
        self.user_id = user_id

    # ── Layer 2: 对话历史（PostgreSQL，按 session 隔离）──
    async def save_messages(self, new_messages: list, session_id: str = "default") -> None:
        """保存消息：先落 PG（真相来源，单事务批量），再 best-effort 写热缓存。"""
        rows = [{"role": m.type, "content": m.content}
                for m in new_messages if m.type in ("human", "ai")]
        if not rows:
            return

        # 外键前提：写消息前确保该身份在 users 里有行（guest/mock/演示账号也可能走到这里）
        from src.users import ensure_user_row
        await ensure_user_row(self.user_id)

        ts = datetime.now().isoformat()
        await execute_many(  # N 条消息 1 个事务（原实现是 N 个事务）
            "INSERT INTO conversations (user_id, session_id, role, content, created_at) "
            "VALUES (:uid, :sid, :role, :content, :ts)",
            [{"uid": self.user_id, "sid": session_id, "role": r["role"],
              "content": r["content"], "ts": ts} for r in rows],
        )

        try:  # 缓存写失败只降命中率，不影响正确性（也不该让请求失败）
            await _cache_append_many(self.user_id, rows, session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("[记忆] 缓存写入失败（消息已落库，仅影响命中率）: %s", e)

    async def load_recent(self, n: int = 30, session_id: str = "default") -> list:
        """加载某会话最近 n 条：缓存装得下就短路，否则回源 PG 并重建缓存。"""
        cached = None
        try:
            cached = await _cache_get(self.user_id, n, session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("[记忆] 缓存读取失败，回源 PG: %s", e)
        if cached is not None:
            return [_to_message(m) for m in cached]

        # 回源时按"窗口上限"取（而不是只取 n 条）：一次回源就能满足后续更大的 n
        limit = max(n, CACHE_MAXLEN)
        rows = await query_all(
            "SELECT role, content FROM conversations "
            "WHERE user_id = :uid AND session_id = :sid ORDER BY id DESC LIMIT :n",
            {"uid": self.user_id, "sid": session_id, "n": limit},
        )
        messages = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

        try:
            await _cache_replace(self.user_id, messages, session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("[记忆] 缓存回填失败（不影响本次结果）: %s", e)
        return [_to_message(m) for m in messages[-n:]]

    async def get_last_query_type(self) -> str:
        row = await query_one(
            "SELECT value FROM profile WHERE user_id = :uid AND key = 'last_query_type'",
            {"uid": self.user_id},
        )
        return row["value"] if row else "chat"

    async def save_last_query_type(self, qtype: str) -> None:
        from src.users import ensure_user_row
        await ensure_user_row(self.user_id)          # profile 也有外键
        now = datetime.now().isoformat()
        await execute(
            "INSERT INTO profile (user_id, key, value, updated_at) VALUES (:uid, 'last_query_type', :v, :ts) "
            "ON CONFLICT (user_id, key) DO UPDATE SET value = :v2, updated_at = :ts2",
            {"uid": self.user_id, "v": qtype, "ts": now, "v2": qtype, "ts2": now},
        )

    async def clear_history(self, session_id: Optional[str] = None) -> None:
        """清空历史：session_id 为空 → 该用户全部会话。

        **库与缓存的范围必须一致**（旧实现：库删全部、缓存只删 default）。
        """
        if session_id:
            await execute(
                "DELETE FROM conversations WHERE user_id = :uid AND session_id = :sid",
                {"uid": self.user_id, "sid": session_id},
            )
        else:
            await execute("DELETE FROM conversations WHERE user_id = :uid",
                          {"uid": self.user_id})
        await invalidate_cache(self.user_id, session_id)

    # ── Layer 3: Qdrant 用户偏好 ──
    async def extract_and_store(self, messages: list, llm) -> None:
        """后台异步：LLM 扫对话历史提取偏好写入 Qdrant（调用方用 asyncio.create_task 不阻塞回复）。"""
        history_text = "\n".join(
            f"{'客户' if m.type == 'human' else '客服'}: {m.content[:100]}"
            for m in messages[-10:]
        )
        prompt = f"""你是客户档案分析师。只提取客户基本信息和长期偏好。

只记录客户**原话明确说过**的长期信息：
- 身份：客户说 "我是做羽绒服的""我是外贸公司" 才记
- 联系方式：客户主动给的电话、地址

严禁记录：
- 颜色偏好（"喜欢黑色"——除非客户明确说 "我只要黑色"）
- 预算（"便宜点""贵了"——不记）
- 任何从订单反向推断的偏好
- 一次闲聊、问价

不确定就不要记。宁可漏记不可错记。没有明确信息就输出 SKIP。

对话：
{history_text}

值得记住的信息（每条一行，简明扼要；或输出 SKIP）："""

        try:
            resp = await llm.ainvoke([{"role": "user", "content": prompt}])
            result = resp.content.strip() if hasattr(resp, 'content') else str(resp)
            if result and result != "SKIP" and len(result) > 5:
                for line in result.split("\n"):
                    line = line.strip()
                    if line and line != "SKIP" and len(line) > 3:
                        # 去重：相似偏好不再重复写入（距离阈值）
                        if await vector_store.memory_has_similar(self.user_id, line):
                            continue
                        await vector_store.upsert_memory(
                            self.user_id, line, datetime.now().isoformat(),
                        )
                logger.info(f"[记忆] 提取偏好: {result.splitlines()}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[记忆] 提取失败: {e}")

    async def retrieve_preferences(self, n: int = 10) -> List[str]:
        """检索该用户偏好（注入 system prompt）。"""
        try:
            return await vector_store.search_memory(self.user_id, limit=n)
        except Exception:  # noqa: BLE001
            return []


# ============================================================
# 全局注册表
# ============================================================
_active_users: dict[str, "UserMemory"] = {}


def sanitize_user_id(user_id: str, default: str = "guest") -> str:
    """校验并规范化 user_id（防御兜底：任何入口都不会把恶意 ID 拼进 SQL/路径）。"""
    from src.user_identity import is_valid_user_id
    if is_valid_user_id(user_id):
        return user_id
    logger.warning(f"[记忆] 非法 user_id 已降级为 '{default}': {user_id!r}")
    return default


def get_user(user_id: str) -> UserMemory:
    user_id = sanitize_user_id(user_id)
    if user_id not in _active_users:
        _active_users[user_id] = UserMemory(user_id)
    return _active_users[user_id]
