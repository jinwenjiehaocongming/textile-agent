"""管理员数据分析 Agent —— 分析图（LangGraph）
=============================================
一句话问题 → 规划 → 生成只读 SQL → 执行 → 自纠错 → 结论 → 图表 spec。

节点与边：

    planner ──► coder ──► executor ──► critic ──┬─(retry ≤2)─► coder
                                                 ├─(还有步骤)─► coder
                                                 └─(全部完成)─► reporter ──► charter ──► END

与参考实现（Insight）对齐的地方：7 类节点、声明式图表 spec、reporter 反编造。
**这里额外做的三件事**（都是参考实现没有的，也是这个模块能站住脚的原因）：

1. **图表数据不经过 LLM**：LLM 只输出「用哪条结果 + 图型 + 哪列做 x + 哪几列做系列」，
   真正的数值由程序从 executor 的结果里取。参考实现让 LLM 输出图表内容，
   一旦模型编数，图上的柱子就是假的——而图恰恰是最容易被相信的东西。
2. **结论里的数字做程序校验**：reporter 写完结论后，逐个数字回查结果集；
   查不到的数字不会被静默忽略，而是列进"数据局限声明"里给管理员看。
3. **取数走同一个只读安全层**（``src/analytics/sql.py`` 四层防护），
   与 MCP `analytics_server` 是同一套实现（那个是给其它 Agent 用的工具面）。
"""

import json
import os
import re
import time
from typing import Annotated, Any, Optional, TypedDict

from src.analytics import sql as analytics_sql
from src.analytics import schema_hints
from src.logging_config import get_logger

logger = get_logger(__name__)

# 步数默认 2：每步 ≈ 2 次 LLM 调用（coder + critic），而实测单次调用 10-30 秒
# （输出越长越慢），3 步以上一次分析要 5 分钟以上，对交互式界面不可用。
MAX_STEPS = max(1, int(os.getenv("ANALYTICS_MAX_STEPS", "2")))
MAX_RETRY = 2            # 每步 SQL 自纠错最多 2 次
PREVIEW_ROWS = 12        # 放进 prompt / 事件的预览行数

# 前端「快捷问题」用（也提示管理员这个 Agent 能回答什么）
EXAMPLES = [
    "最近的质量投诉有什么规律？集中在哪些产品？",
    "各品类的销售额和订单量对比",
    "哪些产品被问得多、下单少？",
    "近 8 个月的 GMV 趋势如何？",
    "退款率最高的颜色是哪些？",
    "大客户贡献了多少销售额？",
]

NODE_LABELS = {
    "planner": "分析规划",
    "coder": "生成 SQL",
    "executor": "只读执行",
    "critic": "结果校验",
    "reporter": "撰写结论",
    "charter": "图表选型",
}


class AnalyticsState(TypedDict, total=False):
    question: str
    plan: list           # [{step, status}]
    step_index: int
    sql: str
    attempts: int
    feedback: str
    records: Annotated[list, "每步执行记录"]
    report: str
    chart_intents: list   # reporter 给的图表意图（LLM 只选图型/列名，数据由程序填）
    charts: list          # charter 用真实数据拼好的、可直接渲染的 spec
    notes: list
    error: str


# ============================================================
# Prompt
# ============================================================

PLANNER_PROMPT = """你是数据分析师。把用户的问题拆成 {max_steps} 步以内、可用 SQL 回答的具体步骤。

{semantics}

用户问题：{question}

要求：
- 每步是一个**可独立用一条 SQL 查出结果**的问题，措辞具体（说清维度/指标/时间范围）。
- 步骤间可以有先后（后一步基于前一步的发现），但不要重复。
- 只输出 JSON，不要解释：{{"steps": ["步骤1", "步骤2"]}}"""

CODER_PROMPT = """你是 SQL 工程师。根据步骤写出**一条只读 SQL**（PostgreSQL）。

{semantics}

用户原始问题：{question}
已完成的步骤与结果摘要：
{history}

当前步骤：{step}
{feedback}

要求：
- 只输出 SQL 本身（SELECT/WITH 开头，单条语句，不要分号、不要 markdown 代码块）。
- **写成紧凑的一行**：不要缩进/换行/多余 CTE（输出越短越快，且更容易看懂）。
- 必须用上面的表关系做 JOIN（本库没有外键）。注意时间列类型（TEXT 需 ::timestamp）。
- 聚合查询请给列起英文别名（如 AS category, AS gmv）。"""

CRITIC_PROMPT = """你是数据质量校验员。判断这次查询结果是否可以采信。

当前步骤：{step}
SQL：
{sql}

执行结果：
{result}

判断标准（任一不满足则 retry）：
- 执行报错；
- 结果为空，但该步骤本应有数据（表里有相关记录时不该为空）；
- **分母/口径明显错**（把"退款率"算成退款笔数、未排除已取消订单、用错时间列、
  分母用了别的月份/别的范围的数量）。

注意：**不要**因为"退款金额由被退款订单的 orders.total 汇总而来"而判 retry ——
refunds 表没有金额列，这是本项目约定的口径。

只输出极简 JSON（reason 不超过 40 字）：{{"verdict": "ok"|"retry", "reason": "..."}}"""

REPORTER_PROMPT = """你是资深数据分析师，向业务负责人汇报，并顺手给可视化建议。

用户问题：{question}

以下是**唯一的**数据来源（每一步的真实查询结果）：
{evidence}

写作要求（最重要）：
- **结论里出现的每一个数字，必须能在上面的结果原文中找到**；找不到就不要写。
- 不要编造字段、不要外推、不要用"大约/预计"去猜没有查的数。
- 结构：先说结论（1-3 句），再给关键发现（2-4 条，带数字），最后如有必要给 1 条建议。
- 用中文，150-300 字，不要 markdown 标题层级，不要输出表格。

最后额外给出图表意图（**只选图型和列名，不要给数据**，数据由程序从真实结果里取）：
{{"report": "上面那段结论原文", "charts": [{{"record_index": 0, "chart_type": "bar",
  "title": "标题", "x": "分类/时间列名", "series": ["数值列名"]}}]}}

选型规则：分类对比→bar，时间趋势→line，占比→pie，排名→hbar；
只能用结果里真实存在的列名；x 要能唯一标识每一行；行数 <2 的结果不要画。
整体只输出这一个 JSON 对象。"""

# ============================================================
# 工具函数
# ============================================================

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def _clean_sql(text: str) -> str:
    """从模型输出里抠出 SQL：去代码块围栏、去结尾分号、只保留第一条语句。"""
    t = (text or "").strip()
    t = _FENCE_RE.sub("", t).strip()
    if t.lower().startswith("sql"):
        t = t[3:].strip()
    t = t.split(";")[0].strip()
    return t


def extract_json(text: str) -> Optional[dict]:
    """从模型输出里稳健地抠 JSON（允许前后有解释文字/代码块）。"""
    t = (text or "").strip()
    t = _FENCE_RE.sub("", t).strip()
    try:
        return json.loads(t)
    except (ValueError, TypeError):
        pass
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(t[start:end + 1])
    except (ValueError, TypeError):
        return None


_FK_CACHE = {"text": None, "ts": 0.0}
_FK_TTL = 300          # 外键关系变化很慢，缓存 5 分钟足够（避免每个节点都查一次库）


async def _semantics() -> str:
    """语义层文本：表结构/口径手写 + **表关系从数据库外键自动读取**。"""
    now = time.time()
    if _FK_CACHE["text"] is None or now - _FK_CACHE["ts"] > _FK_TTL:
        _FK_CACHE["text"] = await schema_hints.load_fk_text()
        _FK_CACHE["ts"] = now
    return schema_hints.semantic_hints(_FK_CACHE["text"])


def reset_semantics_cache() -> None:
    """清缓存（测试/库结构变化后用）。"""
    _FK_CACHE["text"] = None
    _FK_CACHE["ts"] = 0.0


async def _ask(llm, prompt: str) -> str:
    """调一次 LLM，返回纯文本（节点里统一走这里，方便测试替换）。"""
    resp = await llm.ainvoke([{"role": "user", "content": prompt}])
    return (getattr(resp, "content", None) or str(resp)).strip()


def _records_evidence(records: list) -> str:
    """把执行记录整理成"证据文本"（结论与图表都只能引用它）。"""
    if not records:
        return "（没有任何查询结果）"
    blocks = []
    for i, r in enumerate(records):
        head = f"[结果 {i}] 步骤：{r.get('step', '')}\nSQL: {r.get('sql', '')}"
        if not r.get("ok"):
            blocks.append(head + f"\n执行失败：{r.get('error', '')}")
            continue
        block = [head, f"列：{r.get('columns')}", f"共 {r.get('row_count')} 行"]
        for row in (r.get("rows") or [])[:PREVIEW_ROWS]:
            block.append("  " + " | ".join("" if v is None else str(v) for v in row))
        if (r.get("row_count") or 0) > PREVIEW_ROWS:
            block.append(f"  ...（还有 {r['row_count'] - PREVIEW_ROWS} 行未展示；"
                         f"如需完整数据请给出聚合后的 SQL）")
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


# ── 反编造：结论里的数字必须在结果里找得到 ──────────────────

# 数字 + 可选中文量级单位（"838.6 万" / "1 个亿"）：必须把量级算进去再比对，
# 否则"1 个亿"里的 "1" 会被"小整数不追责"放过 —— 那正好是最该抓的编造。
_NUM_RE = re.compile(r"(\d[\d,]*\.?\d*)\s*(?:个)?\s*(千万|亿|万)?")
_UNIT_SCALE = {"": 1.0, "万": 1e4, "亿": 1e8, "千万": 1e7}
# 这些不是"指标数字"，不该被当成编造：
_DATE_RE = re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}(\s*[-/月]\s*\d{1,2})?")     # 2026-07 / 2026年7月
_WINDOW_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:天|个月|年|月|日|小时|周)")           # 最近 90 天
# ⚠️ 必须用 ASCII 字符集 [A-Za-z0-9_-]：Python 的 \w **匹配中文**，
# 用 \w 会让 "P0016退款率0.1702" 里的标识符吞掉后面的中文和数字的前导 0
#（实测："0.1702" 被削成 ".1702" → 提取出 1702 → 误报编造）
_IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\d[A-Za-z0-9_-]*|\d+[A-Za-z][A-Za-z0-9_-]*")
# 这些"数字"不追责：TopN、步骤序号、天数等小整数（**仅限没有量级单位时**）
_SAFE_NUM_MAX = 31


def collect_values(records: list) -> set:
    """收集**证据文本里真的出现过**的所有数值（用于校验结论）。

    判定标准只有一条：这个数字有没有可能出现在给模型看的证据里（`build_evidence`）。
    两个方向都踩过坑：

    - **漏收 → 冤枉模型**：`row_count` 印在"共 221 行"里，不收就会把"结果共 221 行"
      判成编造；
    - **多收 → 放走编造**：`r["rows"]` 可能存了 2000 行，但证据只印前 `PREVIEW_ROWS` 行，
      把未展示行的数字也收进池子，编造的数字只要撞上"模型根本没看到的那几行"就能过关。

    所以这里严格按证据文本收：预览行 + `共 N 行` + 截断提示里那句
    "还有 N-12 行未展示"（这句把 `PREVIEW_ROWS` 和差值都写进了证据）。
    """
    values = set()
    for r in records:
        for meta in ("row_count", "elapsed_ms"):
            v = r.get(meta)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                values.add(round(float(v), 4))
        rc = r.get("row_count") or 0
        if rc > PREVIEW_ROWS:
            # 证据里写的是"...（还有 {row_count - PREVIEW_ROWS} 行未展示；...）"
            values.add(float(PREVIEW_ROWS))
            values.add(float(rc - PREVIEW_ROWS))
        for row in (r.get("rows") or [])[:PREVIEW_ROWS]:
            for cell in row:
                if isinstance(cell, bool) or cell is None:
                    continue
                if isinstance(cell, (int, float)):
                    values.add(round(float(cell), 4))
                elif isinstance(cell, str):
                    m = _NUM_RE.fullmatch(cell.strip())
                    if m:
                        try:
                            values.add(round(float(m.group(0).replace(",", "")), 4))
                        except ValueError:
                            pass
    return values


def num_close(a: float, b: float, tolerance: float = 0.15) -> bool:
    """两个数是否"同一个数"（容差按量级决定）。

    ⚠️ 不能给统一的绝对地板（比如 ±0.51）：比率是 0~1 的小数，
    0.009 和 0.1667 在 ±0.51 下会被判成"相等" —— 那会让编造的数字蒙混过关
    （实测：评测里 0.9 与 0.009 误判命中）。所以：小于 1 的数只用相对容差。
    """
    scale = max(abs(a), abs(b))
    if scale < 1:
        return abs(a - b) <= max(1e-6, scale * max(tolerance, 0.05))
    return abs(a - b) <= max(0.51, scale * tolerance)


def _number_supported(num: float, values: set, has_unit: bool = False,
                      percent: bool = False) -> bool:
    """这个数字能否由结果集解释（允许四舍五入 / 万·亿单位 / 百分比取整）。

    ``has_unit`` 为真表示原文带了量级单位（"1 个亿"）——这时**不再享受小整数豁免**。
    ``percent`` 为真表示原文写了百分号（唯一的 ×100 放行条件，见 `_derivable`）。
    """
    if not has_unit and num <= _SAFE_NUM_MAX and float(num).is_integer():
        return True                     # TopN、步骤序号、天数等小整数不追责
    if _derivable(num, values, percent=percent):
        return True                     # 可由结果里的数字简单推算，见 _derivable
    for v in values:
        if v == 0:
            continue
        if num_close(v, num, 0.01):                      # 同量级
            return True
        # 0.097 → "9.7%"：**同样只在原文带 % 时放行**。
        # 这里踩的坑和 `_derivable` 一模一样（两处都有 ×100，只修一处不够）：
        # 池子里有 10 时，10×100 = 1000 与 999 相差 1，落在 1% 容差内 →
        # 编造的"999 次"被判成有出处。百分比是唯一需要 ×100 的形态。
        if percent and num_close(v * 100, num, 0.01):
            return True
    return False


def _mask_non_metrics(report: str, string_cells: set) -> str:
    """把"不是指标"的数字抹掉：日期、时间窗口、订单号/产品号等标识符。

    不这么做会大量误报（实测："最近 90 天"的 90、P0163 的 0163、2026-07 的 2026
    全被当成编造数字），误报多了管理员就不看告警了——告警要有信噪比。
    """
    text = _DATE_RE.sub(" ", report or "")
    text = _WINDOW_RE.sub(" ", text)

    def _ident(m):
        tok = m.group(0)
        # 是已知的字符串单元格（如 P0163），或形如"字母+数字"的编号 → 不算指标
        return " " if (tok in string_cells or re.search(r"[A-Za-z]", tok)) else tok

    return _IDENT_RE.sub(_ident, text)


def collect_string_cells(records: list) -> set:
    """结果里的非数值单元格（产品号/订单号/颜色等），用于识别标识符。"""
    out = set()
    for r in records:
        for row in (r.get("rows") or []):
            for cell in row:
                if isinstance(cell, str) and cell.strip() and not _NUM_RE.fullmatch(cell.strip()):
                    out.add(cell.strip())
    return out


def extract_numbers(text: str) -> list:
    """从文本里抽取"指标数字"，并把中文量级单位换算成绝对值（"838.6 万" → 8386000）。

    日期/时间窗口/编号（P0163、ORD-…）不算指标 —— 与反编造检查同一套口径，
    评测脚本也复用它，避免"两套判定标准"。
    """
    masked = _mask_non_metrics(text, set())
    out = []
    for m in _NUM_RE.finditer(masked):
        raw, unit = m.group(1).replace(",", ""), (m.group(2) or "")
        if raw in ("", "."):
            continue
        try:
            out.append(float(raw) * _UNIT_SCALE.get(unit, 1.0))
        except ValueError:
            continue
    return out


_DERIVE_CAP = 120          # 参与两两推算的取值上限（避免 O(n²) 爆炸）


def _derivable(num: float, values: set, percent: bool = False) -> bool:
    """该数字能否由结果里的两个数字**简单推算**出来（加减乘除）。

    为什么需要：结论里出现「结果共 84 行，未展示 72 行」是完全合理的（84-12），
    但 72 并不原样出现在结果里 —— 只认原样出现会把这类正确结论判成编造（实测踩过）。
    加一层两两推算：既不放过凭空数字，也不冤枉推算出来的数字。

    ``percent``：原文是否写了百分号。**这个开关是必须的** —— 无条件放行 `cand*100`
    时，池子里只要有 11 和 1，`(11-1)*100 = 1000 ≈ 999`（2% 容差内）就成立，
    于是**任何接近整百的编造数字都能被"推算"出来**（实测：999 次询价被判为有出处）。
    百分比是唯一需要 ×100 的形态，所以只对带 % 的数字开这个口子。
    """
    pool = [v for v in list(values)[:_DERIVE_CAP] if isinstance(v, (int, float))]
    for a in pool:
        for b in pool:
            if b == 0:
                continue
            cands = [a + b, a - b, a * b, a / b]
            if percent:
                # 也接受百分比形态（推算得 0.1429，报告里写 14.29%）
                cands += [c * 100 for c in cands]
            for cand in cands:
                if num_close(cand, num, 0.02):
                    return True
    return False


def check_report_numbers(report: str, records: list) -> list:
    """返回结论里**无法用结果集解释**的数字（可能被编造）。

    原文带量级单位时先换算成绝对量级再比对（"838.6 万" → 8,386,000），
    这样既不会误报"838.6 万"，也不会放过"1 个亿"。
    """
    values = collect_values(records)
    masked = _mask_non_metrics(report, collect_string_cells(records))
    unsupported = []
    for m in _NUM_RE.finditer(masked):
        raw, unit = m.group(1).replace(",", ""), (m.group(2) or "")
        if raw in ("", "."):
            continue
        try:
            magnitude = float(raw) * _UNIT_SCALE.get(unit, 1.0)
        except ValueError:
            continue
        # 紧跟在数字后面的是不是百分号（"14.29%" / "14.29 %"）——决定是否允许 ×100 推算
        percent = bool(re.match(r"\s*%", masked[m.end():]))
        if not _number_supported(magnitude, values, has_unit=bool(unit), percent=percent):
            unsupported.append(raw + unit)
    return unsupported


# ============================================================
# 节点
# ============================================================

def build_graph(llm, max_steps: int = 0):
    """把节点拼成图。``llm`` 注入（测试可传假 LLM，不触网）。

    ``max_steps``：覆盖规划步数上限（0=用默认 MAX_STEPS）。步数是延迟的主要来源
    （每步 ≈ 2 次 LLM 调用，单次 10-30 秒），所以 UI 上给"快速（1 步）/ 标准（2 步）"两档。
    """
    from langgraph.graph import END, StateGraph

    step_cap = max(1, int(max_steps or MAX_STEPS))

    async def planner(state: AnalyticsState) -> dict:
        prompt = PLANNER_PROMPT.format(max_steps=step_cap, semantics=await _semantics(),
                                       question=state["question"])
        data = extract_json(await _ask(llm, prompt)) or {}
        steps = [s for s in (data.get("steps") or []) if isinstance(s, str) and s.strip()][:step_cap]
        if not steps:
            steps = [state["question"]]        # 兜底：至少把原问题当成一步
        return {"plan": [{"step": s, "status": "pending"} for s in steps], "step_index": 0,
                "attempts": 0, "feedback": "", "records": [], "notes": []}

    async def coder(state: AnalyticsState) -> dict:
        idx = state.get("step_index", 0)
        step = state["plan"][idx]["step"]
        history = "\n".join(
            f"- {r['step']} → 成功 {r.get('row_count', 0)} 行" if r.get("ok")
            else f"- {r['step']} → 失败：{r.get('error', '')[:80]}"
            for r in state.get("records", [])
        ) or "（无）"
        feedback = ""
        if state.get("feedback"):
            feedback = f"\n上一次尝试失败/被质疑，请修正：{state['feedback']}\n上一次 SQL：{state.get('sql', '')}"
        prompt = CODER_PROMPT.format(semantics=await _semantics(), question=state["question"],
                                     history=history, step=step, feedback=feedback)
        sql = _clean_sql(await _ask(llm, prompt))
        return {"sql": sql, "feedback": ""}

    async def executor(state: AnalyticsState) -> dict:
        idx = state.get("step_index", 0)
        step = state["plan"][idx]["step"]
        result = await analytics_sql.run_readonly_sql(state.get("sql", ""))
        record = {
            "step": step,
            "sql": state.get("sql", ""),
            "ok": result["ok"],
            "error": result.get("error", ""),
            "columns": result["columns"],
            "rows": result["rows"],
            "row_count": result["row_count"],
            "truncated": result["truncated"],
            "elapsed_ms": result["elapsed_ms"],
            "attempts": state.get("attempts", 0) + 1,
        }
        records = list(state.get("records", []))
        # 同一步骤的重试：替换掉上一条失败记录，避免记录里堆垃圾
        if records and records[-1]["step"] == step and not records[-1]["ok"]:
            records[-1] = record
        else:
            records.append(record)
        return {"records": records, "attempts": state.get("attempts", 0) + 1,
                "error": result.get("error", "")}

    async def critic(state: AnalyticsState) -> dict:
        idx = state.get("step_index", 0)
        attempts = state.get("attempts", 0)
        step = state["plan"][idx]["step"]
        record = (state.get("records") or [{}])[-1]
        result_text = "执行失败：" + record.get("error", "") if not record.get("ok") else \
            f"列：{record.get('columns')}；返回 {record.get('row_count')} 行\n" + \
            "\n".join(" | ".join("" if v is None else str(v) for v in row)
                      for row in (record.get("rows") or [])[:5])
        prompt = CRITIC_PROMPT.format(step=step, sql=state.get("sql", ""), result=result_text)
        data = extract_json(await _ask(llm, prompt)) or {}
        verdict = (data.get("verdict") or "ok").lower()
        reason = data.get("reason") or ""
        if verdict not in ("ok", "retry"):
            verdict = "ok"
        if not record.get("ok"):
            verdict = "retry"          # 程序兜底：执行没成功必须重试，不完全交给模型判断

        notes = list(state.get("notes", []))
        if verdict == "retry" and attempts <= MAX_RETRY:
            # 还有重试额度 → 带着反馈回到 coder 重写 SQL
            return {"feedback": reason or "结果不可信，请修正 SQL",
                    "plan": _mark(state, idx, "pending")}

        give_up = verdict == "retry"        # 走到这里 = 重试额度用尽
        plan = _mark(state, idx, "failed" if give_up else "done")
        if give_up:
            # 如实降级：不静默、不死循环，把"这一步没查成"告诉管理员
            notes.append(f"⚠️ 步骤「{step}」在 {attempts} 次尝试后仍未通过校验"
                         f"（{reason or 'SQL 执行失败'}），该步骤没有采信。")
        # 推进到下一步（**必须在这里递增**：路由函数只返回节点名，不会改 state，
        # 初版忘了递增导致第 1 步被无限重跑）
        return {"feedback": "", "plan": plan, "step_index": idx + 1, "attempts": 0,
                "notes": notes}

    async def reporter(state: AnalyticsState) -> dict:
        evidence = _records_evidence(state.get("records", []))
        prompt = REPORTER_PROMPT.format(question=state["question"], evidence=evidence)
        raw = await _ask(llm, prompt)
        # 期望格式：{"report": "...", "charts": [...]}；模型不配合时退化成"整段当结论"
        data = extract_json(raw) or {}
        report = (data.get("report") or "").strip() or raw
        intents = data.get("charts") or []
        unsupported = check_report_numbers(report, state.get("records", []))
        notes = list(state.get("notes", []))
        if unsupported:
            # 不是静默丢弃，也不是假装没发生：明确告诉管理员哪些数字没查到出处
            notes.append("⚠️ 以下数字未能与查询结果对上，请谨慎采信：" + "、".join(unsupported[:8]))
        failed = [r for r in state.get("records", []) if not r.get("ok")]
        if failed:
            notes.append(f"⚠️ 有 {len(failed)} 个步骤执行失败，结论可能不完整：" +
                         "；".join(r["step"] for r in failed))
        if any(r.get("truncated") for r in state.get("records", [])):
            notes.append("⚠️ 部分结果触发了行数上限被截断，聚合值仍准确，明细可能不全。")
        notes.append("数据说明：库内含演示数据（customer_id 以 demo_ 开头的客户），"
                     "趋势与结构分析已包含它们。")
        return {"report": report, "notes": notes, "chart_intents": intents}

    async def charter(state: AnalyticsState) -> dict:
        """纯程序节点：把 reporter 给的"图表意图" + **真实结果数据** 拼成可渲染 spec。

        这里**不调 LLM**：① 省一次调用（实测单次 10-30 秒）；② 图上的数字永远来自
        查询结果，模型没有机会编造柱子的高度。
        """
        charts, notes = [], list(state.get("notes", []))
        for spec in (state.get("chart_intents") or [])[:3]:
            built = _build_chart(spec, state.get("records", []))
            if built:
                charts.append(built)
        if state.get("chart_intents") and not charts:
            notes.append("⚠️ 模型建议的图表因列名/数据不匹配未生成（结果里没有可用的分类列）。")
        return {"charts": charts, "notes": notes}


    def route_after_critic(state: AnalyticsState) -> str:
        """critic 已经决定好"重试/推进/放弃"并把 state 改到位，这里只按状态选边。"""
        if state.get("feedback"):
            return "coder"                                   # 同一步骤重写 SQL
        if state.get("step_index", 0) < len(state.get("plan", [])):
            return "coder"                                   # 进入下一步
        return "reporter"                                    # 全部完成 → 写结论

    g = StateGraph(AnalyticsState)
    g.add_node("planner", planner)
    g.add_node("coder", coder)
    g.add_node("executor", executor)
    g.add_node("critic", critic)
    g.add_node("reporter", reporter)
    g.add_node("charter", charter)
    g.set_entry_point("planner")
    g.add_edge("planner", "coder")
    g.add_edge("coder", "executor")
    g.add_edge("executor", "critic")
    g.add_conditional_edges("critic", route_after_critic, {"coder": "coder", "reporter": "reporter"})
    g.add_edge("reporter", "charter")
    g.add_edge("charter", END)
    return g.compile()


def _mark(state: AnalyticsState, idx: int, status: str) -> list:
    plan = [dict(p) for p in state.get("plan", [])]
    if 0 <= idx < len(plan):
        plan[idx]["status"] = status
    return plan


def _build_chart(spec: dict, records: list) -> Optional[dict]:
    """把 LLM 的图表选用意图，加上**真实数据**，拼成前端可直接渲染的 spec。"""
    try:
        idx = int(spec.get("record_index", -1))
    except (TypeError, ValueError):
        return None
    if not (0 <= idx < len(records)):
        return None
    rec = records[idx]
    if not rec.get("ok"):
        return None
    columns = rec.get("columns") or []
    x = spec.get("x")
    series = [c for c in (spec.get("series") or []) if c in columns]
    chart_type = spec.get("chart_type") if spec.get("chart_type") in ("bar", "line", "pie", "hbar") else "bar"
    if x not in columns or not series:
        return None

    def _unique(col: str) -> bool:
        vals = [str(row[columns.index(col)]) for row in (rec.get("rows") or [])]
        return len(set(vals)) == len(vals)

    if not _unique(x):
        # x 有重复值（比如用"颜色"当轴、但每行还带品类，两个"黑色"）→ 换一个能唯一
        # 标识行的分类列；没有就放弃这张图（重复的轴会让人读出错误的对比）
        better = [c for c in columns
                  if c != x and c not in series and _unique(c)
                  and not all(isinstance(row[columns.index(c)], (int, float))
                              for row in (rec.get("rows") or []) if row[columns.index(c)] is not None)]
        if not better:
            return None
        x = better[0]
    xi = columns.index(x)
    rows = []
    for row in (rec.get("rows") or [])[:50]:
        item = {"x": row[xi]}
        for s in series:
            try:
                item[s] = float(row[columns.index(s)]) if row[columns.index(s)] is not None else 0.0
            except (TypeError, ValueError):
                item[s] = 0.0
        rows.append(item)
    return {"chart_type": chart_type, "title": (spec.get("title") or "查询结果")[:60],
            "x": x, "series": series, "data": rows, "source_record": idx,
            "source_step": rec.get("step", ""), "sql": rec.get("sql", "")}


# ============================================================
# 对外入口
# ============================================================

def _initial_state(question: str) -> AnalyticsState:
    return {"question": question, "plan": [], "step_index": 0, "sql": "", "attempts": 0,
            "feedback": "", "records": [], "report": "", "charts": [], "notes": [], "error": ""}


async def analyze(question: str, llm=None, max_steps: int = 0) -> dict:
    """跑完整条流水线，返回最终 state（测试/非流式调用用）。"""
    from src.agent import get_analytics_llm
    graph = build_graph(llm or get_analytics_llm(), max_steps=max_steps)
    return await graph.ainvoke(_initial_state(question))


async def analyze_stream(question: str, llm=None, max_steps: int = 0):
    """流式跑流水线，逐个节点产出事件（SSE 用）。

    事件类型：start / node / plan / sql / rows / critic / retry / report / chart / done / error
    """
    from src.agent import get_analytics_llm
    graph = build_graph(llm or get_analytics_llm(), max_steps=max_steps)
    started = time.perf_counter()
    yield {"type": "start", "question": question}
    records: list = []
    states: dict = {}
    try:
        async for chunk in graph.astream(_initial_state(question), stream_mode="updates"):
            for node, update in (chunk or {}).items():
                update = update or {}
                states.update(update)
                yield {"type": "node", "node": node, "label": NODE_LABELS.get(node, node)}
                if node == "planner":
                    yield {"type": "plan", "steps": update.get("plan", [])}
                elif node == "coder":
                    yield {"type": "sql", "sql": update.get("sql", "")}
                elif node == "executor":
                    records = update.get("records", records)
                    rec = records[-1] if records else {}
                    yield {"type": "rows", "ok": rec.get("ok"), "columns": rec.get("columns", []),
                           "rows": (rec.get("rows") or [])[:50], "row_count": rec.get("row_count", 0),
                           "truncated": rec.get("truncated", False),
                           "elapsed_ms": rec.get("elapsed_ms", 0), "step": rec.get("step", ""),
                           "sql": rec.get("sql", ""), "error": rec.get("error", "")}
                elif node == "critic":
                    retry = bool(update.get("feedback"))
                    yield {"type": "critic", "retry": retry, "reason": update.get("feedback", "")}
                    if retry:
                        yield {"type": "retry", "reason": update.get("feedback", ""),
                               "attempt": states.get("attempts", 0)}
                elif node == "reporter":
                    yield {"type": "report", "content": update.get("report", ""),
                           "notes": update.get("notes", [])}
                elif node == "charter":
                    yield {"type": "chart", "charts": update.get("charts", [])}
    except Exception as e:  # noqa: BLE001
        logger.exception("[分析] 流水线异常: %s", e)
        yield {"type": "error", "content": f"分析失败：{str(e)[:200]}"}
        return
    yield {"type": "done", "elapsed_ms": int((time.perf_counter() - started) * 1000),
           "steps": len(records), "sql_count": len({r.get("sql") for r in records if r.get("sql")})}
