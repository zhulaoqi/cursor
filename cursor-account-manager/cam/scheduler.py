"""简易调度器（MVP）。"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .bi_sync import run_daily_sync
from .config import SETTINGS
from .logger import get

try:
    import fcntl  # POSIX only
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt  # Windows only
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

log = get("scheduler")
BJ_TZ = timezone(timedelta(hours=8))


def _parse_cron_hour_min(expr: str) -> tuple[int, int]:
    parts = (expr or "").split()
    if len(parts) < 2:
        return 30, 1
    minute = int(parts[0])
    hour = int(parts[1])
    minute = min(max(minute, 0), 59)
    hour = min(max(hour, 0), 23)
    return minute, hour


@contextmanager
def _try_lock(lock_file: str):
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o644)
    locked = False
    try:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            elif msvcrt is not None:
                # Lock first byte in non-blocking style on Windows.
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                locked = True
            else:
                # Fallback: no locking backend, allow execution.
                locked = True
        except OSError:
            locked = False
        yield locked
    finally:
        if locked:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                elif msvcrt is not None:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)


def run_scheduler_once_for_yesterday() -> dict:
    with _try_lock(SETTINGS.bi_sync_lock_file) as ok:
        if not ok:
            raise RuntimeError("已有同步任务在运行（锁已占用）")
        return run_daily_sync(trigger_type="scheduler")


def run_scheduler_loop(poll_interval_sec: int = 30) -> None:
    minute, hour = _parse_cron_hour_min(SETTINGS.bi_sync_cron)
    last_trigger_date = ""
    log.info(f"调度器启动：每天 {hour:02d}:{minute:02d} 执行")
    while True:
        now = datetime.now(BJ_TZ)
        date_key = now.strftime("%Y-%m-%d")
        if now.hour == hour and now.minute == minute and last_trigger_date != date_key:
            last_trigger_date = date_key
            try:
                with _try_lock(SETTINGS.bi_sync_lock_file) as ok:
                    if not ok:
                        log.warning("本次调度跳过：检测到已有任务在运行")
                        continue
                    result = run_daily_sync(trigger_type="scheduler")
                log.info(f"调度执行完成: {result}")
            except Exception as e:
                log.exception(f"调度执行失败: {type(e).__name__}: {e}")
        time.sleep(max(5, poll_interval_sec))

