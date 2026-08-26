"""Qdrant 向量存储 — 知识库 + 用户偏好

企业级演进（2026-08）：ChromaDB（嵌入式，单写者）→ Qdrant
=========================================================
- 本地开发：LocalMode（path=index/qdrant_storage，嵌入式，无服务）
- 生产部署：QDRANT_URL 指向独立 Qdrant 服务（并发、过滤、多租户、水平扩展）
- 一份代码两种模式：`QDRANT_URL` 未设置 → LocalMode；设置 → 直连服务
- 多租户隔离：用户偏好单 collection + payload `user_id` 过滤（行级隔离）
  （替代 ChromaDB 时代"每用户一个 collection"的笨办法）

embedding 仍在客户端（本地 bge-base-zh），Qdrant 只存向量与 payload。
"""
import asyncio
import os
from typing import Any, Optional

from qdrant_client import QdrantClient
from qdrant_client.http import models

from src.logging_config import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMBED_MODEL = "BAAI/bge-base-zh-v1.5"
VECTOR_SIZE = 768  # bge-base-zh 输出维度
COSINE_THRESHOLD = 0.15  # 距离阈值（去重用，与原 ChromaDB 语义一致）

KNOWLEDGE_COLLECTION = "textile_knowledge"
MEMORY_COLLECTION = "user_memory"

_client: Optional[QdrantClient] = None
_embed_model = None


def get_client() -> QdrantClient:
    """Qdrant 客户端：QDRANT_URL 未设置 → LocalMode（本地嵌入式）；设置 → 独立服务。"""
    global _client
    if _client is None:
        url = os.getenv("QDRANT_URL")
        if url:
            _client = QdrantClient(url=url)
            logger.info(f"[Qdrant] 连接独立服务: {url}")
        else:
            path = str(os.path.join(PROJECT_ROOT, "index", "qdrant_storage"))
            _client = QdrantClient(path=path)
            logger.info(f"[Qdrant] LocalMode（本地存储）: {path}")
    return _client


def _embedder():
    global _embed_model
    if _embed_model is None:
        from sentence_transformers import SentenceTransformer
        _embed_model = SentenceTransformer(EMBED_MODEL)
    return _embed_model


async def _run(fn, *args, **kwargs):
    """把同步 Qdrant 调用挪出事件循环（LocalMode 是同步客户端，to_thread 包装）。"""
    return await asyncio.to_thread(fn, *args, **kwargs)


# ---------------------------------------------------------------------------
# 建库/集合（幂等）
# ---------------------------------------------------------------------------


async def ensure_collections() -> None:
    """确保知识库与用户偏好两个 collection 存在（服务端 vector_size 对齐）。"""
    client = get_client()
    for name, desc in [
        (KNOWLEDGE_COLLECTION, "纺织面料 B2B 知识库"),
        (MEMORY_COLLECTION, "用户长期偏好（多租户 user_id 隔离）"),
    ]:
        if not client.collection_exists(name):
            client.create_collection(
                collection_name=name,
                vectors_config=models.VectorParams(size=VECTOR_SIZE, distance=models.Distance.COSINE),
            )
            logger.info(f"[Qdrant] 创建 collection: {name}（{desc}）")


def reset_collections() -> None:
    """清空集合（建索引/测试用）。"""
    client = get_client()
    for name in (KNOWLEDGE_COLLECTION, MEMORY_COLLECTION):
        if client.collection_exists(name):
            client.delete_collection(name)


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


async def upsert_knowledge(items: list[dict[Any, Any]]) -> None:
    """批量写入知识条目。
    items: [{"text": str, "category": str, "tags": str, "title": str}, ...]
    """
    await ensure_collections()
    client = get_client()
    texts = [it["text"] for it in items]
    vectors = _embedder().encode(texts, show_progress_bar=False).tolist()
    points = [
        models.PointStruct(
            id=i + 1,
            vector=vectors[i],
            payload={
                "text": items[i]["text"],
                "category": items[i].get("category", "未知"),
                "tags": items[i].get("tags", ""),
                "title": items[i].get("title", ""),
            },
        )
        for i in range(len(items))
    ]
    # 分批 upsert（service 对单请求 payload 有上限）
    for i in range(0, len(points), 100):
        await _run(client.upsert, collection_name=KNOWLEDGE_COLLECTION, points=points[i:i + 100])
    logger.info(f"[Qdrant] 知识库写入 {len(points)} 条")


async def upsert_memory(user_id: str, text: str, timestamp: str) -> None:
    """写入一条用户偏好（payload 带 user_id，实现行级多租户隔离）。"""
    await ensure_collections()
    client = get_client()
    vector = _embedder().encode([text], show_progress_bar=False).tolist()[0]
    await _run(
        client.upsert,
        collection_name=MEMORY_COLLECTION,
        points=[models.PointStruct(
            id=None, vector=vector,
            payload={"user_id": user_id, "text": text, "timestamp": timestamp},
        )],
    )


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------


async def search_knowledge(query_text: str, category: Optional[str] = None, top_k: int = 5) -> list[dict]:
    """语义检索知识库（可选 category 过滤），返回 [{"text", "category", "score"}, ...]。"""
    await ensure_collections()
    client = get_client()
    vector = _embedder().encode([query_text], show_progress_bar=False).tolist()[0]
    query_filter = (
        models.Filter(must=[models.FieldCondition(key="category", match=models.MatchValue(value=category))])
        if category else None
    )
    resp = await _run(
        client.query_points,
        collection_name=KNOWLEDGE_COLLECTION,
        query=vector,
        query_filter=query_filter,
        limit=top_k,
        with_payload=True,
    )
    return [{"text": p.payload.get("text", ""), "category": p.payload.get("category", ""), "score": p.score}
            for p in resp.points]


async def search_memory(user_id: str, limit: int = 10) -> list[str]:
    """检索某用户的全部偏好（payload 过滤，无需向量排序）。"""
    client = get_client()
    points, _ = await _run(
        client.scroll,
        collection_name=MEMORY_COLLECTION,
        scroll_filter=models.Filter(must=[models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id))]),
        limit=limit,
        with_payload=True,
    )
    return [p.payload.get("text", "") for p in points]


async def memory_has_similar(user_id: str, text: str, threshold: float = COSINE_THRESHOLD) -> bool:
    """去重检查：该用户下已存在相似偏好（距离小于阈值视为重复）。"""
    client = get_client()
    vector = _embedder().encode([text], show_progress_bar=False).tolist()[0]
    filter_ = models.Filter(must=[models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id))])
    try:
        resp = await _run(
            client.query_points,
            collection_name=MEMORY_COLLECTION,
            query=vector,
            query_filter=filter_,
            limit=1,
            with_payload=False,
        )
        return bool(resp.points and resp.points[0].score >= 1 - threshold)
    except Exception:  # noqa: BLE001
        return False