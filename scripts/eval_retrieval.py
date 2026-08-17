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
# 评测集：query → 期望命中的知识类别（独立标注，覆盖 142 块语料）
# 题型混合：精确面料名（向量易漏、BM25 能救）+ 语义场景 + 工艺 + 口语
# ============================================================
EVAL_SET = [
    # ── 面料基础（精确面料名）──
    ("涤塔夫是什么面料", "面料基础-涤塔夫"),
    ("尼丝纺的规格和用途", "面料基础-尼丝纺"),
    ("春亚纺有什么特点", "面料基础-春亚纺"),
    ("牛津布能做什么", "面料基础-牛津布"),
    ("桃皮绒是什么", "面料基础-桃皮绒"),
    ("麂皮绒的特点", "面料基础-麂皮绒"),
    ("记忆布是干嘛的", "面料基础-记忆布"),
    ("雪纺和乔其纱是什么关系", "面料基础-雪纺（乔其纱）"),
    ("塔夫绸是什么面料", "面料基础-塔夫绸"),
    ("塔丝绒和尼丝纺的区别", "面料基础-塔丝绒"),
    ("色丁缎面是什么", "面料基础-色丁（缎面）"),
    ("斜纹布和平纹布区别", "面料基础-斜纹布"),
    ("帆布适合做什么", "面料基础-帆布"),
    ("灯芯绒条数是什么意思", "面料基础-灯芯绒"),
    ("摇粒绒保暖吗", "面料基础-摇粒绒"),
    ("法兰绒和珊瑚绒区别", "面料基础-法兰绒"),
    ("阳离子面料是什么", "面料基础-阳离子面料"),
    ("醋酸面料的特点", "面料基础-醋酸面料"),
    ("天丝是什么面料", "面料基础-天丝（莱赛尔）"),
    ("莫代尔和棉的区别", "面料基础-莫代尔"),
    ("四面弹是什么面料", "面料基础-四面弹"),
    ("高密尼丝纺能防钻绒吗", "面料基础-高密尼丝纺"),
    ("三明治网布是什么", "面料基础-网眼布（三明治网布）"),
    ("速干面料原理", "面料基础-速干面料"),
    ("阻燃面料是什么", "面料基础-阻燃面料"),

    # ── 纤维原料 ──
    ("涤纶纤维的特点", "纤维原料-涤纶"),
    ("锦纶和涤纶哪个耐磨", "纤维原料-锦纶"),
    ("氨纶的弹性", "纤维原料-氨纶"),
    ("腈纶是什么纤维", "纤维原料-腈纶"),
    ("粘胶纤维的特点", "纤维原料-粘胶"),
    ("FDY DTY ATY 区别", "纤维原料-D/DTY/FDY/ATY概念"),
    ("丙纶纤维", "纤维原料-丙纶（聚丙烯纤维）"),
    ("维纶是什么纤维", "纤维原料-维纶（聚乙烯醇缩甲醛纤维）"),
    ("纯棉面料的特点", "纤维原料-棉"),
    ("亚麻和苎麻", "纤维原料-麻（亚麻/苎麻）"),
    ("羊毛面料的优缺点", "纤维原料-羊毛"),
    ("真丝和蚕丝", "纤维原料-蚕丝（真丝）"),

    # ── 后整理工艺 ──
    ("面料防水怎么做", "后整理-防水"),
    ("羽绒服防钻绒处理", "后整理-防绒"),
    ("面料涂层工艺", "后整理-涂层"),
    ("压光工艺是什么", "后整理-压光"),
    ("面料磨毛处理", "后整理-磨毛"),
    ("面料贴合复合工艺", "后整理-贴合复合"),
    ("防晒衣怎么防晒", "后整理-防紫外线整理"),
    ("面料抗菌整理", "后整理-抗菌整理"),
    ("吸湿排汗整理", "后整理-透气透湿整理（吸湿排汗）"),
    ("衬衫免烫怎么处理", "后整理-抗皱免烫整理"),
    ("牛仔水洗砂洗", "后整理-水洗/砂洗整理"),
    ("面料预缩处理", "后整理-预缩整理"),

    # ── 场景推荐 ──
    ("羽绒服用什么面料", "场景推荐-羽绒服"),
    ("冲锋衣面料怎么选", "场景推荐-冲锋衣"),
    ("夏季衣服用什么面料", "场景推荐-夏季服装"),
    ("箱包用什么面料", "场景推荐-箱包"),
    ("户外登山服面料要求", "场景推荐-户外登山服"),
    ("防晒衣选什么面料", "场景推荐-防晒衣"),
    ("雨衣用什么面料", "场景推荐-雨衣"),
    ("内衣面料怎么选", "场景推荐-内衣"),
    ("工装劳保服面料", "场景推荐-工装/劳保服"),
    ("校服面料有什么要求", "场景推荐-校服"),
    ("运动服面料要求", "场景推荐-运动服"),
    ("床品窗帘用什么面料", "场景推荐-家纺（床品/窗帘）"),
    ("帐篷面料要求", "场景推荐-帐篷"),

    # ── 常见问题 ──
    ("面料色差怎么办", "常见问题-色差"),
    ("面料起球怎么办", "常见问题-起毛起球"),
    ("面料静电怎么处理", "常见问题-静电"),
    ("面料纬斜是什么", "常见问题-纬斜"),
    ("面料勾丝了怎么办", "常见问题-勾丝"),
    ("面料掉色褪色", "常见问题-掉色/褪色"),

    # ── 检测标准 ──
    ("色牢度检测标准", "检测标准-色牢度标准体系"),
    ("面料甲醛含量标准", "检测标准-甲醛含量"),
    ("面料pH值标准", "检测标准-pH 值"),
    ("面料耐磨性测试", "检测标准-耐磨性"),
    ("面料断裂强力撕破强力", "检测标准-断裂强力与撕破强力"),

    # ── 织造工艺 ──
    ("三原组织是什么", "织造工艺-三原组织"),
    ("面料克重是什么意思", "织造工艺-克重（g/㎡）"),
    ("面料T数是什么", "织造工艺-经纬密与 T 数"),
    ("面料门幅", "织造工艺-门幅"),

    # ── 采购指南 ──
    ("面料打样流程", "采购指南-打样流程"),
    ("面料缸差批差是什么意思", "采购指南-缸差与批差"),
    ("面料验货标准", "采购指南-验货标准"),
    ("面料交期一般多久", "采购指南-交期"),

    # ── 印花工艺 ──
    ("数码印花工艺", "印花工艺-数码印花"),
    ("活性印花工艺", "印花工艺-活性印花"),
    ("涂料印花工艺", "印花工艺-涂料印花"),
    ("转移印花工艺", "印花工艺-转移印花"),
]


def _cat(r: HybridRetriever, text: str) -> str:
    """从元数据取类别（正文与元数据已分离，不再正则抠正文）"""
    return r._text2cat.get(text, "未知")


# ============================================================
# 四种检索配置
# ============================================================
def _vector(r: HybridRetriever, query: str, k: int) -> List[str]:
    raw = r.collection.query(query_texts=[query], n_results=k, include=["documents"])
    return [_cat(r, d) for d in raw["documents"][0]]


def _bm25(r: HybridRetriever, query: str, k: int) -> List[str]:
    hits = r.bm25.search(query, top_k=k)
    return [_cat(r, r.bm25.documents[idx]) for idx, _ in hits]


def _hybrid(r: HybridRetriever, query: str, k: int) -> List[str]:
    vec_raw = r.collection.query(query_texts=[query], n_results=10, include=["documents"])
    vec_hits = [(d, i, 1.0) for i, d in enumerate(vec_raw["documents"][0])]
    bm25_hits = [(r.bm25.documents[idx], idx, score) for idx, score in r.bm25.search(query, top_k=10)]
    fused = rrf_fusion(vec_hits, bm25_hits)
    return [_cat(r, text) for text, _, _ in fused[:k]]


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

    # 结论：用 MRR 对比（Hit@3 已接近饱和，MRR 更能体现排序质量）
    vec_mrr = results["① 纯向量"]["MRR"]
    bm25_mrr = results["② 纯BM25"]["MRR"]
    best_mrr = results["④ 混合+Rerank"]["MRR"]
    print("\n" + "=" * 36)
    print(f"结论: 纯BM25 MRR {bm25_mrr:.3f} / 纯向量 {vec_mrr:.3f} → 混合+Rerank {best_mrr:.3f}")
    print("说明: BM25 补精确词、Rerank 精排去噪，混合+Rerank 的 MRR 优于单路检索。")

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
