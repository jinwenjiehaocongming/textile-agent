"""
有界任务队列
============
替代「每请求裸开 threading.Thread」（app.py / agent.py / stream_chat.py 原用法）：

- 固定 worker 池（默认 2），任务排队执行，线程数有上限
- 队列有界（默认 200），满时丢弃并告警（记忆提取属尽力而为，可丢）
- 请求延迟与后台任务解耦；daemon 线程保证进程退出不挂死
- 带轻量计量（submitted/processed/dropped/queued），供 /healthz 观测

生产可替换为 Redis Stream / Celery / arq（跨进程、可独立扩缩容），
本模块是单进程部署下的标准做法；接口保持一致即可无缝切换。
"""

import logging
import queue
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_STOP = object()


class TaskQueue:
    def __init__(self, maxsize: int = 200, workers: int = 2, name: str = "task"):
        self._q: "queue.Queue" = queue.Queue(maxsize=maxsize)
        self._workers: list = []
        self._name = name
        self._submitted = 0
        self._processed = 0
        self._dropped = 0
        self._lock = threading.Lock()
        for i in range(workers):
            t = threading.Thread(
                target=self._run, name=f"{name}-worker-{i}", daemon=True
            )
            t.start()
            self._workers.append(t)

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is _STOP:
                self._q.task_done()
                break
            fn, args, kwargs = item
            try:
                fn(*args, **kwargs)
            except Exception:
                logger.exception("[任务队列] 任务执行失败")
            finally:
                with self._lock:
                    self._processed += 1
                self._q.task_done()

    def submit(self, fn: Callable, *args, **kwargs) -> bool:
        """入队（非阻塞）。队列满 → 丢弃并告警，返回 False。"""
        try:
            self._q.put_nowait((fn, args, kwargs))
        except queue.Full:
            with self._lock:
                self._dropped += 1
            logger.warning(
                f"[任务队列] {self._name} 队列已满，任务丢弃（累计丢 {self._dropped}）"
            )
            return False
        with self._lock:
            self._submitted += 1
        return True

    def metrics(self) -> dict:
        """轻量计量，供 /healthz 与观测。"""
        with self._lock:
            return {
                "name": self._name,
                "queued": self._q.qsize(),
                "submitted": self._submitted,
                "processed": self._processed,
                "dropped": self._dropped,
            }

    def shutdown(self, timeout: float = 5.0) -> None:
        """优雅关闭：停止接收新任务，等 worker 收尾（daemon 兜底不挂死）。"""
        for _ in self._workers:
            try:
                self._q.put(_STOP, timeout=timeout)
            except queue.Full:
                logger.warning(f"[任务队列] {self._name} 关闭时队列已满，跳过哨兵")
        for t in self._workers:
            t.join(timeout=timeout)


# ── 全局单例：记忆提取队列 ────────────────────────────────
_extraction_queue: Optional[TaskQueue] = None
_extraction_queue_lock = threading.Lock()


def get_extraction_queue() -> TaskQueue:
    """获取全局记忆提取队列（懒初始化单例）。"""
    global _extraction_queue
    if _extraction_queue is None:
        with _extraction_queue_lock:
            if _extraction_queue is None:
                _extraction_queue = TaskQueue(
                    maxsize=200, workers=2, name="mem-extract"
                )
    return _extraction_queue