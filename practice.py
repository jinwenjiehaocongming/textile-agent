"""
面试算法练习 — Agent 岗
=======================
Agent 岗位算法难度比纯后端低，重点考：
  1. 设计题（LRU、线程安全）
  2. 哈希表 / 堆 / 栈
  3. 字符串处理

本题：2 道，先做再做下一批

运行: python practice.py
"""

# ============================================================
# 题目 1: LRU Cache（设计题，面试最高频）
# ============================================================
# 设计一个 LRU（最近最少使用）缓存，支持 get(key) 和 put(key, value)。
# 容量满了删最久没用的。get 和 put 都 O(1)。
#
# 示例:
#   cache = LRUCache(2)
#   cache.put(1, 1)     # 缓存: {1=1}
#   cache.put(2, 2)     # 缓存: {1=1, 2=2}
#   cache.get(1)        # 返回 1, 1 变最新
#   cache.put(3, 3)     # 删掉 2, 缓存 {1=1, 3=3}
#   cache.get(2)        # -1（被删了）
#
# 提示: 哈希表 + 双向链表，Python 可以用 OrderedDict


class LRUCache:
    """
    OrderedDict 实现。dict 天然记住插入顺序，
    每次 get 或 put 把 key 移到末尾 = "最近使用"。
    满了删第一个 = "最久没用"。
    """

    def __init__(self, capacity: int):
        from collections import OrderedDict
        self.cap = capacity
        self.cache = OrderedDict()

    def get(self, key: int) -> int:
        if key not in self.cache:
            return -1
        # 移到末尾 = 标记为最新使用
        self.cache.move_to_end(key)
        return self.cache[key]

    def put(self, key: int, value: int) -> None:
        if key in self.cache:
            # 更新值 + 移到末尾
            self.cache.move_to_end(key)
        self.cache[key] = value
        # 满了删最前面 = 最久没用
        if len(self.cache) > self.cap:
            self.cache.popitem(last=False)


# ============================================================
# 题目 2: 消息去重 — 每 10 秒窗口内重复消息只保留一条
# ============================================================
# Agent 收到用户消息："你好" → 1 秒后又收到一条 "你好" → 再收到 "你好"
# 设计一个去重器，同一个用户在 window 秒内重复发相同内容的消息，只算一条。
#
# 示例:
#   dedup = MessageDeduplicator(window=5)  # 5 秒窗口
#   dedup.is_duplicate("user_1", "你好")  → False (第一次)
#   dedup.is_duplicate("user_1", "你好")  → True  (5秒内重复)
#   dedup.is_duplicate("user_1", "你好吗") → False (不同内容)
#   # 6 秒后...
#   dedup.is_duplicate("user_1", "你好")  → False (过期了，不算重复)
#
# 提示: 用 dict 存 {user_id: (last_content, last_time)}


class MessageDeduplicator:
    """
    dict 存 {user_id: (last_content, last_time)}
    每次检查：同一个 user + 同一个 content + 时间差 < window → 去重
    """

    def __init__(self, window: float = 10.0):
        self.window = window
        self.last_msg: dict[str, tuple[str, float]] = {}

    def is_duplicate(self, user_id: str, content: str) -> bool:
        import time
        now = time.time()
        if user_id in self.last_msg:
            prev_content, prev_time = self.last_msg[user_id]
            # 内容相同 且 在窗口期内 → 重复
            if prev_content == content and (now - prev_time) <= self.window:
                return True
        # 不是重复 → 更新记录
        self.last_msg[user_id] = (content, now)
        return False


# ============================================================
# 测试
# ============================================================
def test_lru():
    cache = LRUCache(2)
    cache.put(1, 1)
    cache.put(2, 2)
    assert cache.get(1) == 1, "get(1) 应该返回 1"
    cache.put(3, 3)
    assert cache.get(2) == -1, "get(2) 应该返回 -1（被淘汰了）"
    cache.put(4, 4)
    assert cache.get(1) == -1, "get(1) 应该返回 -1（被淘汰了）"
    assert cache.get(3) == 3, "get(3) 应该返回 3"
    assert cache.get(4) == 4, "get(4) 应该返回 4"
    print("✅ LRU Cache 通过")


def test_dedup():
    import time
    dedup = MessageDeduplicator(window=0.5)
    assert not dedup.is_duplicate("u1", "你好"), "第一次不应去重"
    assert dedup.is_duplicate("u1", "你好"), "0.5秒内重复应去重"
    assert not dedup.is_duplicate("u1", "你好吗"), "不同内容不应去重"
    assert not dedup.is_duplicate("u2", "你好"), "不同用户不应去重"
    time.sleep(0.6)
    assert not dedup.is_duplicate("u1", "你好"), "过期后不应去重"
    print("✅ 消息去重 通过")


if __name__ == "__main__":
    test_lru()
    test_dedup()
