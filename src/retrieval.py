"""
混合检索器 Hybrid Retrieval
============================
向量语义检索 + BM25 关键词检索 → 加权 RRF 融合
"""

# load_dotenv 必须在 chromadb 之前——.env 里有 HF_HUB_OFFLINE=1
from dotenv import load_dotenv
load_dotenv()

import os
import re
import math
import pickle
from pathlib import Path
from typing import List, Tuple, Dict, Optional
import chromadb
from chromadb.utils import embedding_functions

load_dotenv()

# ---- 路径 ----
PROJECT_ROOT = Path(__file__).parent.parent
CHROMA_PATH = PROJECT_ROOT / "index" / "chroma_db"
BM25_PATH = PROJECT_ROOT / "index" / "bm25_index.pkl"

# ---- embedding（本地，免费）----
# 本地 embedding 模型（免费，无需 API）
EMBED_MODEL = "BAAI/bge-base-zh-v1.5"  # 102MB，效果≈text-embedding-v4的90%  # 中文 MTEB 榜首，326MB

from langsmith import traceable
from src.logging_config import get_logger
from sentence_transformers import CrossEncoder
rerank_model = CrossEncoder("BAAI/bge-reranker-base", max_length=512)
logger = get_logger(__name__)

# ============================================================
# 中文分词（跟 build_index.py 一致）
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
# BM25 索引
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
    """
    加权 RRF：按文本内容去重，同一文档两路命中则分数累加。
    """
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
    def __init__(self, chroma_path: Path = CHROMA_PATH, bm25_path: Path = BM25_PATH):
        # ChromaDB 向量端
        self.embed_func = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=EMBED_MODEL,
        )
        self.client = chromadb.PersistentClient(path=str(chroma_path))
        self.collection = self.client.get_collection(
            "textile_knowledge", embedding_function=self.embed_func
        )

        # BM25 端
        self.bm25 = BM25Index.load(bm25_path)

        self.vec_k = 10
        self.bm25_k = 10
        self.vec_weight = 0.5  # 向量:BM25 = 5:5

    @traceable(run_type="retriever", name="hybrid_retrieve")
    def retrieve(
        self,
        query: str,
        top_k: int = 3,
        category: Optional[str] = None,
        use_rerank: bool = False,
        verbose: bool = False,
    ) -> List[Dict]:
        """
        混合检索入口。

        参数:
            query:    用户查询
            top_k:    返回条数
            category: 可选，只搜指定类别（面料基础/后整理/场景推荐...）
            verbose:  打印检索详情

        返回: [{"text": str, "score": float, "category": str}, ...]
        """
        # 1. 构建 ChromaDB 查询条件
        where = {"category": category} if category else None

        # 2. 向量检索
        vec_raw = self.collection.query(
            query_texts=[query],
            n_results=self.vec_k,
            include=["documents", "metadatas", "distances"],
            where=where,
        )
        vector_hits = []
        for i, (meta, dist) in enumerate(zip(
            vec_raw["metadatas"][0], vec_raw["distances"][0]
        )):
            similarity = 1.0 - (dist / 2.0)
            vector_hits.append((vec_raw["documents"][0][i], i, similarity))

        # 3. BM25 检索
        bm25_raw = self.bm25.search(query, top_k=self.bm25_k)
        bm25_hits = []
        for idx, score in bm25_raw:
            doc = self.bm25.documents[idx]
            # category 过滤
            cat_match = re.search(r"\[类别\]\s*(.+)", doc)
            if category and cat_match and cat_match.group(1).strip() != category:
                continue
            bm25_hits.append((doc, idx, score))

        # 4. RRF 融合 — 多拿一些候选，留给 Rerank 筛
        fetch_k = max(top_k, 10) if use_rerank else top_k
        fused = rrf_fusion(vector_hits, bm25_hits, vec_weight=self.vec_weight)

        # 5. 组装结果
        results = []
        for text, idx, score in fused[:fetch_k]:
            cat_match = re.search(r"\[类别\]\s*(.+)", text)
            results.append({
                "text": text,
                "score": round(score, 4),
                "category": cat_match.group(1).strip() if cat_match else "未知",
            })

        # 6. LLM Rerank（可选）
        if use_rerank and len(results) > top_k:
            results = self.rerank(query, results, top_k)

        if verbose:
            mode = "hybrid" if bm25_hits else "vector_only"
            mode += "+rerank" if use_rerank else ""
            logger.info(f"[检索] [{mode}] 查询: {query} → {len(results)} 条结果")

        return results

    def rerank(self, query: str, candidates: List[Dict], top_k: int = 3) -> List[Dict]:
        """Cross-Encoder Rerank：本地模型 batch 推理，毫秒级，免费"""
        if len(candidates) <= top_k:
            return candidates
        pairs = [[query, doc["text"][:500]] for doc in candidates]
        scores = rerank_model.predict(pairs, show_progress_bar=False)
        for doc, score in zip(candidates, scores):
            doc["rerank_score"] = round(float(score), 4)
        candidates.sort(key=lambda x: x["rerank_score"], reverse=True)
        return candidates[:top_k]


# ============================================================
# 快速测试
# ============================================================
def main():
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
        results = retriever.retrieve(q, top_k=2, verbose=True)
        for i, r in enumerate(results):
            print(f"  #{i+1} [{r['category']}] score={r['score']}")
            # 只打印第一段
            first_line = r["text"].strip().split("\n")[0]
            print(f"     {first_line}")


if __name__ == "__main__":
    main()
