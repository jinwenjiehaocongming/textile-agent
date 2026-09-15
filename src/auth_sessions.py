"""refresh token 会话存储（Redis）— 路线②：短期 access + 可轮换 refresh
=====================================================================
分工（关键）
============
- **access token**：JWT，默认 15 分钟，无状态，验签不查任何存储（``src/auth.py``）
- **refresh token**：**不透明随机串**（``{sid}.{secret}``），只在 Redis 存 SHA-256 哈希

为什么 refresh 要有状态：JWT 的代价是"签发即不可撤销"。把撤销能力放在 refresh 上，
access 保持无状态，撤销窗口就被压缩成一个 access TTL（15 分钟）——这就是
"short-lived access + long-lived refresh" 的全部意义。

安全机制（对照 RFC 9700 / Auth0 / Okta）
======================================
- **轮换**：每次刷新都作废旧的、发新的。RFC 9700 §2.2.2 对 public client 是 **MUST**
  （或改用 sender-constrained token）。
- **重用检测**：已被轮换掉的 refresh 再次出现 → 判定为泄露 → **作废整个会话**
  （Auth0 的 Automatic Reuse Detection 同理）。
- **竞态 leeway**（默认 30s）：刚被轮换掉的旧 token 在窗口内重现 → 当作并发刷新
  （前端多请求同时 401 的经典场景），同会话再发一对新 token，不触发"核弹"。
  Okta 的 ``rotation leeway``（0–60s）就是同一招。代价：泄露的旧 token 在 leeway
  窗口内仍可用，所以**前端单飞（single-flight）才是第一道防线**，leeway 只是安全网。
  注意窗口是**左闭右开**（``now - prev_at < leeway``）：否则 leeway=0 时"同一秒内的
  重放"会永远落进宽限窗，重用检测形同虚设。
- **双封顶**：空闲期（每次使用续期，默认 14 天）+ 绝对上限（自登录起，默认 30 天）。
  少了绝对上限，活跃用户会被无限续命。

Redis 结构（前缀默认 study1，与缓存 ``study1:chat:*`` 分家）
=========================================================
``study1:auth:sess:{sid}``  → HASH {user_id, role, hash, prev_hash, prev_at, born_at, last_at, ua, ip}
``study1:auth:user:{uid}``  → SET{sid...}（列会话 / 全端下线）

**fail-closed**：Redis 不可用 → 抛 ``AuthBackendUnavailable``（HTTP 503）。
鉴权不能 fail-open：放行等于"任何 refresh 都能换到 access"。副作用要知道：Redis 挂掉时
**已签发的 access token 仍然有效**（无状态，最多再活一个 access TTL），但登录/刷新 503。
"""

import hashlib
import os
import re
import secrets
import time
import uuid

from src import redis_client
from src.logging_config import get_logger

logger = get_logger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        logger.warning("环境变量 %s 不是整数，回退默认值 %s", name, default)
        return default


AUTH_PREFIX = (os.getenv("REDIS_KEY_PREFIX", "study1") or "study1").strip(":")
# 下面三个都在函数里按"模块属性"读取（不拷进局部变量），测试可以 monkeypatch 改小
IDLE_SECONDS = max(60, _env_int("REFRESH_IDLE_DAYS", 14) * 86400)           # 空闲期：用一次续一次
ABSOLUTE_SECONDS = max(60, _env_int("REFRESH_ABSOLUTE_DAYS", 30) * 86400)   # 绝对上限：自登录起
LEEWAY_SECONDS = max(0, _env_int("REFRESH_ROTATION_LEEWAY_SECONDS", 30))    # 轮换竞态宽限
UA_MAX_LEN = 160
_SID_RE = re.compile(r"^[0-9a-f]{32}$")


class RefreshInvalid(Exception):
    """refresh token 无效 / 过期 / 被撤销 / 疑似泄露。HTTP 层转 401。"""

    def __init__(self, detail: str, code: str = "invalid_refresh_token", user_id: str = ""):
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.reuse = code == "refresh_token_reuse"
        self.user_id = user_id          # 供审计留痕（谁被判定为泄露）


class AuthBackendUnavailable(Exception):
    """会话存储不可用。HTTP 层转 503（fail-closed：绝不放行）。"""

    def __init__(self, detail: str = "登录状态服务暂时不可用，请稍后重试"):
        super().__init__(detail)
        self.detail = detail


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _sess_key(sid: str) -> str:
    return f"{AUTH_PREFIX}:auth:sess:{sid}"


def _user_key(user_id: str) -> str:
    return f"{AUTH_PREFIX}:auth:user:{user_id}"


def _hash(token: str) -> str:
    """只存哈希：Redis 泄露 ≠ 会话可被直接冒用。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _build_token(sid: str, secret: str) -> str:
    """refresh token = ``{sid}.{secret}``（sid 随 token 走，便于定位/撤销会话）。"""
    return f"{sid}.{secret}"


def _parse_sid(token: str) -> str:
    """取出并校验 sid —— sid 会进 Redis key，必须白名单校验（防 key 注入）。"""
    sid = (token or "").split(".", 1)[0]
    if not _SID_RE.match(sid):
        raise RefreshInvalid("无效的登录凭证", "malformed_refresh_token")
    return sid


def _require():
    try:
        return redis_client.require_client()
    except redis_client.RedisUnavailable as e:
        raise AuthBackendUnavailable(
            "登录状态服务暂时不可用（Redis 不可达），请稍后重试"
        ) from e


async def _forget_sid(client, user_id: str, sid: str) -> None:
    """把会话从"该用户的会话集合"里摘掉（失败只记日志：list_sessions 会自愈清理）。"""
    if not user_id:
        return
    try:
        await client.srem(_user_key(user_id), sid)
    except Exception as e:  # noqa: BLE001
        logger.warning("[鉴权] 会话索引清理失败（不影响撤销本身）: %s", e)


# 原子轮换：判定（现值 / leeway 宽限 / 重用）+ 换发 + 续期一次做完。
# 拆成多条命令会有竞态：两个并发刷新都认为自己是合法的。
# 注意：只操作 sess 这一个 key；user set 的 SREM 放到脚本外（那里才知道 user_id），
# 少一个 key 也避免了在 Lua 里拼 key 名。
_ROTATE_LUA = """
-- KEYS[1]=sess key
-- ARGV: 1=now 2=new_hash 3=idle 4=absolute 5=leeway 6=presented_hash
local cur = redis.call('HGET', KEYS[1], 'hash')
if not cur then return {'invalid'} end
local uid = redis.call('HGET', KEYS[1], 'user_id')
local role = redis.call('HGET', KEYS[1], 'role')
local now = tonumber(ARGV[1])
local born = tonumber(redis.call('HGET', KEYS[1], 'born_at') or '0')
if now - born > tonumber(ARGV[4]) then
  redis.call('DEL', KEYS[1])
  return {'absolute_expired', uid, role}
end
local presented = ARGV[6]
local grace = 0
if presented ~= cur then
  local prev = redis.call('HGET', KEYS[1], 'prev_hash')
  local prev_at = tonumber(redis.call('HGET', KEYS[1], 'prev_at') or '0')
  if prev and presented == prev and (now - prev_at) < tonumber(ARGV[5]) then
    grace = 1
  else
    redis.call('DEL', KEYS[1])
    return {'reuse', uid, role}
  end
end
redis.call('HSET', KEYS[1], 'hash', ARGV[2], 'prev_hash', presented,
           'prev_at', now, 'last_at', now)
redis.call('EXPIRE', KEYS[1], ARGV[3])
return {'ok', uid, role, grace}
"""


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------

async def create_session(user_id: str, role: str, ua: str = "", ip: str = "") -> dict:
    """登录成功：新建会话，返回 refresh token + 会话 id。"""
    sid = uuid.uuid4().hex
    token = _build_token(sid, secrets.token_urlsafe(32))
    now = int(time.time())
    client = _require()
    try:
        pipe = client.pipeline(transaction=False)
        pipe.hset(_sess_key(sid), mapping={
            "user_id": user_id,
            "role": role,
            "hash": _hash(token),
            "born_at": now,
            "last_at": now,
            "ua": (ua or "")[:UA_MAX_LEN],
            "ip": ip or "",
        })
        pipe.expire(_sess_key(sid), IDLE_SECONDS)
        pipe.sadd(_user_key(user_id), sid)
        pipe.expire(_user_key(user_id), ABSOLUTE_SECONDS)
        await pipe.execute()
    except Exception as e:  # noqa: BLE001
        logger.warning("[鉴权] 创建会话失败（Redis 不可用）: %s", e)
        raise AuthBackendUnavailable() from e
    return {
        "sid": sid,
        "refresh_token": token,
        "idle_expires_in": IDLE_SECONDS,
        "absolute_expires_in": ABSOLUTE_SECONDS,
    }


async def rotate(refresh_token: str, ua: str = "") -> dict:
    """用 refresh 换一对新凭证（轮换 + 重用检测 + 双封顶续期）。

    返回 ``{sid, refresh_token, user_id, role}``。
    失败：``RefreshInvalid``（401）/ ``AuthBackendUnavailable``（503）。
    """
    sid = _parse_sid(refresh_token)
    new_secret = secrets.token_urlsafe(32)
    client = _require()
    try:
        result = await client.eval(
            _ROTATE_LUA, 1, _sess_key(sid),
            int(time.time()), _hash(f"{sid}.{new_secret}"),
            IDLE_SECONDS, ABSOLUTE_SECONDS, LEEWAY_SECONDS, _hash(refresh_token),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[鉴权] 刷新失败（Redis 不可用）: %s", e)
        raise AuthBackendUnavailable() from e

    status = result[0] if result else "invalid"
    user_id = result[1] if len(result) > 1 else None
    role = result[2] if len(result) > 2 else None
    grace = bool(result[3]) if len(result) > 3 else False

    if status == "absolute_expired":
        await _forget_sid(client, user_id, sid)
        logger.info("[鉴权] 会话超过绝对上限已失效 sid=%s user=%s", sid, user_id)
        raise RefreshInvalid("登录时间过长，请重新登录", "refresh_absolute_expired", user_id)
    if status == "reuse":
        await _forget_sid(client, user_id, sid)
        # 安全事件：旧 token 被人重放 → 该会话视为已泄露，整体作废。
        # 副作用要如实承认：攻击者拿到旧 token 就能让该会话被注销（用户需重登）——
        # 但"宁可登出也不留活口"，这是刻意的选择。
        logger.warning("[鉴权] 检测到 refresh token 重复使用，已作废会话 sid=%s user=%s",
                       sid, user_id)
        raise RefreshInvalid("检测到登录凭证被重复使用，该会话已注销，请重新登录",
                             "refresh_token_reuse", user_id)
    if status != "ok":
        raise RefreshInvalid("登录状态已失效，请重新登录", "refresh_session_gone")
    if grace:
        logger.info("[鉴权] 轮换竞态宽限命中（并发刷新）sid=%s user=%s", sid, user_id)

    return {
        "sid": sid,
        "refresh_token": _build_token(sid, new_secret),
        "user_id": user_id,
        "role": role,
        "grace": grace,
    }


async def revoke(refresh_token: str) -> bool:
    """撤销该 refresh 所属会话（登出）。幂等：不存在也算成功。"""
    sid = _parse_sid(refresh_token)
    client = _require()
    key = _sess_key(sid)
    try:
        user_id = await client.hget(key, "user_id")
        await client.delete(key)
    except Exception as e:  # noqa: BLE001
        logger.warning("[鉴权] 撤销会话失败（Redis 不可用）: %s", e)
        raise AuthBackendUnavailable() from e
    await _forget_sid(client, user_id or "", sid)
    return True


async def revoke_session(user_id: str, sid: str) -> bool:
    """踢掉某个会话（校验归属，防越权）。返回 False = 不存在/不属于该用户。"""
    if not _SID_RE.match(sid or ""):
        return False
    client = _require()
    key = _sess_key(sid)
    try:
        owner = await client.hget(key, "user_id")
        if owner != user_id:
            return False
        await client.delete(key)
    except Exception as e:  # noqa: BLE001
        logger.warning("[鉴权] 踢会话失败（Redis 不可用）: %s", e)
        raise AuthBackendUnavailable() from e
    await _forget_sid(client, user_id, sid)
    return True


async def revoke_all(user_id: str) -> int:
    """该用户全端下线（改密码/封号时用）。返回撤销的会话数。"""
    client = _require()
    try:
        sids = await client.smembers(_user_key(user_id))
        if sids:
            await client.delete(*[_sess_key(s) for s in sids])
        await client.delete(_user_key(user_id))
    except Exception as e:  # noqa: BLE001
        logger.warning("[鉴权] 全端下线失败（Redis 不可用）: %s", e)
        raise AuthBackendUnavailable() from e
    return len(sids or [])


async def list_sessions(user_id: str, current_sid: str = "") -> list:
    """列出该用户当前有效的登录会话（顺带清理已失效的索引，自愈）。"""
    client = _require()
    try:
        sids = list(await client.smembers(_user_key(user_id)))
        if not sids:
            return []
        pipe = client.pipeline(transaction=False)
        for sid in sids:
            pipe.hgetall(_sess_key(sid))
        rows = await pipe.execute()
    except Exception as e:  # noqa: BLE001
        logger.warning("[鉴权] 列会话失败（Redis 不可用）: %s", e)
        raise AuthBackendUnavailable() from e

    sessions, stale = [], []
    for sid, row in zip(sids, rows):
        if not row:
            stale.append(sid)
            continue
        sessions.append({
            "sid": sid,
            "current": sid == current_sid,
            "ua": row.get("ua", ""),
            "ip": row.get("ip", ""),
            "created_at": int(row.get("born_at") or 0),
            "last_used_at": int(row.get("last_at") or 0),
        })
    for sid in stale:                       # 顺手清掉过期会话的索引（不阻塞返回）
        await _forget_sid(client, user_id, sid)
    sessions.sort(key=lambda s: s["last_used_at"], reverse=True)
    return sessions


async def ping() -> bool:
    """探活（/healthz 与测试用）。Redis 不可用 → False（不抛）。"""
    try:
        client = redis_client.require_client()
        await client.ping()
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 凭证版本缓存（"秒级吊销"的支点）
# ---------------------------------------------------------------------------
# 背景：access token 无状态 → 改密码/封号后，已签发的 access 在 TTL 内仍然有效
# （最长 15 分钟）。要缩短这个窗口，就得让每个请求都能知道"这个版本号还有效吗"。
#
# 代价与取舍（务必说清楚）：
# - 每请求都查 Redis/DB 会把 15 分钟一次的 IO 变成"每请求一次 IO"，违背短期 access 的初衷；
# - 所以这里用 **60 秒读穿缓存**：绝大多数请求只命中进程内 dict（零 IO），
#   每 60 秒（或撤销发生时）才回源一次 Redis→DB；
# - 于是撤销窗口 = min(access TTL, 缓存 TTL) ≈ **≤60 秒**（改密码/封号立即 bump 版本，
#   并同步清缓存 → 本进程立刻生效，多副本 ≤60s 收敛）；
# - Redis/DB 都不可用时 **fail-open**（放行 + 告警）：access 本身已被 TTL 限制，
#   而这里绝不能把"缓存故障"升级成全站 401。
# 想恢复"热路径零查询"的原始设计：设 AUTH_REVOCATION_CHECK=0。

VERSION_CACHE_TTL = max(0, _env_int("AUTH_VERSION_CACHE_SECONDS", 60))
# 读不到版本时的"未知"标记缓存几秒：避免故障期间每个请求都去重试 Redis+DB（放大故障）
VERSION_FAIL_CACHE_TTL = max(1, _env_int("AUTH_VERSION_FAIL_CACHE_SECONDS", 10))
REVOCATION_CHECK = (os.getenv("AUTH_REVOCATION_CHECK", "1") or "1").strip().lower() \
    not in ("0", "false", "no")
_version_cache: dict = {}          # user_id -> (expire_at_monotonic, version)  version=-1 表示未知


def _version_key(user_id: str) -> str:
    return f"{AUTH_PREFIX}:auth:ver:{user_id}"


async def current_token_version(user_id: str) -> int:
    """该用户当前的凭证版本（带缓存）。

    Redis 里放副本是为了让多副本快速收敛（不必每个副本都打 DB）；
    DB 是唯一真相来源 —— Redis 数据丢了也只是多查一次 DB，不会放行错的人。
    """
    now = time.monotonic()
    hit = _version_cache.get(user_id)
    if hit and hit[0] > now:
        return hit[1]

    version = None
    try:                                   # ① Redis 副本（可能没有 → 回源 DB）
        client = redis_client.get_client()
        if client is not None:
            raw = await client.get(_version_key(user_id))
            if raw is not None:
                version = int(raw)
    except Exception as e:  # noqa: BLE001
        logger.warning("[鉴权] 版本缓存读取失败（放行，靠 TTL 兜底）: %s", e)

    if version is None:
        try:                               # ② 真相来源
            from src.users import get_token_version
            version = await get_token_version(user_id)
            client = redis_client.get_client()
            if client is not None:
                await client.set(_version_key(user_id),
                                 version, ex=max(VERSION_CACHE_TTL, 60))
        except Exception as e:  # noqa: BLE001
            logger.warning("[鉴权] 版本回源失败（fail-open，放行靠 TTL 兜底）: %s", e)
            # 把"未知"也缓存一小会儿：否则故障期间每个请求都会重试 Redis+DB，把故障放大
            _version_cache[user_id] = (now + VERSION_FAIL_CACHE_TTL, -1)
            return -1                      # -1 表示"未知"：调用方一律放行

    if VERSION_CACHE_TTL:
        _version_cache[user_id] = (now + VERSION_CACHE_TTL, version)
    return version


async def invalidate_token_version(user_id: str) -> None:
    """撤销发生时清缓存（本进程立刻生效；多副本靠 60s TTL 收敛）。"""
    _version_cache.pop(user_id, None)
    try:
        client = redis_client.get_client()
        if client is not None:
            await client.delete(_version_key(user_id))
    except Exception as e:  # noqa: BLE001
        logger.warning("[鉴权] 版本缓存清理失败: %s", e)


def reset_version_cache() -> None:
    """清空进程内版本缓存（测试隔离用）。"""
    _version_cache.clear()
