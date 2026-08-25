"""
用户记忆 — 三层存储
====================
Layer 1: 热缓存 — _active_users dict（生产换 Redis）
Layer 2: SQLite — 对话历史永久存档
Layer 3: ChromaDB — LLM 提取的结构化偏好
"""

import json, threading
from pathlib import Path
from datetime import datetime
from typing import List, Optional
from langchain_core.messages import HumanMessage, AIMessage
from dotenv import load_dotenv
import os, chromadb
from chromadb.utils import embedding_functions
from src.mcp_servers.sqlite_utils import execute, executescript, query_all, query_one

load_dotenv()
from src.logging_config import get_logger
logger = get_logger(__name__)

DB_DIR = Path(__file__).parent.parent / "data" / "users"
DB_DIR.mkdir(parents=True, exist_ok=True)


class UserMemory:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.user_dir = DB_DIR / user_id
        self.user_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.user_dir / "chat.db"
        self._init_db()

    # ============================================================
    # SQLite 持久化
    # ============================================================
    def _init_db(self):
        executescript(self.db_path, """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS profile (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

    def save_messages(self, new_messages: list):
        """保存消息到热缓存 + SQLite"""
        for m in new_messages:
            if m.type in ("human", "ai"):
                # Layer 1: 写 Redis
                _cache_append(self.user_id, {"role": m.type, "content": m.content})
                # Layer 2: 写 SQLite
                execute(self.db_path,
                    "INSERT INTO conversations (role, content, created_at) VALUES (?, ?, ?)",
                    (m.type, m.content, datetime.now().isoformat()),
                )

    def load_recent(self, n: int = 30) -> list:
        """加载最近 N 轮对话。先查 Redis，没有再查 SQLite。"""
        # Layer 1: 先读 Redis
        cached = _cache_load_recent(self.user_id, n)
        if cached:
            messages = []
            for m in cached:
                if m["role"] == "human":
                    messages.append(HumanMessage(content=m["content"]))
                else:
                    messages.append(AIMessage(content=m["content"]))
            return messages

        # Layer 2: Redis 没命中 → 从 SQLite 补
        rows = query_all(self.db_path,
            "SELECT role, content FROM conversations ORDER BY id DESC LIMIT ?", (n,))

        messages = []
        for role, content in reversed(rows):
            if role == "human":
                messages.append(HumanMessage(content=content))
            else:
                messages.append(AIMessage(content=content))
            # 同时回写 Redis
            _cache_append(self.user_id, {"role": role, "content": content})
        return messages

    def get_last_query_type(self) -> str:
        """获取上一轮对话模式，用于 Web 端状态延续"""
        row = query_one(self.db_path,
            "SELECT value FROM profile WHERE key = 'last_query_type'")
        return row[0] if row else "chat"

    def save_last_query_type(self, qtype: str):
        execute(self.db_path,
            "INSERT OR REPLACE INTO profile (key, value, updated_at) VALUES (?, ?, ?)",
            ("last_query_type", qtype, datetime.now().isoformat()),
        )

    def clear_history(self):
        execute(self.db_path, "DELETE FROM conversations")
        _cache_clear(self.user_id)

    # ============================================================
    # Layer 3 — ChromaDB 用户偏好（异步提取 + 检索注入）
    # ============================================================
    def _chroma_collection(self):
        """获取该用户的 ChromaDB collection"""
        chroma_path = str(self.user_dir / "chroma")
        embed_func = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="BAAI/bge-base-zh-v1.5",
        )
        client = chromadb.PersistentClient(path=chroma_path)
        return client.get_or_create_collection(
            name="user_memory", embedding_function=embed_func,
            metadata={"description": f"用户 {self.user_id} 的偏好和特征"},
        )

    def extract_and_store(self, messages: list, llm):
        """
        后台异步：用 LLM 扫对话历史，提取值得记住的信息存入 ChromaDB。
        主线程不阻塞——回复已经发给用户了。
        """
        history_text = "\n".join(
            f"{'客户' if m.type == 'human' else '客服'}: {m.content[:100]}"
            for m in messages[-10:]
        )

        prompt = f"""你是客户档案分析师。只提取客户基本信息和长期偏好。

只记录客户**原话明确说过**的长期信息：
- 身份：客户说 "我是做羽绒服的""我是外贸公司" 才记
- 联系方式：客户主动给的电话、地址

严禁记≠不记录：
- 颜色偏好（"喜欢黑色"——除非客户明确说 "我只要黑色"）
- 预算（"便宜点""贵了"——不记）
- 任何从订单反向推断的偏好
- 一次闲聊、问价

不确定就不要记。宁可漏记不可错记。没有明确信息就输出 SKIP。
如果没有值得记住的信息，输出 SKIP。

对话：
{history_text}

值得记住的信息（每条一行，简明扼要；或输出 SKIP）："""

        try:
            resp = llm.invoke([{"role": "user", "content": prompt}])
            result = resp.content.strip() if hasattr(resp, 'content') else str(resp)
            if result and result != "SKIP" and len(result) > 5:
                collection = self._chroma_collection()
                for line in result.split("\n"):
                    line = line.strip()
                    if line and line != "SKIP" and len(line) > 3:
                        # 去重：检查是否已存在相似记忆
                        try:
                            existing = collection.query(query_texts=[line], n_results=1)
                            if existing["distances"] and existing["distances"][0] and existing["distances"][0][0] < 0.15:
                                continue  # 太相似，跳过
                        except Exception:
                            pass
                        doc_id = f"mem_{datetime.now().strftime('%Y%m%d%H%M%S')}_{hash(line) % 10000}"
                        try:
                            collection.add(
                                ids=[doc_id], documents=[line],
                                metadatas=[{"timestamp": datetime.now().isoformat()}],
                            )
                        except Exception:
                            pass
                logger.info(f"[记忆] 提取 {len(result.split(chr(10)))} 条偏好:")
                for line in result.split("\n"):
                    if line.strip() and line.strip() != "SKIP":
                        logger.info(f"[记忆] → {line.strip()}")
        except Exception as e:
            logger.warning(f"[记忆] 提取失败: {e}")

    def retrieve_preferences(self, n: int = 10) -> List[str]:
        """检索该用户的偏好，用于注入 system prompt"""
        try:
            collection = self._chroma_collection()
            if collection.count() == 0:
                return []
            all_data = collection.get()
            return all_data["documents"] or []
        except Exception:
            return []


# ============================================================
# Layer 1 热缓存 — Redis
# ============================================================
import json as _json

try:
    import redis
    _r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
    _r.ping()
    _use_redis = True
except Exception:
    _r = None
    _use_redis = False

if _use_redis:
    logger.info("Layer 1 热缓存: Redis")
else:
    logger.warning("Layer 1 热缓存: Dict (Redis 未运行)")


def _cache_load_recent(user_id: str, n: int = 50) -> list:
    if _use_redis:
        raw = _r.lrange(f"chat:{user_id}", -n, -1)
        return [_json.loads(m) for m in raw]
    return _dict_cache.get(user_id, [])[-n:]


def _cache_append(user_id: str, msg: dict):
    if _use_redis:
        _r.rpush(f"chat:{user_id}", _json.dumps(msg, ensure_ascii=False))
        _r.ltrim(f"chat:{user_id}", -50, -1)
        _r.expire(f"chat:{user_id}", 3600)
    else:
        if user_id not in _dict_cache:
            _dict_cache[user_id] = []
        _dict_cache[user_id].append(msg)
        _dict_cache[user_id] = _dict_cache[user_id][-50:]


def _cache_clear(user_id: str):
    if _use_redis:
        _r.delete(f"chat:{user_id}")
    else:
        _dict_cache.pop(user_id, None)


_dict_cache: dict[str, list] = {}

# 全局注册表
_active_users: dict[str, UserMemory] = {}


def sanitize_user_id(user_id: str, default: str = "guest") -> str:
    """
    校验并规范化 user_id（兜底层，规则见 src.user_identity）。
    - 合法 → 原样返回
    - 非法（空、超长、含路径分隔符等）→ 返回 default 并告警
    Web 层应在请求边界拒绝非法 ID（返回 400）；本函数作为防御兜底，
    保证任何入口（含 CLI agent.py）都不会把恶意 ID 拼进文件路径。
    """
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
