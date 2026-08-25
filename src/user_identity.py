"""
用户身份（user_id）校验 — 单一事实来源
======================================
约束：1-64 位 [A-Za-z0-9_-]，禁止路径分隔符/空白/中文等，
防止目录穿越（data/users/../../）与注入式 key。

被两处复用：
- Web 层（app.py）：请求边界解析，非法 → 400 拒绝
- 持久层（memory.py）：sanitize_user_id 兜底，非法 → 降级 default

零外部依赖，可被单测直接覆盖。
"""

import re

USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DEFAULT_USER_ID = "guest"


def is_valid_user_id(user_id) -> bool:
    """是否合法：非空、1-64 位、仅字母/数字/下划线/连字符。"""
    return isinstance(user_id, str) and bool(USER_ID_PATTERN.fullmatch(user_id))


def resolve_user_id(raw) -> str:
    """
    Web 请求边界解析：
    - 缺省/空 → DEFAULT_USER_ID（开发期便利）
    - 非法    → 抛 ValueError（API 层捕获后转 400）
    - 合法    → 原样返回
    """
    uid = (raw or "").strip()
    if not uid:
        return DEFAULT_USER_ID
    if not is_valid_user_id(uid):
        raise ValueError(f"非法 user_id: {uid!r}")
    return uid