#!/usr/bin/env python
"""创建只读分析角色（把"四层防护"的第一层变成真的）
==================================================
为什么需要它：分析 Agent 执行的是 **LLM 生成的 SQL**（不可信输入）。只靠语句白名单
不够——真正的兜底是**数据库权限**：用一个只有 SELECT 权限的角色，即便 SQL 被绕过，
数据库也不让写。参考实现用的是应用同一个可写连接，这一层是缺的。

用法
----
    python scripts/create_analytics_role.py                      # 创建/更新角色并打印 DSN
    python scripts/create_analytics_role.py --password 自定义密码
    python scripts/create_analytics_role.py --role my_ro --print-env

执行内容（幂等）：
    CREATE ROLE analytics_ro LOGIN PASSWORD '...'              （已存在则改密码）
    GRANT CONNECT ON DATABASE <db> TO analytics_ro
    GRANT USAGE ON SCHEMA public TO analytics_ro
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO analytics_ro
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO analytics_ro
    REVOKE CREATE ON SCHEMA public FROM analytics_ro           （连建表都不给）

⚠️ 需要超级用户权限执行；生产建议由 DBA 跑等价 SQL，而不是让应用自建角色。
"""
import argparse
import asyncio
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://rain@localhost:5432/study1")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from src.db import execute, query_one  # noqa: E402

DEFAULT_ROLE = "analytics_ro"


def build_dsn(base_dsn: str, role: str, password: str) -> str:
    """把业务 DSN 改写成只读角色 DSN（保持 host/port/db 不变）。"""
    u = urlparse(base_dsn)
    netloc = f"{role}:{password}@{u.hostname or 'localhost'}"
    if u.port:
        netloc += f":{u.port}"
    return urlunparse((u.scheme, netloc, u.path, "", "", ""))


async def main() -> None:
    ap = argparse.ArgumentParser(description="创建只读分析角色")
    ap.add_argument("--role", default=DEFAULT_ROLE, help=f"角色名（默认 {DEFAULT_ROLE}）")
    ap.add_argument("--password", default="", help="密码（默认随机生成）")
    ap.add_argument("--print-env", action="store_true", help="只打印 .env 配置行")
    args = ap.parse_args()

    role = args.role
    password = args.password or secrets.token_urlsafe(18)
    base = os.getenv("DATABASE_URL", "")
    dbname = urlparse(base).path.lstrip("/") or "study1"
    dsn = build_dsn(base, role, password)

    if args.print_env:
        print(f"ANALYTICS_DATABASE_URL={dsn}")
        return

    exists = await query_one("SELECT 1 AS ok FROM pg_roles WHERE rolname = :r", {"r": role})
    if exists:
        await execute(f'ALTER ROLE "{role}" WITH LOGIN PASSWORD \'{password}\'')
        print(f"♻️  角色 {role} 已存在 → 已更新密码")
    else:
        await execute(f'CREATE ROLE "{role}" WITH LOGIN PASSWORD \'{password}\'')
        print(f"✅ 已创建只读角色 {role}")

    # 权限：只给"连库 + 读 public 下所有表"，连建表都不给
    await execute(f'GRANT CONNECT ON DATABASE "{dbname}" TO "{role}"')
    await execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
    await execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{role}"')
    await execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO "{role}"')
    await execute(f'REVOKE CREATE ON SCHEMA public FROM "{role}"')
    print("🔒 权限：CONNECT + USAGE + SELECT（无 INSERT/UPDATE/DELETE/DDL）")

    # 自查：确认这个角色真的写不动
    print("\n🔎 用该角色实测（写入必须失败）：")
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text as _text
    eng = create_async_engine(dsn)
    try:
        async with eng.connect() as conn:
            n = (await conn.execute(_text("SELECT count(*) FROM orders"))).scalar()
            print(f"   SELECT count(*) FROM orders → {n} ✅ 可读")
        try:
            async with eng.begin() as conn:
                await conn.execute(_text("UPDATE orders SET total = 0 WHERE false"))
            print("   UPDATE → ❌ 竟然成功了！请检查权限")
        except Exception as e:  # noqa: BLE001
            print(f"   UPDATE → ✅ 被拒（{str(e).splitlines()[0][:70]}）")
    finally:
        await eng.dispose()

    print("\n把下面这行加到 .env（生产务必配，否则只剩②③④三层防护）：")
    print(f"ANALYTICS_DATABASE_URL={dsn}")


if __name__ == "__main__":
    asyncio.run(main())
