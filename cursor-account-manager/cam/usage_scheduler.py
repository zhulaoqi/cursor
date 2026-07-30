"""用量快照的独立调度器，不受旧 BI 调度长任务影响。"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from datetime import datetime, timedelta, timezone
from typing import Callable

from .config import SETTINGS
from .logger import get
from .usage_snapshot_refresh import run_usage_periodic, run_usage_pre_reset_due


log = get("usage_scheduler")
_singleton_lock = threading.Lock()
_singleton: UsageSchedulerCoordinator | None = None


class UsageSchedulerCoordinator:
    """分别调度 periodic 与 pre-reset，后者不会等待前者完成。"""

    def __init__(self, *, poll_interval_sec: int = 15):
        self._poll_interval_sec = max(1, poll_interval_sec)
        self._periodic_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="cam-usage-periodic"
        )
        self._pre_reset_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="cam-usage-pre-reset"
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._periodic_future: Future[object] | None = None
        self._pre_reset_future: Future[object] | None = None
        self._next_periodic_at: datetime | None = None
        self._next_pre_reset_at: datetime | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """启动一条计时线程；同一 coordinator 的重复调用无副作用。"""
        with self._lock:
            if self.is_running:
                return
            if self._stop.is_set():
                raise RuntimeError("已停止的 UsageSchedulerCoordinator 不能重新启动")
            self._thread = threading.Thread(
                target=self._run,
                name="cam-usage-scheduler",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick(datetime.now(timezone.utc))
            except Exception:
                # 调度判断异常不能终止计时线程，下次轮询仍可恢复。
                log.exception("用量调度轮询异常")
            self._stop.wait(self._poll_interval_sec)

    def tick(self, now: datetime) -> None:
        """在给定时刻提交到期任务；pre-reset 始终先于 periodic 检查。"""
        if self._stop.is_set():
            return
        current = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
        with self._lock:
            if self._is_due(self._pre_reset_future, self._next_pre_reset_at, current):
                self._submit_pre_reset(current)
            if self._is_due(self._periodic_future, self._next_periodic_at, current):
                self._submit_periodic(current)

    @staticmethod
    def _is_due(
        future: Future[object] | None,
        due_at: datetime | None,
        now: datetime,
    ) -> bool:
        return (future is None or future.done()) and (due_at is None or now >= due_at)

    def _submit_periodic(self, now: datetime) -> None:
        self._next_periodic_at = now + timedelta(
            hours=SETTINGS.usage_periodic_interval_hours
        )
        self._periodic_future = self._submit(
            self._periodic_pool, "periodic", run_usage_periodic, now
        )

    def _submit_pre_reset(self, now: datetime) -> None:
        self._next_pre_reset_at = now + timedelta(
            minutes=SETTINGS.usage_pre_reset_scan_interval_min
        )
        self._pre_reset_future = self._submit(
            self._pre_reset_pool, "pre-reset", run_usage_pre_reset_due, now
        )

    @staticmethod
    def _submit(
        pool: ThreadPoolExecutor,
        task_name: str,
        worker: Callable[..., object],
        now: datetime,
    ) -> Future[object]:
        future = pool.submit(worker, now=now)

        def report_result(done: Future[object]) -> None:
            try:
                done.result()
            except Exception:
                log.exception("用量 %s 任务异常", task_name)

        future.add_done_callback(report_result)
        return future

    def stop(self, *, timeout_sec: float = 30) -> None:
        """停止新提交，优先等待 pre-reset，取消尚未执行的 periodic。"""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0, timeout_sec))

        deadline = max(0, timeout_sec)
        future = self._pre_reset_future
        if future is not None and not future.done():
            try:
                future.result(timeout=deadline)
            except TimeoutError:
                log.warning("等待 pre-reset 任务停止超时")
            except Exception:
                # 回调已记录异常；停止路径只需继续回收执行器。
                pass

        self._periodic_pool.shutdown(wait=False, cancel_futures=True)
        self._pre_reset_pool.shutdown(wait=False, cancel_futures=False)


def start_usage_scheduler_once() -> UsageSchedulerCoordinator:
    """按进程复用唯一 coordinator，仅在用量快照启用时启动。"""
    global _singleton
    with _singleton_lock:
        if _singleton is None or _singleton._stop.is_set():
            _singleton = UsageSchedulerCoordinator()
        if SETTINGS.usage_snapshot_enable:
            _singleton.start()
        return _singleton


def stop_usage_scheduler(*, timeout_sec: float = 30) -> None:
    """停止当前进程的用量调度器，重复调用安全。"""
    global _singleton
    with _singleton_lock:
        coordinator = _singleton
        _singleton = None
    if coordinator is not None:
        coordinator.stop(timeout_sec=timeout_sec)
