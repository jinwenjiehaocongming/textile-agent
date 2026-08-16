"""混合检索纯函数测试（分词 / BM25 / RRF 融合）"""
from src.retrieval import BM25Index, rrf_fusion, tokenize


def test_tokenize_english_number():
    assert "t400" in tokenize("T400")


def test_tokenize_chinese_bigram():
    tokens = tokenize("黑色弹力")
    assert "黑色" in tokens
    assert "色弹" in tokens
    assert "弹力" in tokens


def test_bm25_ranks_relevant_first():
    idx = BM25Index()
    idx.index(["T400 黑色弹力布", "380T 尼丝纺白色", "牛津布箱包面料"])
    results = idx.search("T400", top_k=3)
    assert results[0][0] == 0  # 第一个文档含 T400，最相关


def test_bm25_empty_corpus():
    idx = BM25Index()
    idx.index([])
    assert idx.search("任意查询") == []


def test_rrf_fusion_sums_multi_source():
    # docB 在向量和 BM25 两路都命中，分数应高于只命中一路的 docA
    vec = [("docA", 0, 0.9), ("docB", 1, 0.8)]
    bm25 = [("docB", 1, 0.7)]
    fused = rrf_fusion(vec, bm25)
    assert fused[0][0] == "docB"
