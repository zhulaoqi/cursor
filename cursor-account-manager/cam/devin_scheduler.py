"""Devin 独立调度器：每天凌晨固定时刻（默认 02:30，业务时区）跑一轮采集并发飞书卡片。

与 Cursor 的 UsageSchedulerCoordinator 完全独立：自己的线程、自己的锁文件、自己的开关。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import SETTINGS
from .devin_config import devin_settings
from .devin_runner import run_devin_collection
from .devin_store import get_default_devin_store
from .logger import get

log = get("devin_scheduler")


def _hhmm_parts(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def _zone() -> ZoneInfo:
    return ZoneInfo(getattr(SETTINGS, "bi_sync_biz_tz", None) or "Asia/Shanghai")


def next_run_at(now: datetime, daily_at: str, tz: ZoneInfo | None = None) -> datetime:
    """严格晚于 now 的下一个每日触发时刻（UTC）。"""
    if now.tzinfo is None:
        raise ValueError("now 必须带时区")
    zone = tz or _zone()
    hour, minute = _hhmm_parts(daily_at)
    local = now.astimezone(zone)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local >= candidate:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


class DevinScheduler:
    def __init__(self, *, poll_interval_sec: int = 20):
        self._poll = max(1, poll_interval_sec)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_at: datetime | None = None
        self._worker: threading.Thread | None = None

    @property
    def next_at(self) -> datetime | None:
        return self._next_at

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        # 进程重启后清理遗留的 running 批次
        try:
            n = get_default_devin_store().fail_stale_runs()
            if n:
                log.warning(f"已把 {n} 个遗留 running 批次标记为 interrupted")
        except Exception:
            log.exception("清理遗留批次失败")
        self._thread = threading.Thread(target=self._loop, name="cam-devin-scheduler", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        cfg = devin_settings()
        self._next_at = next_run_at(datetime.now(timezone.utc), cfg.daily_at)
        log.info(f"Devin 每日采集已调度 next_at={self._next_at.isoformat()} daily_at={cfg.daily_at}")
        while not self._stop.is_set():
            try:
                self.tick(datetime.now(timezone.utc))
            except Exception:
                log.exception("Devin 调度轮询异常")
            self._stop.wait(self._poll)

    def tick(self, now: datetime) -> bool:
        """到点则异步触发一轮；返回是否触发。"""
        if self._next_at is None or now < self._next_at:
            return False
        if self._worker is not None and self._worker.is_alive():
            return False
        cfg = devin_settings()
        self._next_at = next_run_at(now, cfg.daily_at)
        log.info(f"触发 Devin 每日采集，下次 {self._next_at.isoformat()}")
        self._worker = threading.Thread(
            target=self._run_once, name="cam-devin-daily", daemon=True
        )
        self._worker.start()
        return True

    @staticmethod
    def _run_once() -> None:
        try:
            run_devin_collection(trigger="scheduler")
        except Exception:
            log.exception("Devin 每日采集异常")

    def stop(self, timeout_sec: float = 10) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0, timeout_sec))


_singleton: DevinScheduler | None = None
_singleton_lock = threading.Lock()


def start_devin_scheduler_once() -> DevinScheduler | None:
    global _singleton
    cfg = devin_settings()
    if not cfg.schedule_enable:
        log.info("Devin 调度未启用（DEVIN_SCHEDULE_ENABLE=false）")
        return None
    with _singleton_lock:
        if _singleton is None:
            _singleton = DevinScheduler()
        _singleton.start()
        return _singleton


def stop_devin_scheduler(timeout_sec: float = 10) -> None:
    global _singleton
    with _singleton_lock:
        sched, _singleton = _singleton, None
    if sched is not None:
        sched.stop(timeout_sec)


def get_devin_scheduler() -> DevinScheduler | None:
    return _singleton
