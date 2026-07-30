"""用量快照的独立调度器，不受旧 BI 调度长任务影响。"""

from __future__ import annotations

import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from .config import SETTINGS
from .logger import get
from .usage_snapshot_refresh import run_usage_periodic, run_usage_pre_reset_due


log = get("usage_scheduler")
_singleton_lock = threading.Lock()
_singleton: UsageSchedulerCoordinator | None = None
_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_usage_periodic_daily_at(value: str) -> tuple[int, int]:
    """解析 USAGE_PERIODIC_DAILY_AT（HH:MM）。"""
    text = (value or "").strip()
    match = _HHMM_RE.fullmatch(text)
    if match is None:
        raise ValueError("USAGE_PERIODIC_DAILY_AT 必须是 HH:MM（00:00-23:59）")
    return int(match.group(1)), int(match.group(2))


def _biz_zone(tz: ZoneInfo | str | None = None) -> ZoneInfo:
    if isinstance(tz, ZoneInfo):
        return tz
    name = (tz or getattr(SETTINGS, "bi_sync_biz_tz", None) or "Asia/Shanghai").strip()
    return ZoneInfo(name or "Asia/Shanghai")


def _daily_at_parts(daily_at: str | None = None) -> tuple[int, int]:
    raw = daily_at if daily_at is not None else getattr(
        SETTINGS, "usage_periodic_daily_at", "06:00"
    )
    return parse_usage_periodic_daily_at(str(raw))


def align_usage_periodic_at(
    now: datetime,
    *,
    daily_at: str | None = None,
    tz: ZoneInfo | str | None = None,
) -> datetime:
    """对齐到「当天」固定日常采集时刻（UTC）；用于首次调度。"""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now 必须是有时区 datetime")
    zone = _biz_zone(tz)
    hour, minute = _daily_at_parts(daily_at)
    local = now.astimezone(zone)
    return local.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    ).astimezone(timezone.utc)


def next_usage_periodic_at(
    now: datetime,
    *,
    daily_at: str | None = None,
    tz: ZoneInfo | str | None = None,
) -> datetime:
    """返回严格晚于 now 的下一个固定日常采集时刻（UTC）。"""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now 必须是有时区 datetime")
    zone = _biz_zone(tz)
    hour, minute = _daily_at_parts(daily_at)
    local = now.astimezone(zone)
    candidate = local.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if local >= candidate:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


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
            if self._next_periodic_at is None:
                # 首次对齐到当天固定点：未到则等待；已过则立刻补跑一轮。
                self._next_periodic_at = align_usage_periodic_at(current)
                log.info(
                    "用量日常采集已对齐固定时刻 next_at=%s daily_at=%s tz=%s",
                    self._next_periodic_at.isoformat(),
                    getattr(SETTINGS, "usage_periodic_daily_at", "06:00"),
                    getattr(SETTINGS, "bi_sync_biz_tz", "Asia/Shanghai"),
                )
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
        self._next_periodic_at = next_usage_periodic_at(now)
        log.info(
            "提交用量日常采集 next_at=%s",
            self._next_periodic_at.isoformat(),
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
