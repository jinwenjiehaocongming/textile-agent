"""
构建纺织知识库索引
==================
1. 读取结构化 txt
2. 按 --- 切块，提取 [类别] 和 [标签] 做 metadata
3. 建 Qdrant 向量索引（dense）+ BM25 稀疏索引
4. 输出：index/qdrant_storage（LocalMode）或 QDRANT_URL 独立服务 + bm25_index.pkl

运行: python build_index.py
"""

import asyncio
import json
import math
import pickle
import re
from pathlib import Path
from typing import Dict, List, Tuple

from src import vector_store
from src.vector_store import reset_collections, upsert_knowledge

from dotenv import load_dotenv
load_dotenv()

# ============================================================
# 1. 配置
# ============================================================
PROJECT_ROOT = Path(__file__).parent.parent
CHUNKS_PATH = PROJECT_ROOT / "data" / "chunks.json"
BM25_PATH = PROJECT_ROOT / "index" / "bm25_index.pkl"

EMBED_MODEL = "BAAI/bge-base-zh-v1.5"


# ============================================================
# 2. 数据加载和切块
# ============================================================
def load_chunks_json(path: Path) -> List[Dict]:
    """读取 ingest.py 产出的 chunks.json（正文干净、元数据与正文分离）。"""
    if not path.exists():
        raise FileNotFoundError(
            f"chunks 文件不存在: {path}（请先运行 python scripts/ingest.py）"
        )
    chunks = json.loads(path.read_text(encoding="utf-8"))

    print(f"✅ 加载完成: {len(chunks)} 条知识块")
    for cat in sorted(set(c["category"].split("-")[0] for c in chunks)):
        count = sum(1 for c in chunks if c["category"].split("-")[0] == cat)
        print(f"   [{cat}] {count} 条")
    return chunks


# ============================================================
# 3. Qdrant 向量索引（dense retrieval）
# ============================================================
async def build_qdrant(chunks: List[Dict]) -> None:
    """将每条知识 embed 后写入 Qdrant（text + category/tags/title 进 payload）。"""
    print("   🗑️ 清空旧向量集合...")
    reset_collections()
    await upsert_knowledge(chunks)
    print("   ✅ 写入完成")


# ============================================================
# 4. BM25 稀疏索引（sparse retrieval，与检索端保持一致）
# ============================================================
def tokenize_chinese(text: str) -> List[str]:
    """中文 bigram 分词 + 英文单词"""
    text = text.lower().strip()
    tokens = []
    for match in re.finditer(r"[a-zA-Z0-9]+", text):
        tokens.append(match.group())
    chinese_chars = re.findall(r"[一-鿿]", text)
    for i in range(len(chinese_chars) - 1):
        tokens.append(chinese_chars[i] + chinese_chars[i + 1])
    return tokens


class BM25Index:
    """BM25 稀疏索引"""

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
        self.doc_tokens = [tokenize_chinese(doc) for doc in self.documents]
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
        query_tokens = tokenize_chinese(query)
        scores: List[Tuple[int, float]] = []
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
                numerator = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (1 - self.b + self.b * doc_len / self.avgdl)
                score += idf * idf * numerator / denominator
            if score > 0:
                scores.append((idx, score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({
                "documents": self.documents,
                "doc_tokens": self.doc_tokens,
                "doc_lengths": self.doc_lengths,
                "avgdl": self.avgdl,
                "idf": self.idf,
                "N": self.N,
                "k1": self.k1,
                "b": self.b,
            }, f)


def build_bm25(chunks: List[Dict]):
    """对 chunks 建 BM25 索引并保存。"""
    texts = [c["text"] for c in chunks]
    bm25 = BM25Index()
    bm25.index(texts)
    bm25.save(BM25_PATH)
    print(f"✅ BM25 索引完成: {BM25_PATH} ({len(texts)} 条文档)")


# ============================================================
# 5. 主流程
# ============================================================
async def main():
    print("🔨 开始构建索引...\n")

    chunks = load_chunks_json(CHUNKS_PATH)

    print("\n📊 构建 Qdrant 向量索引...")
    await build_qdrant(chunks)

    print("\n📊 构建 BM25 稀疏索引...")
    build_bm25(chunks)

    print("\n🎉 索引构建完成！")
    print(f"   Qdrant: {vector_store.get_client()}")
    print(f"   BM25:   {BM25_PATH}")


if __name__ == "__main__":
    asyncio.run(main())