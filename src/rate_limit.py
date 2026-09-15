"""速率限制 — Redis 令牌桶（2026-09）
====================================
用途：挡住 `/auth/*` 的暴力破解与刷接口（OAuth 2.0 BCP 的 *Initial Login Timeout*
与 OWASP 的"会话/登录暴力破解检测"落地）。

设计要点
========
1. **令牌桶**而不是固定窗口：固定窗口有"边界双倍"问题（59 秒打满 + 61 秒再打满 = 两倍
   配额）；令牌桶按时间连续补充，行为更平滑。桶状态 `{tokens, ts}` 存 Redis HASH。
2. **原子性**：判定 + 扣减写成一段 **Lua**（读改写三步一体），否则并发请求会同时通过。
3. **按维度分桶**：IP、账号、账号+IP、会话，各用各的桶。特别是
   *账号+IP* 用来做登录失败锁定 —— 只按账号锁会被人拿来"故意锁死别人账号"（DoS）。
4. **fail-open**：Redis 不可用时**放行**并告警。理由：限流是"降低风险"不是"授权判定"，
   把它变成 fail-closed 会让 Redis 抖动直接升级成"谁都登不上"。
   注：`/auth/login` 本身还依赖会话存储（fail-closed 503），所以 Redis 真挂了也进不来。
"""

import math
import os
import time

from src import redis_client
from src.logging_config import get_logger

logger = get_logger(__name__)

RL_PREFIX = (os.getenv("REDIS_KEY_PREFIX", "study1") or "study1").strip(":")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        logger.warning("环境变量 %s 不是整数，回退默认值 %s", name, default)
        return default


# ── 策略（capacity=突发上限，per_seconds=补满整桶所需时间）──────
# 登录：同一 IP 每分钟 20 次（正常用户手速远达不到；脚本爆破立刻撞墙）
LOGIN_IP = (_env_int("RL_LOGIN_IP_CAPACITY", 20), 60)
# 登录失败锁定：同一 账号+IP 15 分钟内最多 5 次失败 → 之后 429（并带 Retry-After）
LOGIN_FAIL = (_env_int("RL_LOGIN_FAIL_CAPACITY", 5), 15 * 60)
# 注册：同一 IP 每小时 10 个账号（防批量注册）
REGISTER_IP = (_env_int("RL_REGISTER_IP_CAPACITY", 10), 3600)
# 刷新：单个会话每分钟 60 次（正常 15 分钟才 1 次，这里只是防脚本刷）
REFRESH_SESSION = (_env_int("RL_REFRESH_SESSION_CAPACITY", 60), 60)
# 业务接口（LLM 调用）：按 **user_id** 限流 —— 为什么不是按 IP？
# - 登录时还不知道用户是谁，只能按 IP；而 /chat 已经从 JWT 拿到 user_id，
#   用身份做桶才准：客户端换 IP/挂代理绕不过，NAT 下同公司的人也不会互相误伤；
# - /chat 的成本是真金白银的 LLM 调用（还常带一次后台偏好提取），所以额度要按人算。
CHAT_USER = (_env_int("RL_CHAT_USER_CAPACITY", 30), 60)
# 数据分析：一次请求会跑 5 次 LLM + 若干只读 SQL，成本远高于聊天 → 桶更紧（默认 10 次/5 分钟）
ANALYTICS_USER = (_env_int("RL_ANALYTICS_USER_CAPACITY", 10), 300)


class RateLimited(Exception):
    """触发限流。HTTP 层转 429 + Retry-After。"""

    def __init__(self, detail: str, retry_after: int):
        super().__init__(detail)
        self.detail = detail
        self.retry_after = max(1, int(retry_after))


def _bucket_key(scope: str, identity: str) -> str:
    return f"{RL_PREFIX}:rl:{scope}:{identity}"


# 令牌桶：KEYS[1]=桶；ARGV: capacity, per_seconds, now, cost
# 返回 {allowed(0/1), 剩余令牌, 需要等待的秒数}
_TOKEN_BUCKET_LUA = """
local capacity = tonumber(ARGV[1])
local per = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local rate = capacity / per                       -- 每秒补充的令牌数
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = capacity; ts = now end
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * rate)
local allowed = 0
local wait = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  wait = math.ceil((cost - tokens) / rate)
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], math.ceil(per * 2))
return {allowed, math.floor(tokens), wait}
"""


class Decision:
    __slots__ = ("allowed", "remaining", "retry_after")

    def __init__(self, allowed: bool, remaining: int, retry_after: int):
        self.allowed = allowed
        self.remaining = remaining
        self.retry_after = retry_after


async def hit(scope: str, identity: str, policy: tuple, cost: int = 1) -> Decision:
    """消耗一个令牌。Redis 不可用 → 放行（fail-open，只告警）。"""
    capacity, per_seconds = policy
    client = redis_client.get_client()
    if client is None:
        return Decision(True, capacity, 0)
    try:
        allowed, remaining, wait = await client.eval(
            _TOKEN_BUCKET_LUA, 1, _bucket_key(scope, identity),
            capacity, per_seconds, time.time(), cost,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[限流] Redis 不可用，本层放行（fail-open）: %s", e)
        return Decision(True, capacity, 0)
    return Decision(bool(int(allowed)), int(remaining), int(wait))


async def reset(scope: str, identity: str) -> None:
    """清空某个桶（登录成功后清掉失败计数）。"""
    client = redis_client.get_client()
    if client is None:
        return
    try:
        await client.delete(_bucket_key(scope, identity))
    except Exception as e:  # noqa: BLE001
        logger.warning("[限流] 桶重置失败: %s", e)


async def clear_all() -> None:
    """清空所有限流桶（测试隔离 / 运维手动解封用）。只扫自己的命名空间。"""
    client = redis_client.get_client()
    if client is None:
        return
    try:
        async for key in client.scan_iter(match=f"{RL_PREFIX}:rl:*", count=200):
            await client.delete(key)
    except Exception as e:  # noqa: BLE001
        logger.warning("[限流] 清空失败: %s", e)


async def enforce(scope: str, identity: str, policy: tuple,
                  message: str = "操作过于频繁，请稍后再试") -> None:
    """不满足则抛 RateLimited（HTTP 层统一转 429 + Retry-After）。"""
    decision = await hit(scope, identity, policy)
    if not decision.allowed:
        wait = max(1, math.ceil(decision.retry_after))
        raise RateLimited(f"{message}（{wait} 秒后可重试）", wait)
