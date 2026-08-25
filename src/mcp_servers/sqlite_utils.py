"""
SQLite 并发安全工具（共享层）
============================
MCP Server 与 src/memory 统一走这里连接 SQLite：
- 每次操作独立连接、finally 保证关闭（消灭异常路径的连接泄漏）
- 统一 PRAGMA：WAL 日志模式 + busy_timeout=5000（并发写入不报 locked）
- WAL 用进程级 set 只开启一次，避免每次连接都执行 PRAGMA

注意：本模块零项目依赖，MCP Server 以「纯脚本」方式运行时可直接
`import sqlite_utils`（sys.path[0] = src/mcp_servers/）。
"""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, List, Optional

_wal_ensured: set = set()
_wal_lock = threading.Lock()


def connect(db_path, row_factory: bool = True) -> sqlite3.Connection:
    """建立连接：busy_timeout=5000 + 首次连接时启用 WAL。"""
    db_path_str = str(db_path)
    conn = sqlite3.connect(db_path_str, timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    if db_path_str not in _wal_ensured:
        with _wal_lock:
            if db_path_str not in _wal_ensured:
                # WAL 是持久化的，但保险起见执行一次（幂等，若已 WAL 则无副作用）
                row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
                if row and row[0] == "wal":
                    _wal_ensured.add(db_path_str)
    if row_factory:
        conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def conn_ctx(db_path, row_factory: bool = True):
    """连接上下文：无论如何都 close，杜绝泄漏。"""
    conn = connect(db_path, row_factory=row_factory)
    try:
        yield conn
    finally:
        conn.close()


def query_all(db_path, sql: str, params: Iterable = ()) -> List[sqlite3.Row]:
    """查询多行。"""
    with conn_ctx(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def query_one(db_path, sql: str, params: Iterable = ()) -> Optional[sqlite3.Row]:
    """查询单行（无结果返回 None）。"""
    with conn_ctx(db_path) as conn:
        return conn.execute(sql, params).fetchone()


def execute(db_path, sql: str, params: Iterable = ()) -> None:
    """单条写操作：执行 + commit。"""
    with conn_ctx(db_path) as conn:
        conn.execute(sql, params)
        conn.commit()


def executescript(db_path, sql: str) -> None:
    """多条 DDL/DML：整段执行 + commit。"""
    with conn_ctx(db_path) as conn:
        conn.executescript(sql)
        conn.commit()