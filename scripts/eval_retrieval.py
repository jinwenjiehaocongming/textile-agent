"""
检索质量评估
============
定义测试集 → 跑检索 → 算指标

指标：
  Recall@K  ：正确答案出现在前K个结果中的比例
  MRR       ：第一个正确答案的平均排名倒数
  Precision@K：前K个结果中正确答案占比

运行: python scripts/eval_retrieval.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.retrieval import HybridRetriever
from typing import List, Dict

# ============================================================
# 测试集：查询 → 期望命中的 chunk 标题/类别
# ============================================================
TEST_CASES = [
    # === 面料基础 (8题) ===
    ("羽绒服用什么面料做里料",    ["场景推荐", "羽绒服"], "羽绒服里料推荐"),
    ("300T春亚纺的规格参数",      ["面料基础", "春亚纺"], "春亚纺规格"),
    ("牛津布600D和900D怎么选",    ["面料基础", "牛津布"], "牛津布型号对比"),
    ("尼丝纺和涤塔夫哪个好",      ["面料基础", "尼丝纺"], "面料对比"),
    ("桃皮绒是什么面料手感怎样",   ["面料基础", "桃皮绒"], "桃皮绒特性"),
    ("记忆布和普通面料有什么区别", ["面料基础", "记忆布"], "记忆布特点"),
    ("麂皮绒适合做沙发吗",        ["面料基础", "麂皮绒"], "麂皮绒用途"),
    ("190T涤塔夫能做羽绒服吗",    ["后整理", "防绒", "涤塔夫"], "防绒面料要求"),

    # === 纤维原料 (6题) ===
    ("涤纶和锦纶有什么区别",      ["纤维原料", "涤纶"], "纤维对比"),
    ("氨纶是什么面料有什么特点",   ["纤维原料", "氨纶"], "氨纶概念"),
    ("粘胶纤维和棉哪个好",        ["纤维原料", "粘胶"], "粘胶特性"),
    ("FDY和DTY是什么意思",       ["纤维原料", "FDY", "DTY"], "纱线术语"),
    ("腈纶为什么叫人造羊毛",      ["纤维原料", "腈纶"], "腈纶特性"),
    ("ATY是什么丝有什么用途",     ["纤维原料", "ATY"], "空变丝概念"),

    # === 后整理 (8题) ===
    ("防水面料有哪些等级怎么测",   ["后整理", "防水"], "防水等级"),
    ("PU涂层和PA涂层哪个好",      ["后整理", "涂层"], "涂层对比"),
    ("磨毛会导致面料变脆吗",      ["后整理", "磨毛"], "磨毛影响"),
    ("涤纶染色温度多少需要注意什么", ["后整理", "染色"], "染色工艺"),
    ("防钻绒处理有几种方法",      ["后整理", "防绒"], "防绒工艺"),
    ("压光后还能再染色吗",        ["后整理", "压光"], "压光限制"),
    ("贴合复合工艺是什么意思",    ["后整理", "贴合"], "贴合工艺"),
    ("PVC涂层和PU涂层什么区别",   ["后整理", "涂层"], "涂层区别"),

    # === 织造工艺 (5题) ===
    ("平纹斜纹缎纹有什么区别",     ["织造工艺", "平纹", "斜纹", "缎纹"], "三原组织"),
    ("梭织和针织怎么区分",        ["织造工艺", "梭织", "针织"], "织造区别"),
    ("纬编和经编有什么不同",      ["织造工艺", "纬编", "经编"], "针织分类"),
    ("大提花和小提花是什么意思",   ["织造工艺", "提花"], "提花概念"),
    ("牛仔布是什么组织织的",      ["织造工艺", "斜纹"], "牛仔布组织"),

    # === 印花工艺 (5题) ===
    ("活性印花和涂料印花哪个好",   ["印花工艺", "活性印花", "涂料印花"], "印花对比"),
    ("数码印花一码多少钱适合小批量吗", ["印花工艺", "数码印花"], "数码印花成本"),
    ("转移印花适用于什么面料",    ["印花工艺", "转移印花"], "转移印花适用"),
    ("做床品四件套用什么印花工艺",  ["印花工艺", "活性印花"], "家纺印花选择"),
    ("深色面料能做数码印花吗",     ["印花工艺", "数码印花", "白色墨水"], "深色数码印花"),

    # === 检测标准 (5题) ===
    ("面料色牢度分几级4级算好吗",  ["检测标准", "色牢度", "品质检验"], "色牢度等级"),
    ("A类B类C类面料是什么意思",   ["检测标准", "A类", "B类", "C类"], "安全技术分类"),
    ("出口欧洲面料要测哪些项目",   ["检测标准", "出口", "Oeko"], "出口检测"),
    ("耐摩擦色牢度干摩和湿摩什么区别", ["检测标准", "摩擦", "品质检验"], "摩擦牢度"),
    ("甲醛含量多少才算达标",      ["检测标准", "甲醛", "品质检验"], "甲醛限量"),

    # === 采购指南 (5题) ===
    ("起订量最少多少米",          ["采购指南", "起订量"], "MOQ"),
    ("门幅是什么意思怎么量的",    ["采购指南", "门幅"], "门幅概念"),
    ("面料克重怎么看多少算厚",    ["采购指南", "克重"], "克重概念"),
    ("打样费用一般多少钱",        ["采购指南", "打样"], "样品费用"),
    ("FOB和CIF是什么意思",       ["采购指南", "FOB", "CIF"], "贸易术语"),

    # === 常见问题 (4题) ===
    ("染色色差怎么处理",          ["常见问题", "色差"], "色差解决"),
    ("缩水了怎么办怎么预防",      ["常见问题", "缩水"], "缩水解决"),
    ("面料起静电怎么处理",        ["常见问题", "静电"], "静电解决"),
    ("布料纬斜是什么原因怎么办",   ["常见问题", "纬斜"], "纬斜解决"),

    # === 场景推荐 (2题) ===
    ("冲锋衣用什么面料什么规格",   ["场景推荐", "冲锋衣"], "冲锋衣推荐"),
    ("做箱包用哪种牛津布比较好",   ["场景推荐", "箱包"], "箱包推荐"),

    # === 行业术语 (1题) ===
    ("坯布和大货有什么区别",      ["行业术语", "坯布", "大货"], "术语区别"),

    # === 知识库外 (1题) ===
    ("今天天气怎么样",           ["无"], "知识库外问题"),
]

# ============================================================
# 评估指标
# ============================================================
def is_relevant(result: Dict, expected_categories: List[str]) -> bool:
    """判断一条检索结果是否相关：类别匹配 或 文本中包含期望关键词"""
    # "无" 表示知识库外问题，任何结果都不应该命中
    if expected_categories == ["无"]:
        return False
    if not expected_categories:
        return False
    text = result["text"]
    category = result.get("category", "")
    for exp in expected_categories:
        if not exp:  # 跳过空字符串
            continue
        if exp in category or exp in text:
            return True
    return False


def recall_at_k(results: List[Dict], expected_categories: List[str], k: int) -> float:
    """前K个结果中至少有一个命中的比例（单个查询的二元值）"""
    if expected_categories == ["无"]:
        return None  # 知识库外问题，不参与评分
    if not expected_categories:
        return None
    top_k = results[:k]
    for r in top_k:
        if is_relevant(r, expected_categories):
            return 1.0
    return 0.0


def precision_at_k(results: List[Dict], expected_categories: List[str], k: int) -> float:
    """前K个结果中相关结果的比例"""
    if expected_categories == ["无"] or not expected_categories:
        return None
    top_k = results[:k]
    relevant = sum(1 for r in top_k if is_relevant(r, expected_categories))
    return relevant / k


def mrr(results: List[Dict], expected_categories: List[str]) -> float:
    """第一个正确答案的排名倒数：第1命中得1.0，第3命中得0.33"""
    if expected_categories == ["无"] or not expected_categories:
        return None
    for i, r in enumerate(results):
        if is_relevant(r, expected_categories):
            return 1.0 / (i + 1)
    return 0.0


# ============================================================
# 主评估
# ============================================================
def run_eval(top_k: int = 3):
    retriever = HybridRetriever()

    print(f"{'='*70}")
    print(f"检索质量评估（top_k={top_k}）")
    print(f"{'='*70}\n")

    recall_scores, precision_scores, mrr_scores = [], [], []
    total, skipped = 0, 0
    details = []  # 每题详情

    for query, expected_cats, reason in TEST_CASES:
        results = retriever.retrieve(query, top_k=top_k)

        r = recall_at_k(results, expected_cats, top_k)
        p = precision_at_k(results, expected_cats, top_k)
        m = mrr(results, expected_cats)

        # 知识库外的问题不参与评分
        if r is None:
            skipped += 1
            prefix = "⏭️ "
        elif r > 0:
            prefix = "✅"
        else:
            prefix = "❌"

        print(f"{prefix} [{reason}]")
        print(f"   🔍 {query}")
        print(f"   期望类别: {expected_cats}")
        if r is not None:
            print(f"   Recall@{top_k}={r:.0f}  Precision@{top_k}={p:.2f}  MRR={m:.2f}")
            recall_scores.append(r)
            precision_scores.append(p)
            mrr_scores.append(m)
        details.append({"query": query, "expected": expected_cats, "reason": reason,
                        "recall": r, "precision": p, "mrr": m,
                        "hits": [{"rank": i+1, "category": res["category"], "score": res["score"]}
                                 for i, res in enumerate(results)]})
        for i, res in enumerate(results):
            marker = " ← 命中" if is_relevant(res, expected_cats) else ""
            snippet = res["text"].split("\n")[0][:60]
            print(f"   #{i+1} [{res['category']}] {snippet}{marker}")
        total += 1
        print()

    # 汇总
    print(f"{'='*70}")
    print(f"汇总（{total} 题，{skipped} 题跳过）")
    print(f"{'='*70}")
    print(f"  Recall@{top_k}  avg: {sum(recall_scores)/len(recall_scores):.2%}")
    print(f"  Precision@{top_k} avg: {sum(precision_scores)/len(precision_scores):.2%}")
    print(f"  MRR            avg: {sum(mrr_scores)/len(mrr_scores):.2f}")

    # 逐题详情
    print(f"\n  未命中的查询:")
    failed = [tc for tc in TEST_CASES
              if recall_at_k(retriever.retrieve(tc[0], top_k=top_k), tc[1], top_k) == 0
              and recall_at_k(retriever.retrieve(tc[0], top_k=top_k), tc[1], top_k) is not None]
    if failed:
        for q, exp, reason in failed:
            print(f"    ❌ {q} → 期望 {exp}")
    else:
        print(f"    🎉 全部命中！")

    # 保存报告
    import json
    report_path = Path(__file__).parent.parent / "eval_results" / "eval_retrieval.json"
    report_path.parent.mkdir(exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "top_k": top_k,
            "total": total, "skipped": skipped,
            "Recall@3": f"{sum(recall_scores)/len(recall_scores):.2%}",
            "Precision@3": f"{sum(precision_scores)/len(precision_scores):.2%}",
            "MRR": f"{sum(mrr_scores)/len(mrr_scores):.2f}",
            "details": details,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n📄 报告: {report_path}")


if __name__ == "__main__":
    run_eval(top_k=3)
