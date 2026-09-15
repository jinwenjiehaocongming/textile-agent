"""分析语义层（给 LLM 的"业务字典"）
====================================
Text-to-SQL 的质量取决于**喂给模型的上下文**。参考实现靠"运行时读外键"自动生成
JOIN 关系——但**本项目的库一个外键都没有**（实测 0 个），所以关系、枚举、口径、
以及若干"不看就会算错"的坑，必须在这里手写。

这份内容的两个用途：
1. 拼进 planner/coder 的 prompt（让 LLM 知道表和表怎么连、口径怎么算）；
2. 作为评审依据：口径写清楚了，"同一个问题每次算出同一个数"才成立。

维护约定：**改了表结构/口径，必须同步这里**，否则 LLM 会按旧口径生成 SQL。
"""

import os

# ── 表与用途 ──────────────────────────────────────────────────
TABLES = {
    "users": "账号（客户与管理员）",
    "products": "面料产品目录（281 条，含品类/颜色/价格/库存）",
    "orders": "订单（业务核心）",
    "refunds": "退款工单（挂在订单号上）",
    "sessions": "**聊天会话**（多会话列表用；注意不是登录会话，登录会话在 Redis）",
    "conversations": "聊天消息（user_id + session_id 定位，含客户询价原文）",
    "profile": "用户级 KV（目前只存 last_query_type：chat / place_order / after_sales）",
    "audit_log": "操作审计（谁、何时、做了什么）",
    "pending_approvals": "HITL 人工审批单（下单前的审批状态机）",
}

# ── 关系 ───────────────────────────────────────────────────
# 2026-09 起：**表关系优先从数据库外键自动读取**（见 ``load_fk_text()``）。
# 这个手写版本只在"库里还没有外键"时兜底（比如刚 clone 下来还没跑迁移），
# 或者用来补充**外键表达不了**的语义关系。
#
# 演进故事（面试可讲）：一开始库没有外键、也没有给业务表加外键的条件
#（63 个 user_id 在业务数据里出现但 users 里没有行），所以关系只能手写；
# 后来立起"所有被接受的身份都有账号行"的不变式（users.ensure_user_row）、清掉 313 行
# 历史孤儿、补上 6 条外键，关系就改成运行时自读了 —— **手写的东西越少，越不容易漂移**。
RELATIONS = """
orders.customer_id      = users.id           -- 订单属于哪个客户
orders.product_id       = products.id        -- 订单买的哪个产品
refunds.order_no        = orders.order_no    -- 退款挂在哪张订单上
conversations.user_id   = users.id           -- 消息属于哪个客户
conversations.session_id = sessions.id       -- 消息属于哪个聊天会话
profile.user_id         = users.id           -- 用户级 KV
pending_approvals.user_id / thread_id = users.id   -- 待审批单属于哪个客户
pending_approvals.order_no = orders.order_no       -- 审批通过后生成的订单（可能为空）
audit_log.actor         = users.id 或管理员标识（**自由文本，不保证能关联上 users**）
"""

# ── 枚举值（状态是自由文本，没有 CHECK 约束，必须显式告知）──
ENUMS = """
orders.status            ∈ ('待付款','已付款','已发货','已收货','退款中','已退款','已取消')
                           退款中 = 有未决退款工单（已进退款流程，钱还没退出去）
                           已退款 = 退款已审核通过（终态）；已取消 = 未成交（终态）
refunds.status           ∈ ('待审核','已通过','已驳回')
users.role               ∈ ('customer','admin')
users.status             ∈ ('active','disabled')
pending_approvals.status ∈ ('pending','approved','rejected','expired')
"""

# ── 业务口径（同一个名词只允许一种算法）──────────────────────
METRICS = """
GMV（成交额）      = SUM(orders.total) WHERE status <> '已取消'
                     ⚠️ **含「退款中」和「已退款」** —— GMV 的语义是"成交了多少"，退款是事后发生的
                     另一件事。要"扣掉退款的"请用净成交额，别自己去改 GMV 的口径。
净成交额           = SUM(orders.total) WHERE status NOT IN ('已取消','已退款')
                     （= GMV − 已退款订单金额；「退款中」**不扣**：钱还没退出去）
退款金额           = SUM(orders.total) WHERE status = '已退款'
                     口径说明：**refunds 表没有金额列**，金额只能取被退款订单的 orders.total；
                     这是本项目约定的口径，不是错误（Reviewer/校验环节不要因此判错）
退款中金额（敞口） = SUM(orders.total) WHERE status = '退款中'（已进流程、尚未退出去的敞口）
订单数             = COUNT(*) FROM orders（默认排除已取消；口径要写清）
退款率             = status='已退款' 的订单数 / 订单数（分母是订单数，**不是消息数**）
                     注意「退款中」不计入分子（未决工单还没退），要单独看就是上面的敞口指标
退款申请率（投诉口径）= **有退款工单**的订单数 / 订单数，工单状态不筛选（含被驳回的申请）
                     ⚠️ 这两个率**不是一回事**，问法决定用哪个：
                     "退款率/退了多少钱" → orders.status='已退款'；
                     "投诉/申请退款多少" → refunds 工单数（含被驳回）。
                     实测：同一份数据两者 Top3 相差一倍以上（0.10 vs 0.225），
                     混用会给出完全不同的结论 —— 结论里要写明用的是哪个口径。
                     排名类问题要设**样本下限**：订单数 < 10 的产品/渠道不要拿来排前几名
                     （实测：3 单全退 = 100% 会霸榜，看着惊人但没有业务意义）；
                     若确实要展示，必须注明"样本不足"。
平均客单价         = GMV / 订单数
活跃客户           = COUNT(DISTINCT orders.customer_id)（有下单行为的客户）
询价               = conversations 里 role='human' 且 content 含产品号（形如 P0075）
询价转化率         = 该产品被下单的订单数 / 该产品的询价次数
                     ⚠️ 该比值**可能大于 1**：订单不只来自对话询价（还有电话/线下/复购），
                     所以它衡量的是"这个产品的询价有没有转化成订单"的相对倾向，
                     不要当成严格的漏斗转化率；比较时用同一口径横向比
审批时效           = pending_approvals.decided_at − created_at（仅 status∈approved/rejected）
审批通过率         = approved 数 / (approved + rejected)
"""

# ── 时间范围口径（同一问题两次问出不同数字的真实教训）────────
TIME_SCOPE = """
- **用户没提时间范围时，不要自行加时间过滤**，按全量算。
- 实测踩过：同一个问题「退款率最高的颜色是哪些」，一次按全量（黑色 27/276 = 9.8%），
  另一次模型自行加了"近 90 天"（黑色 12/276 = 4.4%）—— 同一问题两个数字，
  会让管理员不再信任这个 Agent。
- 如果确实按时间过滤了（用户要求，或数据量太大），**必须在结论第一句写明时间范围**，
  并保证分子分母同范围。
"""


# ── 坑（不写进 prompt 就会踩）────────────────────────────────
PITFALLS = """
1. **时间列有两套类型**：
   - orders / users / conversations / sessions / refunds / audit_log 的 created_at 等
     是 **TEXT（ISO 字符串）**，做时间运算必须先转：created_at::timestamp；
     取月份用 substr(created_at,1,7) 更稳。
   - pending_approvals 的 created_at/decided_at/expires_at 是 **TIMESTAMPTZ**，可直接比较。
2. **金额是 numeric**：SUM 后建议 round(...,2)；不要用浮点字面量比较相等。
3. **退款要看"率"不要看"数"**：某颜色的退款数最多，往往只是因为它卖得最多
   （实测黑色占 42% 订单，退款数自然最高；但退款率要按 退款单数/订单数 算）。
4. **sessions 表是聊天会话**，与"登录会话"无关；登录会话在 Redis（study1:auth:*）。
5. **演示数据与真实数据共存**：customer_id 以 'demo_' 开头的客户是造出来的演示数据
   （scripts/seed_demo_data.py 生成）。如果问题只关心真实数据，需要过滤；
   做趋势/结构分析时可以全量。
6. **中文枚举值要用单引号**，且注意是 Unicode（不要写成英文状态名）。
7. 表名/列名全小写；不要访问 pg_* / information_schema（schema 已由工具提供）。
8. **orders.created_at 是下单时间**，paid_at/shipped_at/refunded_at 可能为 NULL
   （未付款/未发货/未退款）。
9. **退单联动（2026-09）**：有未决退款工单的订单状态是「退款中」，退款审核通过才变「已退款」。
   三个容易算错的地方：
   - 只筛 `status <> '已取消'` 得到的是 **GMV（含已退款）**，不是净成交额；
   - 「退款中」既不算已退款、也不是正常在途 —— 它是**敞口**，要用单独的指标看；
   - 想知道"退款工单处理得怎么样"要看 `refunds` 表（待审核/已通过/已驳回），
     想知道"钱退了多少"要看 `orders.status='已退款'` 的金额 —— **两件事别混**。
10. **退款前后状态可追溯**：`refunds.order_status_before` 记的是发起退款时订单的状态
   （驳回会原样退回）。要做"退款驳回率""退款影响面"这类分析可以用它。
"""

OUTPUT_GUIDE = """
输出要求：
- 只输出只读 SQL（SELECT / WITH 开头），单条语句，不要分号结尾。
- 不要手写 LIMIT 上限以外的分页；系统会自动包一层 LIMIT 兜底。
- 列名用英文别名便于前端展示，例如：SELECT p.category AS category, SUM(o.total) AS gmv ...
- 如果问题模糊（没说时间范围/是否含取消单），先按本字典的默认口径计算，并在结论里说明假设。
- **默认不加时间过滤**（全量）；若加了必须在结论里写明范围。
"""


async def load_fk_text() -> str:
    """从数据库读外键关系（JOIN 依据自动生成，不用手写）。

    读不到（表还没建/没有外键）返回空串，调用方会退回手写版本 —— 这样"刚 clone、
    还没跑迁移"的环境也不会拿到一个空的关系表。
    """
    from src.db import query_all           # 懒导入：schema_hints 保持低耦合
    try:
        rows = await query_all("""
            SELECT src.relname AS child, sa.attname AS child_col,
                   dst.relname AS parent, da.attname AS parent_col,
                   CASE con.confdeltype WHEN 'c' THEN 'CASCADE' WHEN 'r' THEN 'RESTRICT'
                        WHEN 'n' THEN 'SET NULL' ELSE 'NO ACTION' END AS on_delete
            FROM pg_constraint con
            JOIN pg_class src ON src.oid = con.conrelid
            JOIN pg_class dst ON dst.oid = con.confrelid
            JOIN pg_attribute sa ON sa.attrelid = src.oid AND sa.attnum = ANY(con.conkey)
            JOIN pg_attribute da ON da.attrelid = dst.oid AND da.attnum = ANY(con.confkey)
            WHERE con.contype = 'f' AND src.relnamespace = 'public'::regnamespace
            ORDER BY src.relname, sa.attname""")
    except Exception:  # noqa: BLE001
        return ""
    if not rows:
        return ""
    lines = [f"{r['child']}.{r['child_col']} = {r['parent']}.{r['parent_col']}"
             f"   (ON DELETE {r['on_delete']})" for r in rows]
    return "\n".join(lines)


def semantic_hints(fk_text: str = "") -> str:
    """拼成一段可直接塞进 prompt 的语义层文本。

    ``fk_text``：数据库外键关系（``load_fk_text()`` 的结果）。给了就用它，
    否则退回手写 RELATIONS。
    """
    tables = "\n".join(f"- {name}：{desc}" for name, desc in TABLES.items())
    relations = fk_text.strip() or RELATIONS.strip()
    source = "数据库外键自动读取" if fk_text.strip() else "手写兜底（库内暂无外键）"
    return (
        "## 可查询的表\n" + tables +
        f"\n\n## 表关系（{source}）—— JOIN 依据\n" + relations +
        "\n\n## 外键管不到的语义关系（JOIN 时注意）\n" + RELATIONS.strip() +
        "\n\n## 枚举取值\n" + ENUMS.strip() +
        "\n\n## 业务口径（同一名词只用一种算法）\n" + METRICS.strip() +
        "\n\n## 时间范围（口径一致性，很重要）\n" + TIME_SCOPE.strip() +
        "\n\n## 必须避开的坑\n" + PITFALLS.strip() +
        "\n\n" + OUTPUT_GUIDE.strip()
    )


def demo_customer_prefix() -> str:
    """演示客户前缀（用于在 prompt 里说明如何区分演示与真实数据）。"""
    return os.getenv("DEMO_CUSTOMER_PREFIX", "demo_")
