"""用户记忆 — 三层存储（异步版）

企业级演进（2026-08）
====================
Layer 1: 热缓存 — Redis（毫秒级，改用 to_thread 不阻塞事件循环）
Layer 2: PostgreSQL（src/db）— 对话历史永久存档；多租户从"每用户一个文件"
         升级为单库 + user_id 行级隔离 + 索引
Layer 3: Qdrant（src/vector_store）— LLM 提取的长期偏好；单 collection +
         payload user_id 过滤（替代每用户一个 collection）
"""

import asyncio
import json
from datetime import datetime
from typing import List

from langchain_core.messages import AIMessage, HumanMessage

from src import vector_store
from src.db import execute, query_all, query_one

from dotenv import load_dotenv
load_dotenv()
from src.logging_config import get_logger
logger = get_logger(__name__)


# ============================================================
# Layer 1 — 热缓存（Redis，未运行则降级为进程内 dict）
# ============================================================
try:
    import redis
    _r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
    _r.ping()
    _use_redis = True
except Exception:  # noqa: BLE001
    _r = None
    _use_redis = False

if _use_redis:
    logger.info("Layer 1 热缓存: Redis")
else:
    logger.warning("Layer 1 热缓存: Dict (Redis 未运行)")

_dict_cache: dict[str, list] = {}


def _cache_load_recent(user_id: str, n: int = 50) -> list:
    if _use_redis:
        raw = _r.lrange(f"chat:{user_id}", -n, -1)
        return [json.loads(m) for m in raw]
    return _dict_cache.get(user_id, [])[-n:]


def _cache_append(user_id: str, msg: dict):
    if _use_redis:
        _r.rpush(f"chat:{user_id}", json.dumps(msg, ensure_ascii=False))
        _r.ltrim(f"chat:{user_id}", -50, -1)
        _r.expire(f"chat:{user_id}", 3600)
    else:
        _dict_cache.setdefault(user_id, []).append(msg)
        _dict_cache[user_id] = _dict_cache[user_id][-50:]


def _cache_clear(user_id: str):
    if _use_redis:
        _r.delete(f"chat:{user_id}")
    else:
        _dict_cache.pop(user_id, None)


# ============================================================
# Layer 2 + 3 — 记忆对象
# ============================================================
class UserMemory:
    def __init__(self, user_id: str):
        self.user_id = user_id

    # ── Layer 2: 对话历史（PostgreSQL）──
    async def save_messages(self, new_messages: list) -> None:
        """保存消息到热缓存 + PostgreSQL。"""
        for m in new_messages:
            if m.type in ("human", "ai"):
                await asyncio.to_thread(_cache_append, self.user_id, {"role": m.type, "content": m.content})
                await execute(
                    "INSERT INTO conversations (user_id, role, content, created_at) "
                    "VALUES (:uid, :role, :content, :ts)",
                    {"uid": self.user_id, "role": m.type, "content": m.content,
                     "ts": datetime.now().isoformat()},
                )

    async def load_recent(self, n: int = 30) -> list:
        """加载最近 N 轮对话：Redis 优先，未命中再查 PG。"""
        cached = await asyncio.to_thread(_cache_load_recent, self.user_id, n)
        if cached:
            return [HumanMessage(content=m["content"]) if m["role"] == "human"
                    else AIMessage(content=m["content"]) for m in cached]

        rows = await query_all(
            "SELECT role, content FROM conversations WHERE user_id = :uid ORDER BY id DESC LIMIT :n",
            {"uid": self.user_id, "n": n},
        )
        messages = []
        for row in reversed(rows):
            if row["role"] == "human":
                messages.append(HumanMessage(content=row["content"]))
            else:
                messages.append(AIMessage(content=row["content"]))
            await asyncio.to_thread(_cache_append, self.user_id, {"role": row["role"], "content": row["content"]})
        return messages

    async def get_last_query_type(self) -> str:
        row = await query_one(
            "SELECT value FROM profile WHERE user_id = :uid AND key = 'last_query_type'",
            {"uid": self.user_id},
        )
        return row["value"] if row else "chat"

    async def save_last_query_type(self, qtype: str) -> None:
        now = datetime.now().isoformat()
        await execute(
            "INSERT INTO profile (user_id, key, value, updated_at) VALUES (:uid, 'last_query_type', :v, :ts) "
            "ON CONFLICT (user_id, key) DO UPDATE SET value = :v2, updated_at = :ts2",
            {"uid": self.user_id, "v": qtype, "ts": now, "v2": qtype, "ts2": now},
        )

    async def clear_history(self) -> None:
        await execute("DELETE FROM conversations WHERE user_id = :uid", {"uid": self.user_id})
        await asyncio.to_thread(_cache_clear, self.user_id)

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