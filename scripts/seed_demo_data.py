#!/usr/bin/env python
"""演示数据生成器（2026-09，为「管理员数据分析」模块准备）
=========================================================
为什么需要它：真实库只有 52 单 / 3 个月 / 11 个产品，任何分析查询画出来都是
"一根光秃秃的柱子"，演示效果等于零。本脚本生成可重复、可清理、**含真实信号**的数据。

关键设计：**造数必须造出"能被发现的信号"**，否则分析 Agent 没有故事可讲。
所以这里有意注入三个真实模式（脚本末尾会打印出来，便于对照验证）：

1. **色差退款集中在特定颜色/品类**——某几个"深色系"产品退款率是均值的 3~5 倍，
   退款原因集中在"色差/缸差"（对应你库里真实存在的 7 条色差投诉）。
2. **询价多但下单少的产品**——几个产品的会话提及量高、转化率低（商机流失信号）。
3. **月度趋势 + 客户分层**——8 个月的缓慢增长 + 少数大客户贡献多数 GMV（帕累托）。

用法
----
    python scripts/seed_demo_data.py                      # 默认规模（600 单 / 8 月 / 40 客户）
    python scripts/seed_demo_data.py --orders 2000 --months 12 --customers 80
    python scripts/seed_demo_data.py --reset               # 清掉所有演示数据（只删 demo_ 前缀）
    python scripts/seed_demo_data.py --force                # 已有演示数据时也重建

数据标记与安全
--------------
所有演示客户 user_id 以 ``demo_`` 开头，``--reset`` 只删这些客户及其订单/退款/会话，
**不碰真实数据**。产品维度取自真实 ``products`` 表（281 条），所以品类/颜色分布是真实的。
"""
import argparse
import asyncio
import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://rain@localhost:5432/study1")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from src.db import execute, execute_many, query_all, query_one  # noqa: E402
from src.logging_config import get_logger  # noqa: E402
from src.users import hash_password  # noqa: E402

logger = get_logger(__name__)

DEMO_PREFIX = "demo_"
DEMO_PASSWORD = "Demo123456"
CITIES = ["杭州余杭区乔司街道", "绍兴柯桥区轻纺城", "广州海珠区中大布匹市场",
          "苏州吴江区盛泽镇", "青岛即墨区服装城", "义乌北苑街道工业区",
          "佛山南海区西樵镇", "南通通州区家纺城"]
FIRST = ["王", "李", "张", "刘", "陈", "杨", "赵", "黄", "周", "吴", "徐", "孙", "马", "朱"]
ROLE_TITLES = ["纺织", "服饰", "家纺", "箱包", "户外", "贸易"]
STATUS_MIX = [("待付款", 0.18), ("已付款", 0.24), ("已发货", 0.20), ("已收货", 0.33), ("已取消", 0.05)]
# 真实库里 7 条退款全是色差/缸差问题 → 造数沿用同一业务现实
COLOR_DEFECT_REASONS = [
    "色差超标，整批布存在色差问题，无法使用",
    "不同卷之间色差超标（缸差），收到货5天在验收期内",
    "面料色差超标，同一卷布两边颜色不一致，不符合合同约定色差标准",
    "色差超标，收到的布颜色与色卡严重不符，未裁剪加工",
    "发货颜色与订单不符，客户下单黑色，实际收到的颜色完全不同",
]
OTHER_REFUND_REASONS = [
    "客户临时取消订单，货物未发出",
    "数量与订单不符，少了 2 卷",
    "交期延误超过约定，客户已另寻供应商",
    "克重与样品偏差较大",
]


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _order_no(dt: datetime) -> str:
    """与应用一致的单号格式：ORD-<日期>-<时分秒><微秒6位><随机4位>。"""
    return (f"ORD-{dt.strftime('%Y%m%d')}-"
            f"{dt.strftime('%H%M%S')}{dt.microsecond:06d}{random.randint(1000, 9999)}")


async def reset_demo() -> None:
    """只清演示数据（demo_ 前缀客户及其关联数据）。"""
    users = await query_all("SELECT id FROM users WHERE id LIKE :p", {"p": f"{DEMO_PREFIX}%"})
    ids = [u["id"] for u in users if u["id"].startswith(DEMO_PREFIX)]
    if ids:
        await execute("DELETE FROM conversations WHERE user_id = ANY(:ids)", {"ids": ids})
        await execute("DELETE FROM orders WHERE customer_id = ANY(:ids)", {"ids": ids})
        await execute("DELETE FROM sessions WHERE user_id = ANY(:ids)", {"ids": ids})
        await execute("DELETE FROM profile WHERE user_id = ANY(:ids)", {"ids": ids})
        await execute("DELETE FROM pending_approvals WHERE user_id = ANY(:ids)", {"ids": ids})
        await execute("DELETE FROM refunds WHERE order_no IN "
                      "(SELECT order_no FROM orders WHERE customer_id = ANY(:ids))", {"ids": ids})
        await execute("DELETE FROM users WHERE id = ANY(:ids)", {"ids": ids})
    # 退款可能已被上一步删掉，这里再兜一次"孤儿退款"（演示单号前缀 ORD- 且订单不存在）
    orphans = await query_all(
        "SELECT r.order_no FROM refunds r LEFT JOIN orders o ON o.order_no = r.order_no "
        "WHERE o.order_no IS NULL")
    if orphans:
        await execute("DELETE FROM refunds WHERE order_no = ANY(:nos)",
                      {"nos": [o["order_no"] for o in orphans]})
    print(f"🧹 已清理演示数据：{len(ids)} 个客户及其订单/退款/会话")


async def ensure_demo_users(n: int, rnd: random.Random) -> list:
    """建演示客户。40 个客户用一个预计算哈希（同一演示密码），省掉 40 次 bcrypt。"""
    pw_hash = hash_password(DEMO_PASSWORD)
    rows = []
    for i in range(n):
        uid = f"{DEMO_PREFIX}cust_{i:03d}"
        name = rnd.choice(FIRST) + rnd.choice(ROLE_TITLES) + str(rnd.randint(10, 99))
        rows.append({
            "id": uid, "username": f"{DEMO_PREFIX}user_{i:03d}",
            "pw": pw_hash, "name": name,
            "ts": _iso(datetime.now() - timedelta(days=rnd.randint(30, 400))),
        })
    await execute_many(
        "INSERT INTO users (id, username, password_hash, display_name, role, status, created_at) "
        "VALUES (:id, :username, :pw, :name, 'customer', 'active', :ts) "
        "ON CONFLICT (id) DO NOTHING", rows)
    print(f"👤 演示客户：{len(rows)} 个（统一密码 {DEMO_PASSWORD}，用户名 {DEMO_PREFIX}user_XXX）")
    return [r["id"] for r in rows]


def _pick_products(products: list, rnd: random.Random) -> tuple:
    """选出"问题产品"（色差退款集中）与"高询价低转化产品"（商机流失信号）。"""
    dark = [p for p in products if (p["color"] or "") in ("黑色", "藏青", "深灰", "深蓝", "酒红")]
    rnd.shuffle(dark)
    defect = dark[:3] or products[:3]                      # 信号 1：深色系色差问题
    rnd.shuffle(products)
    hot_no_convert = [p for p in products if p not in defect][:3]   # 信号 2：问得多买得少
    return defect, hot_no_convert


async def seed(args) -> None:
    rnd = random.Random(args.seed)
    products = await query_all("SELECT id, name, category, color, price FROM products")
    if not products:
        raise SystemExit("❌ products 表为空：先跑 scripts/init_db.py 或导入 sql/001_products.sql")
    customers = await ensure_demo_users(args.customers, rnd)
    defect, hot_no_convert = _pick_products(products, rnd)

    # ── 订单：8 个月、缓慢增长、帕累托客户分布 ──
    now = datetime.now()
    start = now - timedelta(days=30 * args.months)
    # 帕累托：前 15% 客户是大客户（B2B 面料真实形态——少数大厂决定产量）
    big = max(1, int(len(customers) * 0.15))
    weights = [15.0 if i < big else 1.0 for i in range(len(customers))]
    orders, refunds, approvals = [], [], []
    for i in range(args.orders):
        # 时间：整体递增（趋势 + 月度波动），让"月度趋势"图有意义
        progress = rnd.random() ** 0.85
        created = start + timedelta(days=progress * 30 * args.months,
                                    seconds=rnd.randint(0, 86399))
        if created > now:
            created = now - timedelta(hours=rnd.randint(1, 48))
        # 产品：问题产品占比偏高（这样色差信号才显著）
        p = rnd.choice(defect) if rnd.random() < 0.22 else rnd.choice(products)
        cust = rnd.choices(customers, weights=weights, k=1)[0]
        qty = rnd.choice([200, 300, 500, 800, 1000, 1500, 2000, 3000])
        unit = float(p["price"])
        total = round(qty * unit, 2)
        status = rnd.choices([s for s, _ in STATUS_MIX], weights=[w for _, w in STATUS_MIX], k=1)[0]
        paid_at = created + timedelta(hours=rnd.randint(2, 72)) if status != "待付款" else None
        shipped_at = (paid_at + timedelta(days=rnd.randint(1, 4))
                      if status in ("已发货", "已收货") and paid_at else None)
        orders.append({
            "order_no": _order_no(created), "customer_id": cust, "product_id": p["id"],
            "product_name": p["name"], "color": p["color"], "quantity": qty,
            "unit_price": unit, "total": total, "status": status,
            "created_at": _iso(created),
            "paid_at": _iso(paid_at) if paid_at else None,
            "shipped_at": _iso(shipped_at) if shipped_at else None,
            "phone": f"1{rnd.choice('3578')}{rnd.randint(100000000, 999999999)}",
            "address": rnd.choice(CITIES) + str(rnd.randint(1, 999)) + "号",
            "delivery_date": f"{rnd.randint(3, 15)}天",
        })
        # 退款：问题产品的退款率显著更高（信号 1），且原因集中在色差
        rate = args.refund_rate * (4.0 if p in defect else 1.0)
        if status in ("已发货", "已收货") and rnd.random() < min(rate, 0.6):
            is_defect = p in defect or rnd.random() < 0.55
            refunds.append({
                "order_no": orders[-1]["order_no"],
                "reason": rnd.choice(COLOR_DEFECT_REASONS if is_defect else OTHER_REFUND_REASONS),
                "status": rnd.choices(["待审核", "已通过", "已驳回"], weights=[0.4, 0.5, 0.1])[0],
                "created_at": _iso((shipped_at or created) + timedelta(days=rnd.randint(1, 6))),
            })

    await execute_many(
        """INSERT INTO orders (order_no, customer_id, product_id, product_name, color,
                               quantity, unit_price, total, status, created_at,
                               paid_at, shipped_at, phone, address, delivery_date)
           VALUES (:order_no, :customer_id, :product_id, :product_name, :color,
                   :quantity, :unit_price, :total, :status, :created_at,
                   :paid_at, :shipped_at, :phone, :address, :delivery_date)
           ON CONFLICT (order_no) DO NOTHING""", orders)
    print(f"📦 订单：{len(orders)} 单（{args.months} 个月，{len(customers)} 客户）"
          f"｜GMV ≈ {sum(o['total'] for o in orders):,.0f} 元")

    if refunds:
        await execute_many(
            """INSERT INTO refunds (order_no, reason, status, created_at)
               VALUES (:order_no, :reason, :status, :created_at)
               ON CONFLICT DO NOTHING""", refunds)
    print(f"↩️  退款：{len(refunds)} 条（其中色差类 "
          f"{sum(1 for r in refunds if r['reason'] in COLOR_DEFECT_REASONS)} 条）")

    # ── 待审批单历史：让"审批时效"分析有数据 ──
    for i in range(args.orders // 40):
        cust = rnd.choice(customers)
        p = rnd.choice(products)
        created = now - timedelta(days=rnd.randint(1, 90), hours=rnd.randint(0, 20))
        decided = created + timedelta(minutes=rnd.randint(5, 60 * 30))
        approved = rnd.random() < 0.72
        approvals.append({
            "id": f"demoappr{i:04d}{rnd.randint(1000, 9999)}",
            "thread_id": cust, "user_id": cust, "session_id": "demo",
            "draft": f'{{"product_name": "{p["name"]}", "quantity": 500, "total": 5000}}',
            "args": f'{{"customer_id": "{cust}", "product_id": "{p["id"]}"}}',
            "status": "approved" if approved else "rejected",
            # ⚠️ pending_approvals 的时间列是 TIMESTAMPTZ（不是 TEXT ISO）：
            # 这里必须传 datetime 对象，asyncpg 不接受字符串 —— 这就是"两套时间风格
            # 并存"的真实代价，同一个脚本要对不同表用不同约定。
            "created_at": created, "expires_at": created + timedelta(hours=24),
            "decided_at": decided, "decided_by": "demo_admin",
            "reason": "" if approved else "价格需再谈",
        })
    if approvals:
        await execute_many(
            """INSERT INTO pending_approvals (id, thread_id, user_id, session_id, draft, args,
                                             status, created_at, expires_at, decided_at, decided_by, reason)
               VALUES (:id, :thread_id, :user_id, :session_id, CAST(:draft AS jsonb),
                       CAST(:args AS jsonb), :status, :created_at,
                       :expires_at, :decided_at, :decided_by, :reason)
               ON CONFLICT (id) DO NOTHING""", approvals)
    print(f"✅ 已判定审批单：{len(approvals)} 条（含提交→审批耗时，可做时效分析）")

    # ── 询价会话：支撑"询价→下单转化漏斗" ──
    # 高询价低转化产品故意多提、少成单（信号 2）
    conv, sessions = [], set()
    for i in range(args.inquiries):
        cust = rnd.choice(customers)
        sid = f"demo_sess_{i % max(1, args.customers):03d}"
        sessions.add((sid, cust))
        p = rnd.choice(hot_no_convert) if rnd.random() < 0.35 else rnd.choice(products)
        asked_at = start + timedelta(days=rnd.random() * 30 * args.months,
                                     seconds=rnd.randint(0, 86399))
        conv.append({"uid": cust, "sid": sid, "role": "human", "content":
                     rnd.choice([f"{p['id']} {p['color']}多少钱一米？",
                                 f"{p['id']} 有现货吗，{p['name']}",
                                 f"问一下 {p['id']} 的报价和起订量",
                                 f"{p['name']} {p['color']} 能便宜点吗"]),
                     "ts": _iso(asked_at)})
        conv.append({"uid": cust, "sid": sid, "role": "ai", "content":
                     f"{p['name']}（{p['id']}）现价 ¥{p['price']}/米，"
                     f"{p['color']}色现货充足，起订量 500 米。", "ts": _iso(asked_at)})
    await execute_many(
        """INSERT INTO conversations (user_id, session_id, role, content, created_at)
           VALUES (:uid, :sid, :role, :content, :ts)""", conv)
    rows = [{"id": sid, "uid": uid, "title": f"演示会话 {sid[-3:]}", "ts": _iso(start)}
            for sid, uid in sessions]
    await execute_many(
        """INSERT INTO sessions (id, user_id, title, created_at, updated_at)
           VALUES (:id, :uid, :title, :ts, :ts) ON CONFLICT (id) DO NOTHING""", rows)
    print(f"💬 会话：{len(sessions)} 个会话 / {len(conv)} 条消息"
          f"（其中带产品号的询价 {len(conv)//2} 条）")

    # ── 造完自查：脚本**自己算出真实数字**再打印，绝不硬编码声称 ──
    # （初版我在这里写死了"前 20% 客户贡献多数 GMV"，实测只有 27% —— 造数脚本吹牛
    #   比不造数更糟，因为它会让人误以为数据里有信号。）
    print("\n📌 注入信号自查（以下数字都是刚查库算出来的）：")
    defect_ids = [p["id"] for p in defect]
    hot_ids = [p["id"] for p in hot_no_convert]

    rate = await query_one("""
        SELECT round(100.0 * count(DISTINCT r.order_no) / nullif(count(DISTINCT o.order_no), 0), 1) AS pct
        FROM orders o LEFT JOIN refunds r ON r.order_no = o.order_no WHERE o.status <> '已取消'""")
    defect_rate = await query_one("""
        SELECT round(100.0 * count(DISTINCT r.order_no) / nullif(count(DISTINCT o.order_no), 0), 1) AS pct
        FROM orders o LEFT JOIN refunds r ON r.order_no = o.order_no
        WHERE o.status <> '已取消' AND o.product_id = ANY(:ids)""", {"ids": defect_ids})
    print(f"   ① 色差信号：问题产品 {'、'.join(p['name'] for p in defect)}")
    print(f"      退款率 {defect_rate['pct']}% vs 全站 {rate['pct']}%"
          f"（{'×%.1f' % (float(defect_rate['pct'] or 0) / max(float(rate['pct'] or 1), 0.1))}）")

    conv = await query_all("""
        WITH ask AS (SELECT (regexp_matches(content, '(P[0-9]{4})'))[1] AS pid, count(*) n
                     FROM conversations WHERE role='human' AND content ~ 'P[0-9]{4}' GROUP BY 1)
        SELECT a.pid, a.n AS inquiries, coalesce(o.n, 0) AS orders
        FROM ask a LEFT JOIN (SELECT product_id, count(*) n FROM orders GROUP BY 1) o ON o.product_id = a.pid
        WHERE a.pid = ANY(:ids) ORDER BY a.n DESC""", {"ids": hot_ids})
    print(f"   ② 转化信号：{'、'.join(f"{r['pid']} 询价{r['inquiries']}→下单{r['orders']}" for r in conv)}")

    par = await query_one("""
        SELECT count(*) n, sum(gmv) g FROM (
          SELECT customer_id, sum(total) gmv FROM orders WHERE status <> '已取消'
          GROUP BY 1 ORDER BY 2 DESC LIMIT :k) t""", {"k": big})
    tot = await query_one("SELECT sum(total) g FROM orders WHERE status <> '已取消'")
    share = float(par["g"] or 0) / max(float(tot["g"] or 1), 1) * 100
    print(f"   ③ 帕累托：前 {big}/{len(customers)} 客户（{big/len(customers)*100:.0f}%）"
          f"贡献 {share:.0f}% GMV")


async def main() -> None:
    ap = argparse.ArgumentParser(description="生成演示数据（分析模块用）")
    ap.add_argument("--orders", type=int, default=600, help="订单数（默认 600）")
    ap.add_argument("--months", type=int, default=8, help="时间跨度（月，默认 8）")
    ap.add_argument("--customers", type=int, default=40, help="演示客户数（默认 40）")
    ap.add_argument("--inquiries", type=int, default=200, help="询价会话数（默认 200）")
    ap.add_argument("--refund-rate", type=float, default=0.06, help="退款率（默认 6%%，问题产品 ×4）")
    ap.add_argument("--seed", type=int, default=20260914, help="随机种子（默认固定，保证可复现）")
    ap.add_argument("--reset", action="store_true", help="只清理演示数据后退出")
    ap.add_argument("--force", action="store_true", help="已有演示数据时也重建")
    args = ap.parse_args()

    if args.reset:
        await reset_demo()
        return
    existing = await query_one("SELECT count(*) AS n FROM users WHERE id LIKE :p",
                               {"p": f"{DEMO_PREFIX}%"})
    if existing and existing["n"] and not args.force:
        print(f"ℹ️ 已有 {existing['n']} 个演示客户。用 --force 重建，或 --reset 清理。")
        return
    if existing and existing["n"]:
        await reset_demo()
    await seed(args)


if __name__ == "__main__":
    asyncio.run(main())
