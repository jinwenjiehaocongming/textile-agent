"""
混合检索器 Hybrid Retrieval（异步版）
====================================
Qdrant 向量语义检索 + BM25 关键词检索 → 加权 RRF 融合 → CrossEncoder 重排

企业级演进（2026-08）：
- 向量端：ChromaDB 嵌入式 → Qdrant（src.vector_store；LocalMode 本地 / 独立服务）
- 类别过滤由 Qdrant payload filter 完成（不再全量拉取建映射）
- rerank 推理挪到 worker 线程（asyncio.to_thread），不阻塞事件循环
"""

# load_dotenv 必须在模型 import 之前——.env 里有 HF_HUB_OFFLINE=1
from dotenv import load_dotenv
load_dotenv()

import asyncio
import math
import os
import pickle
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src import vector_store
from src.logging_config import get_logger

# 离线模式（有缓存则不联网）
if Path(os.path.expanduser("~/.cache/huggingface")).exists():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

# CrossEncoder 懒加载：模型 ~1GB，首次 rerank 时才加载（import 不阻塞）
_reranker = None


def _get_reranker():
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder
        _reranker = CrossEncoder("BAAI/bge-reranker-base", max_length=512)
    return _reranker


logger = get_logger(__name__)

# ---- 路径 ----
PROJECT_ROOT = Path(__file__).parent.parent
BM25_PATH = PROJECT_ROOT / "index" / "bm25_index.pkl"
KNOWLEDGE_COLLECTION = vector_store.KNOWLEDGE_COLLECTION


# ============================================================
# 中文分词（与 build_index.py 一致）
# ============================================================
def tokenize(text: str) -> List[str]:
    text = text.lower().strip()
    tokens = []
    for match in re.finditer(r"[a-zA-Z0-9]+", text):
        tokens.append(match.group())
    chars = re.findall(r"[一-鿿]", text)
    for i in range(len(chars) - 1):
        tokens.append(chars[i] + chars[i + 1])
    return tokens


# ============================================================
# BM25 索引（自研，不依赖向量库）
# ============================================================
class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.documents: List[str] = []
        self.doc_tokens: List[List[str]] = []
        self.doc_lengths: List[int] = []
        self.avgdl: float = 0.0
        self.idf: Dict[str, float] = {}
        self.N: int = 0

    def index(self, documents: List[str]):
        self.documents = list(documents)
        self.N = len(self.documents)
        if self.N == 0:
            return
        self.doc_tokens = [tokenize(doc) for doc in self.documents]
        self.doc_lengths = [len(tokens) for tokens in self.doc_tokens]
        self.avgdl = sum(self.doc_lengths) / self.N
        df: Dict[str, int] = {}
        for tokens in self.doc_tokens:
            for token in set(tokens):
                df[token] = df.get(token, 0) + 1
        self.idf = {
            token: math.log((self.N - freq + 0.5) / (freq + 0.5) + 1.0)
            for token, freq in df.items()
        }

    def search(self, query: str, top_k: int = 10) -> List[Tuple[int, float]]:
        if self.N == 0:
            return []
        query_tokens = tokenize(query)
        scores = []
        for idx in range(self.N):
            score = 0.0
            doc_len = self.doc_lengths[idx]
            if doc_len == 0:
                continue
            for token in query_tokens:
                idf = self.idf.get(token, 0.0)
                if idf == 0.0:
                    continue
                tf = self.doc_tokens[idx].count(token)
                if tf == 0:
                    continue
                num = tf * (self.k1 + 1)
                den = tf + self.k1 * (1 - self.b + self.b * doc_len / self.avgdl)
                score += idf * idf * num / den
            if score > 0:
                scores.append((idx, score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]

    @classmethod
    def load(cls, path: Path) -> "BM25Index":
        with open(path, "rb") as f:
            data = pickle.load(f)
        obj = cls(k1=data["k1"], b=data["b"])
        obj.documents = data["documents"]
        obj.doc_tokens = data["doc_tokens"]
        obj.doc_lengths = data["doc_lengths"]
        obj.avgdl = data["avgdl"]
        obj.idf = data["idf"]
        obj.N = data["N"]
        return obj


# ============================================================
# RRF 融合
# ============================================================
def rrf_fusion(
    vector_results: List[Tuple[str, int, float]],
    bm25_results: List[Tuple[str, int, float]],
    k: int = 60,
    vec_weight: float = 0.5,
) -> List[Tuple[str, int, float]]:
    """加权 RRF：按文本内容去重，同一文档两路命中则分数累加。"""
    scores: Dict[str, float] = {}
    bm25_weight = 1.0 - vec_weight

    for rank, (text, idx, _) in enumerate(vector_results):
        scores[text] = vec_weight / (k + rank + 1)

    for rank, (text, idx, _) in enumerate(bm25_results):
        scores[text] = scores.get(text, 0.0) + bm25_weight / (k + rank + 1)

    fused = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [(text, -1, score) for text, score in fused]


# ============================================================
# 混合检索器
# ============================================================
class HybridRetriever:
    def __init__(self, bm25_path: Path = BM25_PATH):
        # Qdrant 向量端（LocalMode 本地存储 / QDRANT_URL 独立服务）
        self._text2cat: Dict[str, str] = {}
        self._cat_loaded = False  # 正文→类别映射懒加载（首次检索时构建）
        # BM25 端
        self.bm25 = BM25Index.load(bm25_path)
        self.vec_k = 10
        self.bm25_k = 10
        self.vec_weight = 0.5  # 向量:BM25 = 5:5

    async def ensure_ready(self) -> None:
        """显式预热（eval/启动等场景可先调用，避免首次检索时重建映射）。"""
        await self._ensure_text2cat()

    async def _ensure_text2cat(self) -> None:
        """懒构建正文→类别映射（Qdrant scroll，首次检索时执行一次）。"""
        if self._cat_loaded:
            return
        try:
            await vector_store.ensure_collections()
            self._text2cat = await _load_text2cat()
        except Exception as e:  # noqa: BLE001 — 集合未建/服务未起时降级为空映射
            logger.warning(f"[检索] text2cat 构建失败（降级）: {str(e)[:80]}")
            self._text2cat = {}
        self._cat_loaded = True

    async def retrieve(
        self,
        query: str,
        top_k: int = 3,
        category: Optional[str] = None,
        use_rerank: bool = False,
        verbose: bool = False,
    ) -> List[Dict]:
        """
        混合检索入口（异步）。

        返回: [{"text": str, "score": float, "category": str}, ...]
        """
        # 0. 懒加载正文→类别映射
        await self._ensure_text2cat()
        # 1. 向量检索（Qdrant：embed + query 在 worker 线程）
        vec_hits = await vector_store.search_knowledge(query, category=category, top_k=self.vec_k)
        vector_hits = [(h["text"], i, 1.0 - h["score"]) for i, h in enumerate(vec_hits)]

        # 2. BM25 检索（本地索引，毫秒级，直跑）
        bm25_raw = self.bm25.search(query, top_k=self.bm25_k)
        bm25_hits = []
        for idx, score in bm25_raw:
            doc = self.bm25.documents[idx]
            if category and self._text2cat.get(doc, "") != category:
                continue
            bm25_hits.append((doc, idx, score))

        # 3. RRF 融合 — 多拿候选留给 Rerank 筛
        fetch_k = max(top_k, 10) if use_rerank else top_k
        fused = rrf_fusion(vector_hits, bm25_hits, vec_weight=self.vec_weight)

        # 4. 组装结果
        results = []
        for text, idx, score in fused[:fetch_k]:
            results.append({
                "text": text,
                "score": round(score, 4),
                "category": self._text2cat.get(text, "未知"),
            })

        # 5. Rerank（可选）：模型推理挪到线程，避免阻塞事件循环
        if use_rerank and len(results) > top_k:
            results = await asyncio.to_thread(self._rerank_sync, query, results, top_k)

        if verbose:
            mode = "hybrid" if bm25_hits else "vector_only"
            mode += "+rerank" if use_rerank else ""
            logger.info(f"[检索] [{mode}] 查询: {query} → {len(results)} 条结果")

        return results

    def _rerank_sync(self, query: str, candidates: List[Dict], top_k: int = 3) -> List[Dict]:
        """CrossEncoder 重排（同步；由调用方 to_thread 包装）。"""
        if len(candidates) <= top_k:
            return candidates
        pairs = [[query, doc["text"][:500]] for doc in candidates]
        scores = _get_reranker().predict(pairs, show_progress_bar=False)
        for doc, score in zip(candidates, scores):
            doc["rerank_score"] = round(float(score), 4)
        candidates.sort(key=lambda x: x["rerank_score"], reverse=True)
        return candidates[:top_k]


async def _load_text2cat() -> Dict[str, str]:
    """正文→类别 映射：从 Qdrant 全量 scroll 构建一次（BM25 过滤与结果标注用）。"""
    client = vector_store.get_client()
    mapping: Dict[str, str] = {}
    try:
        listed = client.get_collections()
        if not any(c.name == KNOWLEDGE_COLLECTION for c in listed.collections):
            return mapping
    except Exception:
        return mapping
    offset = None
    while True:
        points, offset = await vector_store._run(
            client.scroll, collection_name=KNOWLEDGE_COLLECTION,
            limit=100, with_payload=True, offset=offset,
        )
        for p in points:
            text = (p.payload or {}).get("text")
            if text:
                mapping[text] = (p.payload or {}).get("category", "未知")
        if not offset:
            break
    return mapping


# ============================================================
# 快速测试
# ============================================================
async def main():
    retriever = HybridRetriever()

    queries = [
        "羽绒服用什么面料",
        "牛津布能做什么",
        "起订量多少",
        "色差怎么办",
    ]

    for q in queries:
        print(f"\n{'='*40}")
        print(f"🔍 {q}")
        results = await retriever.retrieve(q, top_k=2, verbose=True)
        for i, r in enumerate(results):
            print(f"  #{i+1} [{r['category']}] score={r['score']}")
            print(f"     {r['text'].strip().splitlines()[0][:60] if r['text'] else ''}")


if __name__ == "__main__":
    asyncio.run(main())