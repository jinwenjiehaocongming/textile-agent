"""
创建/重置管理员账号（幂等）
============================
用法:
    python scripts/create_admin.py

读取环境变量（建议放 .env）:
    ADMIN_USERNAME  登录名，默认 admin
    ADMIN_PASSWORD  密码（必填；建议 >= 8 位强密码）
    ADMIN_NAME      显示名，默认「管理员」

行为:
- 首次运行：建 users 表（ensure_schema 幂等）并创建 role=admin 账号
- 已存在同用户名：重置密码并确保 role=admin（当重置工具用）
- 注册接口只允许 customer，admin 只能从这里创建（防越权）
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()  # .env 里读 ADMIN_*（可选，环境变量优先）

from src.db import ensure_schema, execute, query_one  # noqa: E402
from src.users import (  # noqa: E402
    hash_password, normalize_username, validate_display_name,
    validate_password, validate_username,
)


async def main() -> int:
    await ensure_schema()

    username = validate_username(os.getenv("ADMIN_USERNAME", "admin"))
    password = os.getenv("ADMIN_PASSWORD", "")
    display_name = validate_display_name(os.getenv("ADMIN_NAME", "管理员"))

    if not password:
        print("❌ 未设置 ADMIN_PASSWORD（可写入 .env 或环境变量）")
        return 1
    validate_password(password)

    existing = await query_one(
        "SELECT id, role FROM users WHERE username = :u", {"u": normalize_username(username)}
    )
    ts = datetime.now().isoformat()
    pw_hash = hash_password(password)

    if existing:
        await execute(
            "UPDATE users SET password_hash = :pw, role = 'admin', display_name = :n WHERE username = :u",
            {"pw": pw_hash, "n": display_name, "u": normalize_username(username)},
        )
        print(f"✅ 管理员已存在，已重置密码并确保 admin 角色: {username}")
    else:
        uid = uuid.uuid4().hex
        await execute(
            "INSERT INTO users (id, username, password_hash, display_name, role, status, created_at) "
            "VALUES (:id, :u, :pw, :n, 'admin', 'active', :ts)",
            {"id": uid, "u": normalize_username(username), "pw": pw_hash,
             "n": display_name, "ts": ts},
        )
        print(f"✅ 管理员创建成功: {username}（显示名：{display_name}）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
