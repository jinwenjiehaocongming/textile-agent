#!/usr/bin/env bash
# ============================================================
# 容器启动入口（docker compose）
# 顺序：等依赖就绪 → 建表/管理员(幂等) → 灌知识索引(幂等重建)
#       → 启动 uvicorn
# 解决部署最大坑：容器 healthz 全绿但业务表空、知识库空的问题。
# ============================================================
set -euo pipefail

echo "[init] 等待 PostgreSQL 就绪 ..."
python - <<'PY'
import asyncio, os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

url = os.environ.get("DATABASE_URL",
                     "postgresql+asyncpg://postgres:postgres@postgres:5432/study1")

async def wait():
    engine = create_async_engine(url)
    for i in range(60):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            print("[init] PostgreSQL 就绪 ✓")
            return
        except Exception as e:  # noqa: BLE001
            if i % 5 == 0:
                print(f"[init] 等待 PG ... ({str(e)[:80]})")
            await asyncio.sleep(2)
    raise SystemExit("[init] PostgreSQL 60 秒内未就绪，退出")

asyncio.run(wait())
PY

echo "[init] 等待 Qdrant 就绪 ..."
python - <<'PY'
import asyncio, urllib.request

async def wait():
    for i in range(60):
        try:
            with urllib.request.urlopen("http://qdrant:6333/healthz", timeout=2) as r:
                if r.status == 200:
                    print("[init] Qdrant 就绪 ✓")
                    return
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(2)
    raise SystemExit("[init] Qdrant 60 秒内未就绪，退出")

asyncio.run(wait())
PY

echo "[init] 建表 + 确保管理员账号（幂等）"
python scripts/create_admin.py

echo "[init] 灌知识索引（幂等：重建集合）"
python scripts/build_index.py

echo "[init] 依赖初始化完成，启动应用"
exec python -m uvicorn app:app --host 0.0.0.0 --port 8005
