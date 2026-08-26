"""用量快照的独立调度器，不受旧 BI 调度长任务影响。"""

from __future__ import annotations

import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from .alerting import send_alert
from .config import SETTINGS
from .logger import get
from .usage_snapshot_refresh import run_usage_periodic, run_usage_pre_reset_due


log = get("usage_scheduler")
_singleton_lock = threading.Lock()
_singleton: UsageSchedulerCoordinator | None = None
_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _usage_periodic_alert_status(
    *,
    success: int,
    failed: int,
    lock_busy: int,
    circuit_blocked: int = 0,
) -> str:
    """根据日常采集汇总判定通知状态。"""
    if lock_busy and success == 0 and failed == 0:
        return "failed"
    if circuit_blocked and success == 0 and failed == 0:
        return "failed"
    if failed > 0 or circuit_blocked > 0:
        return "partial_failed"
    return "success"


def _send_usage_periodic_alert(
    *,
    scheduled_at: datetime,
    success: int,
    failed: int,
    skipped: int,
    lock_busy: int,
    circuit_blocked: int = 0,
) -> None:
    """日常采集结束后复用既有飞书告警通道发送结果。"""
    if not getattr(SETTINGS, "usage_periodic_alert_enable", True):
        return
    if not SETTINGS.alert_bot_enable:
        return
    status = _usage_periodic_alert_status(
        success=success,
        failed=failed,
        lock_busy=lock_busy,
        circuit_blocked=circuit_blocked,
    )
    zone = _biz_zone()
    date_key = scheduled_at.astimezone(zone).strftime("%Y-%m-%d")
    if status == "success":
        title = "用量日常采集完成"
        level = "success"
    elif status == "partial_failed":
        title = "用量日常采集部分失败"
        level = "warning"
    else:
        title = "用量日常采集失败"
        level = "error"
    reason = ""
    if lock_busy and success == 0 and failed == 0:
        reason = "未拿到全局锁，将稍后重试"
    elif circuit_blocked and success == 0 and failed == 0:
        reason = "认证熔断开启，本轮未采集账号"
    send_alert(
        title,
        (
            f"trigger_type=usage_periodic\n"
            f"date={date_key}\n"
            f"status={status}\n"
            f"account_success={success}\n"
            f"account_failed={failed}\n"
            f"account_skipped={skipped}\n"
            f"circuit_blocked={circuit_blocked}\n"
            f"lock_busy={lock_busy}"
            + (f"\nreason={reason}" if reason else "")
        ),
        level=level,
    )


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
        # 提交放在锁外，避免任务回调（也需写 next_at）与 tick 死锁。
        submit_pre_reset = False
        submit_periodic = False
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
                # 先占位，防止同轮重复提交；真正执行在锁外。
                self._next_pre_reset_at = current + timedelta(
                    minutes=SETTINGS.usage_pre_reset_scan_interval_min
                )
                submit_pre_reset = True
            if self._is_due(self._periodic_future, self._next_periodic_at, current):
                self._next_periodic_at = next_usage_periodic_at(current)
                submit_periodic = True
        if submit_pre_reset:
            self._submit_pre_reset(current)
        if submit_periodic:
            self._submit_periodic(current)

    @staticmethod
    def _is_due(
        future: Future[object] | None,
        due_at: datetime | None,
        now: datetime,
    ) -> bool:
        return (future is None or future.done()) and (due_at is None or now >= due_at)

    def _submit_periodic(self, now: datetime) -> None:
        log.info(
            "提交用量日常采集 scheduled_next_at=%s",
            (self._next_periodic_at or now).isoformat(),
        )
        future = self._submit(
            self._periodic_pool,
            "periodic",
            run_usage_periodic,
            now,
            on_result=lambda summary: self._on_periodic_result(summary, scheduled_at=now),
        )
        with self._lock:
            self._periodic_future = future

    def _submit_pre_reset(self, now: datetime) -> None:
        future = self._submit(
            self._pre_reset_pool,
            "pre-reset",
            run_usage_pre_reset_due,
            now,
            on_result=self._on_pre_reset_result,
        )
        with self._lock:
            self._pre_reset_future = future

    def _on_periodic_result(
        self,
        summary: object,
        *,
        scheduled_at: datetime,
    ) -> None:
        success = int(getattr(summary, "success", 0) or 0)
        failed = int(getattr(summary, "failed", 0) or 0)
        skipped = int(getattr(summary, "skipped", 0) or 0)
        lock_busy = int(getattr(summary, "lock_busy", 0) or 0)
        circuit_blocked = int(getattr(summary, "circuit_blocked", 0) or 0)
        log.info(
            "用量日常采集完成 success=%s failed=%s skipped=%s "
            "circuit_blocked=%s lock_busy=%s",
            success,
            failed,
            skipped,
            circuit_blocked,
            lock_busy,
        )
        try:
            _send_usage_periodic_alert(
                scheduled_at=scheduled_at,
                success=success,
                failed=failed,
                skipped=skipped,
                lock_busy=lock_busy,
                circuit_blocked=circuit_blocked,
            )
        except Exception:
            log.exception("用量日常采集结果通知发送失败")
        if success == 0 and failed == 0 and (lock_busy or circuit_blocked):
            retry_at = scheduled_at.astimezone(timezone.utc) + timedelta(minutes=15)
            with self._lock:
                self._next_periodic_at = retry_at
            reason = "未拿到全局锁" if lock_busy else "认证熔断未恢复"
            log.warning(
                "用量日常采集%s，将于 %s 重试",
                reason,
                retry_at.isoformat(),
            )

    def _on_pre_reset_result(self, summary: object) -> None:
        success = int(getattr(summary, "success", 0) or 0)
        failed = int(getattr(summary, "failed", 0) or 0)
        skipped = int(getattr(summary, "skipped", 0) or 0)
        lock_busy = int(getattr(summary, "lock_busy", 0) or 0)
        dry_run = len(getattr(summary, "dry_run_items", ()) or ())
        log.info(
            "用量 pre-reset 扫描完成 success=%s failed=%s skipped=%s "
            "lock_busy=%s dry_run=%s",
            success,
            failed,
            skipped,
            lock_busy,
            dry_run,
        )

    @staticmethod
    def _submit(
        pool: ThreadPoolExecutor,
        task_name: str,
        worker: Callable[..., object],
        now: datetime,
        *,
        on_result: Callable[[object], None] | None = None,
    ) -> Future[object]:
        future = pool.submit(worker, now=now)

        def report_result(done: Future[object]) -> None:
            try:
                summary = done.result()
            except Exception as exc:
                log.exception("用量 %s 任务异常", task_name)
                if task_name == "periodic":
                    try:
                        if (
                            getattr(SETTINGS, "usage_periodic_alert_enable", True)
                            and SETTINGS.alert_bot_enable
                        ):
                            zone = _biz_zone()
                            date_key = now.astimezone(zone).strftime("%Y-%m-%d")
                            send_alert(
                                "用量日常采集失败",
                                (
                                    f"trigger_type=usage_periodic\n"
                                    f"date={date_key}\n"
                                    f"status=failed\n"
                                    f"account_success=0\n"
                                    f"account_failed=0\n"
                                    f"account_skipped=0\n"
                                    f"lock_busy=0\n"
                                    f"error={type(exc).__name__}: {exc}"
                                ),
                                level="error",
                            )
                    except Exception:
                        log.exception("用量日常采集异常结果通知发送失败")
                return
            if on_result is not None:
                try:
                    on_result(summary)
                except Exception:
                    log.exception("用量 %s 结果回调异常", task_name)

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
