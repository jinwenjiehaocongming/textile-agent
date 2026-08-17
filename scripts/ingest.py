"""
scripts/ingest.py
=================
原始文档 → 结构化 chunk 的自动流水线。

作用：
  把 data/raw/*.md（原始 markdown，标题分级）+ data/knowledge.txt（旧结构化）
  统一处理成 data/chunks.json，供 build_index.py 建索引。

设计（关键：元数据与正文分离，正文干净、不含 [类别]/[标签] 标记）：
  - 切块：按 markdown 标题自动切（## = 大类，### = 主题，正文 = chunk 内容）
  - 打标签：默认规则（标题+分类关键词，免费离线），--llm 开启 LLM 自动打标签
  - 输出：[{"text","category","tags","title"}, ...]

运行：
  python scripts/ingest.py            # 规则打标签（免费、离线）
  python scripts/ingest.py --llm      # LLM 打标签（更准，需 DeepSeek key）
"""

import argparse
import json
import re
import sys
from pathlib import Path

# 确保项目根在 sys.path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

RAW_DIR = PROJECT_ROOT / "data" / "raw"
OLD_KB = PROJECT_ROOT / "data" / "knowledge.txt"
OUTPUT = PROJECT_ROOT / "data" / "chunks.json"


# ============================================================
# 1. 解析旧结构化知识库（data/knowledge.txt）
#    格式：[类别] X / [标签] Y / 【标题】Z / --- 分隔
# ============================================================
def parse_old_kb(path: Path) -> list:
    """把旧结构化 txt 转成干净 chunk（正文不含标记）。"""
    raw = path.read_text(encoding="utf-8")
    chunks = []
    for block in raw.split("---"):
        block = block.strip()
        if not block or block.startswith("#"):
            continue
        cat = re.search(r"\[类别\]\s*(.+)", block)
        if not cat:
            continue  # 没有 [类别] 标记的块（如头部注释残留）不是有效知识，跳过
        tags = re.search(r"\[标签\]\s*(.+)", block)
        title = re.search(r"【(.+?)】", block)
        category = cat.group(1).strip()
        tag_text = tags.group(1).strip() if tags else ""
        t = title.group(1).strip() if title else ""

        # 正文 = 【标题】行之后的内容
        body = block
        if title:
            body = block.split("】", 1)[1] if "】" in block else block
        body = body.strip()
        if not body:
            continue
        # 标题并入正文：标题（如"腈纶""羽绒服"）是可检索内容，剥离会丢关键词
        text = f"{t}\n{body}" if t else body
        chunks.append({
            "text": text,
            "category": category,
            "tags": tag_text,
            "title": t,
        })
    return chunks


# ============================================================
# 2. 解析原始 markdown（data/raw/*.md）
#    ## = 大类，### = 主题，其下正文 = chunk 内容
# ============================================================
def parse_markdown(path: Path) -> list:
    """按 ## / ### 标题自动切块。"""
    chunks = []
    current_cat = ""
    current_title = ""
    body_lines = []

    def flush():
        nonlocal body_lines
        body = "\n".join(body_lines).strip()
        body_lines = []
        if current_title and body:
            # 标题并入正文（如"雪纺（乔其纱）"让"乔其纱"也可检索）
            chunks.append({
                "text": f"{current_title}\n{body}",
                "category": f"{current_cat}-{current_title}" if current_cat else current_title,
                "tags": "",  # 后面统一生成
                "title": current_title,
            })

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.rstrip()
        if line.startswith("## "):
            flush()
            current_cat = line[3:].strip()
            current_title = ""
        elif line.startswith("### "):
            flush()
            current_title = line[4:].strip()
        elif line.startswith("# "):
            continue  # 跳过文档总标题
        elif line.strip():
            body_lines.append(line.strip())
    flush()
    return chunks


# ============================================================
# 3. 规则打标签（免费、离线）
# ============================================================
def rule_tags(title: str, category: str, text: str) -> str:
    """从标题 + 分类 + 括号别名提取关键词。"""
    tags = []
    # 标题拆词（按括号、顿号、斜杠等）
    for part in re.split(r"[（）()、/，,·]", title or ""):
        part = part.strip()
        if part:
            tags.append(part)
    # 括号内别名
    for m in re.finditer(r"[（(]([^）)]+)[）)]", title or ""):
        tags.append(m.group(1).strip())
    # 分类的大类名
    prefix = (category or "").split("-")[0]
    if prefix:
        tags.append(prefix)
    # 去重保序
    return " ".join(dict.fromkeys(t for t in tags if t))


# ============================================================
# 4. LLM 打标签（可选，--llm 开启）
# ============================================================
TAG_PROMPT = """你是纺织行业术语专家。给下面这段面料知识提取 5~8 个检索关键词（标签），
用空格分隔，只输出关键词本身，不要序号、不要解释。

内容：
{text}

标签："""


def llm_tags(text: str) -> str:
    """用 DeepSeek 给单个 chunk 提取标签。"""
    import os
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage

    llm = ChatOpenAI(
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        model="deepseek-v4-flash",
        temperature=0,
        max_retries=2,
        timeout=30,
    )
    try:
        resp = llm.invoke([HumanMessage(content=TAG_PROMPT.format(text=text[:800]))])
        return (resp.content or "").strip()
    except Exception as e:
        print(f"    [LLM 打标签失败，回退规则] {str(e)[:50]}", file=sys.stderr)
        return ""


# ============================================================
# 5. 主流程
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="原始文档 → 结构化 chunk 流水线")
    parser.add_argument("--llm", action="store_true", help="用 LLM 打标签（需 DeepSeek key）")
    args = parser.parse_args()

    chunks = []

    # 旧结构化知识库
    if OLD_KB.exists():
        old = parse_old_kb(OLD_KB)
        chunks.extend(old)
        print(f"✅ 旧知识库: {len(old)} 块")

    # 原始 markdown
    if RAW_DIR.exists():
        md_files = sorted(RAW_DIR.glob("*.md"))
        for f in md_files:
            parsed = parse_markdown(f)
            chunks.extend(parsed)
            print(f"✅ 原始文档 {f.name}: {len(parsed)} 块")
    else:
        print(f"⚠️ 未找到 {RAW_DIR}")

    if not chunks:
        print("❌ 没有可处理的语料")
        return 1

    # 去重：按「大类 + 规范化标题」去重（"涤纶（聚酯纤维）" 归一为 "涤纶"），保留正文更丰富的那条
    def _norm_title(title: str) -> str:
        # 去括号内容，再只保留中文字符（去掉英文/数字），用于近似去重
        base = re.sub(r"[（(].*?[）)]", "", title or "")
        cn = re.sub(r"[^\u4e00-\u9fff]", "", base).strip()
        return cn if cn else (title or "").strip()

    merged = {}
    for c in chunks:
        major = c["category"].split("-")[0]
        key = (major, _norm_title(c["title"]))
        if key not in merged or len(c["text"]) > len(merged[key]["text"]):
            merged[key] = c
    deduped = list(merged.values())
    print(f"  去重: {len(chunks)} → {len(deduped)} 块")

    # 打标签
    for i, c in enumerate(deduped, 1):
        if not c["tags"]:
            if args.llm:
                c["tags"] = llm_tags(c["text"])
            else:
                c["tags"] = rule_tags(c["title"], c["category"], c["text"])
        print(f"  [{i}/{len(deduped)}] {c['category']}")

    # 写 JSON
    OUTPUT.write_text(json.dumps(deduped, ensure_ascii=False, indent=2), encoding="utf-8")

    # 汇总
    from collections import Counter
    cat_counter = Counter(c["category"].split("-")[0] for c in deduped)
    print(f"\n📄 已写入 {OUTPUT}（{len(deduped)} 块）")
    print("分类分布：")
    for cat, n in cat_counter.most_common():
        print(f"  {cat}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
