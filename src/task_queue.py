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

# 线程退出哨兵对象，放到队列通知worker线程退出
_STOP = object()


class TaskQueue:
    """
    自定义线程任务队列。
    基于queue.Queue手动实现的简易后台工作队列，用于跑不阻塞主流程的异步后台任务。
    主要特性：
    1. 多worker工作线程，daemon守护线程，主进程退出时线程自动回收
    2. submit非阻塞提交；队列达到maxsize上限直接丢弃任务，返回False
    3. 单个任务抛出异常不会杀死worker线程，仅打印异常日志，继续处理后续任务
    4. 内置计数器指标，可用于健康检查、监控观测队列压力
    5. 支持shutdown优雅关闭，等待已有任务执行完毕再退出

    ⚠️重要限制：
    - 队列满直接丢弃任务，适合允许任务丢失的次要后台任务；强可靠性业务不适用
    - submit无返回值，无法获取任务执行结果，没有Future回调
    - 内存队列，进程重启后队列中未完成任务全部丢失
    """
    def __init__(self, maxsize: int = 200, workers: int = 2, name: str = "task"):
        """
        :param maxsize: 队列最大任务容量，超过此值提交任务会被丢弃
        :param workers: 后台工作线程数量
        :param name: 队列名称，用于日志、监控区分不同队列实例
        """
        self._q: "queue.Queue" = queue.Queue(maxsize=maxsize)
        self._workers: list = []
        self._name = name
        self._submitted = 0    # 累计提交任务总数
        self._processed = 0    # 累计处理完成任务总数
        self._dropped = 0      # 累计因队列满被丢弃的任务数
        self._lock = threading.Lock()  # 保护多线程下计数器安全

        # 启动worker守护线程
        for i in range(workers):
            t = threading.Thread(
                target=self._run, name=f"{name}-worker-{i}", daemon=True
            )
            t.start()
            self._workers.append(t)

    def _run(self) -> None:
        """worker线程主循环：不断从队列取任务执行。"""
        while True:
            item = self._q.get()
            # 收到哨兵，退出线程
            if item is _STOP:
                self._q.task_done()
                break
            fn, args, kwargs = item
            try:
                fn(*args, **kwargs)
            except Exception:
                # 捕获所有异常，打印堆栈，保证单个任务失败不会崩掉worker
                logger.exception("[任务队列] 任务执行失败")
            finally:
                with self._lock:
                    self._processed += 1
                self._q.task_done()

    def submit(self, fn: Callable, *args, **kwargs) -> bool:
        """
        非阻塞提交任务到后台队列。
        :param fn: 需要后台执行的函数
        :param args: 函数位置参数
        :param kwargs: 函数关键字参数
        :return: True=入队成功；False=队列已满，任务被丢弃
        """
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
        """获取队列运行指标，用于健康检查接口/监控上报。"""
        with self._lock:
            return {
                "name": self._name,
                "queued": self._q.qsize(),
                "submitted": self._submitted,
                "processed": self._processed,
                "dropped": self._dropped,
            }

    def shutdown(self, timeout: float = 5.0) -> None:
        """
        优雅关闭队列。
        停止接收新任务，等待worker完成现有任务；daemon线程兜底避免卡死。
        :param timeout: 单个线程最大等待秒数
        """
        # 给每个worker发送退出哨兵
        for _ in self._workers:
            try:
                self._q.put(_STOP, timeout=timeout)
            except queue.Full:
                logger.warning(f"[任务队列] {self._name} 关闭时队列已满，跳过哨兵")
        # 等待所有工作线程退出
        for t in self._workers:
            t.join(timeout=timeout)


# ── 全局单例：记忆提取后台任务队列 ────────────────────────────────
_extraction_queue: Optional[TaskQueue] = None
_extraction_queue_lock = threading.Lock()


def get_extraction_queue() -> TaskQueue:
    """
    获取全局记忆提取任务队列（懒初始化单例）。
    第一次调用才实例化，多线程安全，全局复用同一个队列实例。
    """
    global _extraction_queue
    if _extraction_queue is None:
        with _extraction_queue_lock:
            if _extraction_queue is None:
                _extraction_queue = TaskQueue(
                    maxsize=200, workers=2, name="mem-extract"
                )
    return _extraction_queue
