"""
会话模块 — 多对话隔离（2026-09）
===============================
每个登录用户可有多个对话（session）。对话历史按 (user_id, session_id) 隔离：
- 会话列表/新建/改名/删除 由本模块负责（sessions 表）
- 消息存档按 session 落 conversations.session_id（见 src/memory.py）

设计说明：
- 会话归属严格按 user_id 校验（行级隔离），杜绝跨用户读会话
- 标题自动生成：新会话首条用户消息前 18 字（后端做，前端免维护）
- 'default' 是历史遗留会话（老数据迁移目标），不属于 sessions 表，
  不需要出现在会话列表里
"""

import uuid
from datetime import datetime

from src.db import execute, query_all, query_one

DEFAULT_TITLE = "新对话"
MAX_TITLE_LEN = 18


def _now() -> str:
    return datetime.now().isoformat()


async def get_owned_session(user_id: str, session_id: str) -> dict | None:
    """校验会话归属并返回；不存在/不属于该用户 → None。"""
    return await query_one(
        "SELECT id, user_id, title, created_at, updated_at FROM sessions "
        "WHERE id = :sid AND user_id = :uid",
        {"sid": session_id, "uid": user_id},
    )


async def create_session(user_id: str, title: str = DEFAULT_TITLE) -> dict:
    sid = uuid.uuid4().hex
    ts = _now()
    await execute(
        "INSERT INTO sessions (id, user_id, title, created_at, updated_at) "
        "VALUES (:id, :uid, :title, :ts, :ts)",
        {"id": sid, "uid": user_id, "title": (title or DEFAULT_TITLE)[:MAX_TITLE_LEN], "ts": ts},
    )
    return {"session_id": sid, "title": title or DEFAULT_TITLE, "created_at": ts, "updated_at": ts}


async def list_sessions(user_id: str, limit: int = 50) -> list:
    """该用户全部会话，按最近活跃倒序（不含 'default' 遗留会话）。"""
    rows = await query_all(
        "SELECT id, title, created_at, updated_at FROM sessions "
        "WHERE user_id = :uid ORDER BY updated_at DESC, created_at DESC LIMIT :n",
        {"uid": user_id, "n": limit},
    )
    return [
        {"session_id": r["id"], "title": r["title"],
         "created_at": r["created_at"], "updated_at": r["updated_at"]}
        for r in rows
    ]


async def rename_session(user_id: str, session_id: str, title: str) -> bool:
    """改名；会话不存在或不属于该用户 → False。"""
    t = (title or "").strip()
    if not t or not await get_owned_session(user_id, session_id):
        return False
    await execute(
        "UPDATE sessions SET title = :t, updated_at = :ts "
        "WHERE id = :sid AND user_id = :uid",
        {"t": t[:MAX_TITLE_LEN], "ts": _now(), "sid": session_id, "uid": user_id},
    )
    return True


async def delete_session(user_id: str, session_id: str) -> bool:
    """删除会话（连同其中消息，软业务场景可接受）。不存在 → False。"""
    owned = await get_owned_session(user_id, session_id)
    if not owned:
        return False
    await execute("DELETE FROM conversations WHERE user_id = :uid AND session_id = :sid",
                  {"uid": user_id, "sid": session_id})
    await execute("DELETE FROM sessions WHERE id = :sid AND user_id = :uid",
                  {"sid": session_id, "uid": user_id})
    return True


async def touch_session(user_id: str, session_id: str, preview: str = "") -> None:
    """消息落库后调用：更新时间戳；若是「新对话」且首条来了 → 自动取标题。"""
    if session_id in ("", "default"):
        return
    ts = _now()
    if preview:
        await execute(
            "UPDATE sessions SET title = CASE WHEN title = :dft THEN :pv ELSE title END, "
            "updated_at = :ts WHERE id = :sid AND user_id = :uid",
            {"dft": DEFAULT_TITLE, "pv": (preview.strip() or DEFAULT_TITLE)[:MAX_TITLE_LEN],
             "ts": ts, "sid": session_id, "uid": user_id},
        )
    else:
        await execute(
            "UPDATE sessions SET updated_at = :ts WHERE id = :sid AND user_id = :uid",
            {"ts": ts, "sid": session_id, "uid": user_id},
        )
