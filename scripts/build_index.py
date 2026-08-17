"""
构建纺织知识库索引
==================
1. 读取结构化 txt
2. 按 --- 切块，提取 [类别] 和 [标签] 做 metadata
3. 建 ChromaDB 向量索引（dense）+ BM25 稀疏索引
4. 输出：chroma_db/ + bm25_index.pkl

运行: python build_index.py
"""

import re
import math
import pickle
import os
import json
from pathlib import Path
from typing import List, Dict, Tuple
import chromadb
from chromadb.utils import embedding_functions
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# 1. 配置
# ============================================================
# 路径：脚本在 scripts/ 下，数据在 data/，索引输出到 index/
PROJECT_ROOT = Path(__file__).parent.parent
CHUNKS_PATH = PROJECT_ROOT / "data" / "chunks.json"
CHROMA_PATH = PROJECT_ROOT / "index" / "chroma_db"
BM25_PATH = PROJECT_ROOT / "index" / "bm25_index.pkl"

# 阿里云 DashScope embedding
# 本地 embedding 模型（免费，无需 API）
EMBED_MODEL = "BAAI/bge-base-zh-v1.5"  # 102MB，中文 MTEB 前3

# ============================================================
# 2. 数据加载和切块
# ============================================================
def load_chunks_json(path: Path) -> List[Dict]:
    """
    读取 ingest.py 产出的 chunks.json（正文干净、元数据与正文分离）。
    返回: [{"text": str, "category": str, "tags": str, "title": str}, ...]
    """
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
# 3. ChromaDB 向量索引（dense retrieval）
# ============================================================
def build_chroma(chunks: List[Dict]):
    """
    将每条知识 embed 后存入 ChromaDB。
    每条存 text + metadata（category/tags/title），方便后续过滤检索。
    """
    # 本地 SentenceTransformer embedding
    embed_func = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBED_MODEL,
    )

    # 创建持久化客户端
    client = chromadb.PersistentClient(path=str(CHROMA_PATH))

    # 建表（如果已存在先删掉重建）
    try:
        client.delete_collection("textile_knowledge")
        print("   🗑️ 删除旧 collection")
    except Exception:
        pass

    collection = client.create_collection(
        name="textile_knowledge",
        embedding_function=embed_func,
        metadata={"description": "纺织面料B2B智能客服知识库"},
    )

    # 批量写入
    texts = [c["text"] for c in chunks]
    ids = [f"chunk_{i:04d}" for i in range(len(chunks))]
    metadatas = [
        {"category": c["category"], "tags": c["tags"], "title": c["title"]}
        for c in chunks
    ]

    # 分批写入（阿里云 embedding API 限制每批最多 10 条）
    batch_size = 10
    for i in range(0, len(texts), batch_size):
        end = min(i + batch_size, len(texts))
        collection.add(
            documents=texts[i:end],
            ids=ids[i:end],
            metadatas=metadatas[i:end],
        )
        print(f"   📝 写入 {end}/{len(texts)}")

    print(f"✅ ChromaDB 向量索引完成: {CHROMA_PATH}")


# ============================================================
# 4. BM25 稀疏索引（sparse retrieval）
# ============================================================
def tokenize_chinese(text: str) -> List[str]:
    """中文 bigram 分词 + 英文单词"""
    text = text.lower().strip()
    tokens = []

    # 英文/数字
    for match in re.finditer(r"[a-zA-Z0-9]+", text):
        tokens.append(match.group())

    # 中文 bigram
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
    """对 chunks 建 BM25 索引"""
    texts = [c["text"] for c in chunks]
    bm25 = BM25Index()
    bm25.index(texts)
    bm25.save(BM25_PATH)
    print(f"✅ BM25 索引完成: {BM25_PATH} ({len(texts)} 条文档)")


# ============================================================
# 5. 主流程
# ============================================================
def main():
    print("🔨 开始构建索引...\n")

    # Step 1: 加载数据
    chunks = load_chunks_json(CHUNKS_PATH)

    # Step 2: ChromaDB 向量索引
    print("\n📊 构建 ChromaDB 向量索引...")
    build_chroma(chunks)

    # Step 3: BM25 稀疏索引
    print("\n📊 构建 BM25 稀疏索引...")
    build_bm25(chunks)

    print("\n🎉 索引构建完成！")
    print(f"   ChromaDB: {CHROMA_PATH}")
    print(f"   BM25:     {BM25_PATH}")


if __name__ == "__main__":
    main()
