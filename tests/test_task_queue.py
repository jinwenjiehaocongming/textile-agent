"""有界任务队列测试（P0-5：替代裸 threading.Thread）"""
import threading
import time

from src.task_queue import TaskQueue, get_extraction_queue


def test_tasks_executed_in_order():
    q = TaskQueue(maxsize=50, workers=1, name="test-a")
    seen = []
    for i in range(10):
        q.submit(lambda v=i: seen.append(v))
    # worker 是异步的，轮询等待全部处理完
    deadline = time.time() + 5
    while len(seen) < 10 and time.time() < deadline:
        time.sleep(0.01)
    assert seen == list(range(10))
    assert q.metrics()["processed"] >= 10


def test_queue_full_drops_and_metrics():
    q = TaskQueue(maxsize=2, workers=1, name="test-b")
    started = threading.Event()
    release = threading.Event()

    def blocker():
        started.set()
        release.wait(5)

    # 1 个正在执行（worker 已取走，队列空）
    assert q.submit(blocker)
    assert started.wait(2)
    # 再塞 2 个 → 队列满（maxsize=2）
    assert q.submit(blocker)
    assert q.submit(blocker)
    # 第 4 个应被丢弃
    assert q.submit(blocker) is False

    m = q.metrics()
    assert m["dropped"] == 1
    assert m["submitted"] == 3
    assert m["queued"] == 2
    release.set()


def test_failing_task_does_not_kill_worker():
    q = TaskQueue(maxsize=10, workers=1, name="test-c")

    def boom():
        raise RuntimeError("boom")

    def fine():
        pass

    assert q.submit(boom)
    assert q.submit(fine)
    # 炸弹任务失败被捕获，后续任务仍能执行
    time.sleep(0.3)
    assert q.metrics()["processed"] >= 2


def test_extraction_queue_singleton():
    a = get_extraction_queue()
    b = get_extraction_queue()
    assert a is b
    assert a.metrics()["name"] == "mem-extract"