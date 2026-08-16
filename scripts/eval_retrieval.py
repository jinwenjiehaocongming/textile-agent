"""
检索评估 + 消融实验
====================
对比 4 种检索配置在 Hit@k / MRR 上的表现，验证「混合检索 + Rerank」的价值：

  ① 纯向量（ChromaDB + bge-base-zh-v1.5）
  ② 纯 BM25（关键词）
  ③ 混合（向量 + BM25 → RRF 融合）
  ④ 混合 + CrossEncoder Rerank

运行: python scripts/eval_retrieval.py
"""
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable, List

os.environ.setdefault("TQDM_DISABLE", "1")  # 禁用 CrossEncoder 的进度条

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.retrieval import HybridRetriever, rrf_fusion

# ============================================================
# 评测集：query → 期望命中的知识类别（独立标注，覆盖 30/47 个类别）
# ============================================================
EVAL_SET = [
    ("羽绒服用什么面料做", "场景推荐-羽绒服"),
    ("冲锋衣用什么面料", "场景推荐-冲锋衣"),
    ("夏季服装面料推荐", "场景推荐-夏季服装"),
    ("箱包面料怎么选", "场景推荐-箱包"),
    ("涤塔夫是什么", "面料基础-涤塔夫"),
    ("尼丝纺的规格", "面料基础-尼丝纺"),
    ("春亚纺的特点", "面料基础-春亚纺"),
    ("牛津布有什么用途", "面料基础-牛津布"),
    ("桃皮绒是什么", "面料基础-桃皮绒"),
    ("麂皮绒的特点", "面料基础-麂皮绒"),
    ("记忆布是什么面料", "面料基础-记忆布"),
    ("面料起订量多少", "采购指南-起订量"),
    ("采购面料的流程", "采购指南-流程"),
    ("防水面料怎么做", "后整理-防水"),
    ("羽绒服防钻绒怎么处理", "后整理-防绒"),
    ("面料染色工艺", "后整理-染色"),
    ("面料涂层工艺", "后整理-涂层"),
    ("压光是什么工艺", "后整理-压光"),
    ("面料磨毛处理", "后整理-磨毛"),
    ("面料色差怎么办", "常见问题-色差"),
    ("面料缩水怎么办", "常见问题-缩水"),
    ("面料静电怎么处理", "常见问题-静电"),
    ("面料纬斜是什么", "常见问题-纬斜"),
    ("色牢度检测标准", "检测标准-色牢度标准体系"),
    ("面料物理性能检测", "检测标准-物理性能"),
    ("氨纶是什么纤维", "纤维原料-氨纶"),
    ("锦纶的特点", "纤维原料-锦纶"),
    ("粘胶纤维特点", "纤维原料-粘胶"),
    ("数码印花工艺", "印花工艺-数码印花"),
    ("活性印花工艺", "印花工艺-活性印花"),
]


def _cat(text: str) -> str:
    """从 chunk 文本提取 [类别] 标记"""
    m = re.search(r"\[类别\]\s*(.+)", text)
    return m.group(1).strip() if m else "未知"


# ============================================================
# 四种检索配置
# ============================================================
def _vector(r: HybridRetriever, query: str, k: int) -> List[str]:
    raw = r.collection.query(query_texts=[query], n_results=k, include=["documents"])
    return [_cat(d) for d in raw["documents"][0]]


def _bm25(r: HybridRetriever, query: str, k: int) -> List[str]:
    hits = r.bm25.search(query, top_k=k)
    return [_cat(r.bm25.documents[idx]) for idx, _ in hits]


def _hybrid(r: HybridRetriever, query: str, k: int) -> List[str]:
    vec_raw = r.collection.query(query_texts=[query], n_results=10, include=["documents"])
    vec_hits = [(d, i, 1.0) for i, d in enumerate(vec_raw["documents"][0])]
    bm25_hits = [(r.bm25.documents[idx], idx, score) for idx, score in r.bm25.search(query, top_k=10)]
    fused = rrf_fusion(vec_hits, bm25_hits)
    return [_cat(text) for text, _, _ in fused[:k]]


def _hybrid_rerank(r: HybridRetriever, query: str, k: int) -> List[str]:
    return [res["category"] for res in r.retrieve(query, top_k=k, use_rerank=True)]


# ============================================================
# 指标：Hit@k（Recall@k）+ MRR
# ============================================================
def evaluate(search_fn: Callable, retriever: HybridRetriever, k: int = 3) -> dict:
    hits, mrrs = [], []
    for query, expected in EVAL_SET:
        cats = search_fn(retriever, query, k)
        rank = next((i + 1 for i, c in enumerate(cats) if c == expected), None)
        hits.append(1.0 if rank else 0.0)
        mrrs.append(1.0 / rank if rank else 0.0)
    return {
        "Hit@%d" % k: sum(hits) / len(hits),
        "MRR": sum(mrrs) / len(mrrs),
    }


def main():
    print("🔍 加载混合检索器（embedding + BM25 + Reranker 本地模型）...")
    retriever = HybridRetriever()
    k = 3

    methods = [
        ("① 纯向量", _vector),
        ("② 纯BM25", _bm25),
        ("③ 混合(RRF)", _hybrid),
        ("④ 混合+Rerank", _hybrid_rerank),
    ]

    print(f"\n评测集 {len(EVAL_SET)} 条 | top_k={k}\n")
    print(f"{'方法':<18} {'Hit@{0}'.format(k):<10} {'MRR':<8}")
    print("-" * 36)
    results = {}
    for name, fn in methods:
        metrics = evaluate(fn, retriever, k)
        results[name] = metrics
        print(f"{name:<18} {metrics['Hit@%d' % k]:<10.1%} {metrics['MRR']:<8.3f}")

    # 结论：混合 + Rerank 应优于单路
    base = results["① 纯向量"]["Hit@%d" % k]
    best = results["④ 混合+Rerank"]["Hit@%d" % k]
    print("\n" + "=" * 36)
    print(f"结论: 混合+Rerank 相比纯向量，Hit@{k} {base:.1%} → {best:.1%}")
    print("说明: 面料型号(T400/380T)是精确词，向量易漏，BM25 补精确匹配；Rerank 精排去噪。")

    # 写报告
    out = Path(__file__).parent.parent / "eval_results"
    out.mkdir(exist_ok=True)
    report = {
        "top_k": k,
        "total": len(EVAL_SET),
        "methods": results,
    }
    (out / "eval_retrieval.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n📄 报告已写入 eval_results/eval_retrieval.json")


if __name__ == "__main__":
    main()
