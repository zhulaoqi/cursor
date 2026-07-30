"""简易调度器（MVP）。"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .alerting import send_alert
from .bi_sync import run_daily_sync
from .billing_ledger_refresh import (
    BILLING_LEDGER_TRIGGER_TYPE,
    run_daily_billing_ledger_refresh_scheduled,
)
from .config import SETTINGS
from .logger import get
from .spending_refresh import run_daily_spending_refresh_scheduled
from .sync_log_store import get_default_sync_log_store
from .usage_scheduler import start_usage_scheduler_once

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
SPENDING_TRIGGER_TYPE = "spending_scheduler"


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
    lock_path = Path(lock_file)
    if not lock_path.is_absolute():
        from .config import PROJECT_ROOT

        lock_path = PROJECT_ROOT / lock_path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = str(lock_path)
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


def _run_billing_ledger_refresh_if_due(
    now: datetime,
    *,
    last_trigger_date: str,
    log_store,
) -> str:
    date_key = now.strftime("%Y-%m-%d")
    minute, hour = _parse_cron_hour_min(SETTINGS.billing_ledger_refresh_cron)
    due_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    try:
        already_recorded = log_store.has_run_for_trigger(
            biz_date=date_key,
            trigger_type=BILLING_LEDGER_TRIGGER_TYPE,
        )
    except Exception as e:
        log.warning(f"账期净支出调度记录检查失败，跳过本轮: {type(e).__name__}: {e}")
        return last_trigger_date

    if now < due_time or last_trigger_date == date_key or already_recorded:
        return last_trigger_date

    with _try_lock(SETTINGS.billing_ledger_refresh_lock_file) as ok:
        if not ok:
            log.warning("账期净支出调度跳过：检测到已有任务在运行")
            return date_key
        log.info(f"账期净支出调度到期，开始执行: trigger_date={date_key}")
        result = run_daily_billing_ledger_refresh_scheduled(
            trigger_type=BILLING_LEDGER_TRIGGER_TYPE,
            trigger_date=date_key,
        )
        log.info(f"账期净支出调度执行完成: {result}")
    return date_key


def run_scheduler_loop(
    poll_interval_sec: int = 30,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    """运行旧调度任务，并在启用时托管独立的用量调度器。"""
    stopper = stop_event or threading.Event()
    usage_coordinator = (
        start_usage_scheduler_once() if SETTINGS.usage_snapshot_enable else None
    )
    minute, hour = _parse_cron_hour_min(SETTINGS.bi_sync_cron)
    sp_minute, sp_hour = _parse_cron_hour_min(SETTINGS.spending_refresh_cron)
    last_trigger_date = ""
    last_spending_trigger_date = ""
    last_billing_ledger_trigger_date = ""
    parts: list[str] = []
    if SETTINGS.bi_sync_enable:
        parts.append(f"BI 每天 {hour:02d}:{minute:02d}")
    if SETTINGS.spending_refresh_enable:
        parts.append(
            f"套餐/按量刷新 {sp_hour:02d}:{sp_minute:02d}"
            + ("（完成后飞书通知）" if SETTINGS.spending_refresh_alert_enable else "")
        )
    if SETTINGS.billing_ledger_refresh_enable:
        ledger_minute, ledger_hour = _parse_cron_hour_min(SETTINGS.billing_ledger_refresh_cron)
        parts.append(f"账期净支出刷新 {ledger_hour:02d}:{ledger_minute:02d}")
    log.info("调度器启动：" + ("；".join(parts) if parts else "无任务"))

    while not stopper.is_set():
        now = datetime.now(BJ_TZ)
        date_key = now.strftime("%Y-%m-%d")
        log_store = get_default_sync_log_store()

        if SETTINGS.bi_sync_enable:
            due_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            biz_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            try:
                already_recorded = log_store.has_run_for_trigger(
                    biz_date=biz_date,
                    trigger_type="scheduler",
                )
            except Exception as e:
                log.warning(f"BI 调度记录检查失败，跳过本轮: {type(e).__name__}: {e}")
                already_recorded = True
            if now >= due_time and last_trigger_date != date_key and not already_recorded:
                last_trigger_date = date_key
                try:
                    with _try_lock(SETTINGS.bi_sync_lock_file) as ok:
                        if not ok:
                            msg = "本次调度跳过：检测到已有任务在运行"
                            log.warning(msg)
                            send_alert(
                                "BI 调度同步跳过",
                                f"date={date_key}\nreason=sync_lock_busy",
                                level="warning",
                            )
                        else:
                            log.info(
                                f"BI 调度到期，开始执行: trigger_date={date_key} biz_date={biz_date}"
                            )
                            result = run_daily_sync(trigger_type="scheduler")
                            log.info(f"BI 调度执行完成: {result}")
                except Exception as e:
                    log.exception(f"BI 调度执行失败: {type(e).__name__}: {e}")
                    send_alert(
                        "BI 调度同步失败",
                        f"date={date_key}\nerror={type(e).__name__}: {e}",
                        level="error",
                    )

        if SETTINGS.spending_refresh_enable:
            due_sp = now.replace(hour=sp_hour, minute=sp_minute, second=0, microsecond=0)
            try:
                sp_already = log_store.has_run_for_trigger(
                    biz_date=date_key,
                    trigger_type=SPENDING_TRIGGER_TYPE,
                )
            except Exception as e:
                log.warning(f"消费页调度记录检查失败，跳过本轮: {type(e).__name__}: {e}")
                sp_already = True
            if now >= due_sp and last_spending_trigger_date != date_key and not sp_already:
                last_spending_trigger_date = date_key
                try:
                    log.info(f"消费页调度到期，开始执行: trigger_date={date_key}")
                    result = run_daily_spending_refresh_scheduled(
                        trigger_type=SPENDING_TRIGGER_TYPE,
                        trigger_date=date_key,
                    )
                    log.info(f"消费页调度执行完成: {result}")
                except Exception as e:
                    log.exception(f"消费页调度执行失败: {type(e).__name__}: {e}")

        if SETTINGS.billing_ledger_refresh_enable:
            try:
                last_billing_ledger_trigger_date = _run_billing_ledger_refresh_if_due(
                    now,
                    last_trigger_date=last_billing_ledger_trigger_date,
                    log_store=log_store,
                )
            except Exception as e:
                log.exception(f"账期净支出调度执行失败: {type(e).__name__}: {e}")

        stopper.wait(max(5, poll_interval_sec))

    if usage_coordinator is not None:
        usage_coordinator.stop()
