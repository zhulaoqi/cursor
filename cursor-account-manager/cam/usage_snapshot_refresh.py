"""Cursor 用量快照账号解析与触发键工具。"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import time
from typing import TYPE_CHECKING, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from .config import SETTINGS
from .models import Account
from .sync_log_store import SyncLogStore, get_default_sync_log_store
from .token_manager import get_default_manager
from .token_store import get_default_store
from .usage_auth_breaker import UsageAuthBreaker
from .usage_snapshot_collector import UsageSnapshotCollector
from .usage_snapshot_locks import usage_account_lock, usage_task_lock
from .usage_snapshot_models import (
    AccountMappingResult,
    AuthOutcome,
    CollectionResult,
    CollectionStatus,
    MonitoredAccount,
    SnapshotType,
)

if TYPE_CHECKING:
    from .token_store import TokenStore
    from .usage_snapshot_store import UsageSnapshotStore

def _usage_trigger_type(prefix: str, value: datetime) -> str:
    """将有时区时间转换为稳定的 UTC 触发键。"""
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("触发时间必须是有时区 datetime")
    utc_value = value.astimezone(timezone.utc)
    return f"{prefix}:{utc_value.strftime('%Y%m%dT%H%M%S.%fZ')}"


def usage_periodic_trigger_type(slot: datetime) -> str:
    """生成 periodic 时间槽的 UTC 触发键。"""
    return _usage_trigger_type("usage_periodic", slot)


def usage_pre_reset_trigger_type(cycle_start: datetime) -> str:
    """生成 pre-reset 账期起点的 UTC 触发键。"""
    return _usage_trigger_type("usage_pre_reset", cycle_start)


def _normalized_email(value: object, source: str) -> str:
    """规范化数据源邮箱，并拒绝空值。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source} 存在空 email")
    return value.strip().lower()


def _index_account_rows(
    rows: list[dict],
    *,
    source: str,
) -> dict[str, dict]:
    """按规范化邮箱建立索引，并拒绝规范化后的重复账号。"""
    indexed: dict[str, dict] = {}
    for row in rows:
        email = _normalized_email(row.get("email"), source)
        if email in indexed:
            raise ValueError(f"{source} 规范化 email 存在重复：{email}")
        indexed[email] = row
    return indexed


def _normalized_imap_port(value: object) -> int:
    """校验并转换本地 IMAP 端口；仅缺失值使用配置默认值。"""
    if value is None:
        return SETTINGS.default_imap_port
    if isinstance(value, bool):
        raise ValueError("imap_port 必须是 1~65535 的整数或纯十进制字符串")
    if isinstance(value, int):
        port = value
    elif (
        isinstance(value, str)
        and value
        and value.isascii()
        and value.isdecimal()
    ):
        port = int(value)
    else:
        raise ValueError("imap_port 必须是 1~65535 的整数或纯十进制字符串")
    if not 1 <= port <= 65535:
        raise ValueError("imap_port 必须在 1~65535 范围内")
    return port


class AccountResolver:
    """合并 MySQL 监控主数据与 SQLite 本地账号。"""

    def __init__(
        self,
        usage_store: UsageSnapshotStore,
        token_store: TokenStore,
    ) -> None:
        self.usage_store = usage_store
        self.token_store = token_store

    def resolve(self) -> AccountMappingResult:
        """返回可采集交集及两端账号差集，不读写任何业务状态。"""
        mysql_accounts = _index_account_rows(
            self.usage_store.list_monitor_accounts(),
            source="MySQL",
        )
        local_accounts = _index_account_rows(
            self.token_store.list_accounts(),
            source="SQLite",
        )

        shared_emails = sorted(mysql_accounts.keys() & local_accounts.keys())
        collectable = tuple(
            self._to_monitored_account(
                email,
                mysql_accounts[email],
                local_accounts[email],
            )
            for email in shared_emails
        )
        return AccountMappingResult(
            collectable_accounts=collectable,
            not_collectable_emails=tuple(
                sorted(mysql_accounts.keys() - local_accounts.keys())
            ),
            orphan_local_emails=tuple(
                sorted(local_accounts.keys() - mysql_accounts.keys())
            ),
        )

    @staticmethod
    def _to_monitored_account(
        email: str,
        mysql_row: dict,
        local_row: dict,
    ) -> MonitoredAccount:
        """用本地凭据和 MySQL 人员信息构造监控账号。"""
        imap_host = local_row.get("imap_host") or SETTINGS.default_imap_host
        imap_port = _normalized_imap_port(local_row.get("imap_port"))
        return MonitoredAccount(
            account=Account(
                email=email,
                imap_password=local_row.get("imap_password") or "",
                imap_host=imap_host,
                imap_port=imap_port,
                feishu_email=local_row.get("feishu_email") or "",
            ),
            applicant=(
                ""
                if mysql_row.get("applicant") is None
                else mysql_row.get("applicant")
            ),
            department=(
                ""
                if mysql_row.get("department") is None
                else mysql_row.get("department")
            ),
        )


_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class UsageRunSummary:
    """一次批次的精简汇总，dry-run 项仅用于 pre-reset 预览。"""

    success: int = 0
    failed: int = 0
    skipped: int = 0
    lock_busy: int = 0
    circuit_blocked: int = 0
    dry_run_items: tuple[dict, ...] = field(default_factory=tuple)


@dataclass
class UsageRuntime:
    """periodic 与 pre-reset 共用的进程内依赖。"""

    breaker: UsageAuthBreaker
    collector: UsageSnapshotCollector
    store: UsageSnapshotStore


_default_runtime: UsageRuntime | None = None


def get_usage_runtime() -> UsageRuntime:
    """创建进程内单例，保证两类任务共享认证熔断器。"""
    global _default_runtime
    if _default_runtime is None:
        from .usage_snapshot_store import UsageSnapshotStore

        _default_runtime = UsageRuntime(
            breaker=UsageAuthBreaker(
                min_samples=SETTINGS.usage_auth_breaker_min_samples,
                failure_ratio=SETTINGS.usage_auth_breaker_failure_ratio,
                cooldown=timedelta(
                    minutes=SETTINGS.usage_auth_breaker_cooldown_min
                ),
                window_size=SETTINGS.usage_auth_breaker_window_size,
                window_duration=timedelta(
                    minutes=SETTINGS.usage_auth_breaker_window_min
                ),
            ),
            collector=UsageSnapshotCollector(manager=get_default_manager()),
            store=UsageSnapshotStore(),
        )
    return _default_runtime


def periodic_slot(
    now: datetime,
    interval_hours: int,
    biz_tz: ZoneInfo = _SHANGHAI,
) -> datetime:
    """按业务时区的午夜锚点取当前 periodic 槽，并转换回 UTC。"""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now 必须是有时区 datetime")
    if not isinstance(interval_hours, int) or interval_hours <= 0:
        raise ValueError("interval_hours 必须是正整数")
    local = now.astimezone(biz_tz)
    slot_hour = local.hour - local.hour % interval_hours
    return local.replace(
        hour=slot_hour,
        minute=0,
        second=0,
        microsecond=0,
    ).astimezone(timezone.utc)


def pre_reset_target(cycle_end: datetime, target_offset_min: int) -> datetime:
    """从 UTC 账期结束时间倒推 pre-reset 目标时刻。"""
    if cycle_end.tzinfo is None or cycle_end.utcoffset() is None:
        raise ValueError("cycle_end 必须是有时区 datetime")
    if not isinstance(target_offset_min, int) or target_offset_min < 0:
        raise ValueError("target_offset_min 必须是非负整数")
    return cycle_end.astimezone(timezone.utc) - timedelta(
        minutes=target_offset_min
    )


def _utc_now(now: datetime | None) -> datetime:
    """返回传入或当前的 UTC 时间。"""
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now 必须是有时区 datetime")
    return value.astimezone(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """将数据库 UTC DATETIME 或有时区时间标准化为 UTC。"""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _cycle_due(cycle: dict | None, now: datetime) -> tuple[bool, datetime, datetime]:
    """判断最新账期是否进入目标窗口，并返回目标和安全截止点。"""
    if not cycle:
        return False, now, now
    cycle_end = _as_utc(cycle["billing_cycle_end"])
    target = pre_reset_target(
        cycle_end,
        SETTINGS.usage_pre_reset_target_offset_min,
    )
    closes = cycle_end - timedelta(
        minutes=SETTINGS.usage_pre_reset_window_end_min
    )
    return target <= now <= closes, target, closes


def _call_if_present(obj, method: str, **kwargs) -> None:
    """兼容轻量测试替身并调用可用的运行日志接口。"""
    callback = getattr(obj, method, None)
    if callback is not None:
        callback(**kwargs)


def _record_account_log(
    sync_log: SyncLogStore,
    *,
    run_id: str,
    email: str,
    status: str,
    started_at: int,
    error_message: str = "",
) -> None:
    """将单账号终态写入 SyncLogStore。"""
    ended_at = int(time.time())
    _call_if_present(
        sync_log,
        "add_account_log",
        run_id=run_id,
        account_email=email,
        account_source="usage_snapshot",
        is_new_account=False,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        fetch_rows=1 if status == "success" else 0,
        load_rows=1 if status == "success" else 0,
        error_message=error_message,
    )


def _quarantine_failure_limit() -> int:
    return max(1, int(getattr(SETTINGS, "usage_auth_quarantine_failures", 3) or 3))


def _is_quarantined_account(token_store: object | None, email: str) -> bool:
    """连续失败或 disabled 的账号保留历史，但不再污染全局熔断。"""
    getter = getattr(token_store, "get", None)
    if not callable(getter):
        return False
    record = getter(email)
    if record is None:
        return False
    if str(getattr(record, "status", "") or "").strip().lower() == "disabled":
        return True
    failures = int(getattr(record, "consecutive_failures", 0) or 0)
    return failures >= _quarantine_failure_limit()


def _record_breaker_outcome(
    breaker: UsageAuthBreaker,
    result: CollectionResult,
    email: str,
    *,
    now: datetime,
    token_store: object | None,
) -> None:
    """只把健康账号的系统性认证结果记入全局熔断。"""
    outcome = result.auth_outcome
    if outcome is AuthOutcome.SUCCESS:
        breaker.record(outcome, email, now=now)
        return
    if outcome is AuthOutcome.AUTH_FAILURE and not _is_quarantined_account(
        token_store, email
    ):
        breaker.record(outcome, email, now=now)


def _collect_status(result: CollectionResult) -> str:
    if result.status is CollectionStatus.SUCCESS:
        return "success"
    if result.status is CollectionStatus.AUTH_CIRCUIT_OPEN:
        return "circuit_blocked"
    return "failed"


def _prioritize_accounts(
    accounts: tuple[MonitoredAccount, ...],
    token_store: object | None,
) -> tuple[MonitoredAccount, ...]:
    """健康/有缓存的账号先采，隔离账号放后面。"""
    def sort_key(item: MonitoredAccount) -> tuple[int, int, str]:
        email = item.account.email
        if _is_quarantined_account(token_store, email):
            return (2, 0, email)
        return (0, 0, email)

    return tuple(sorted(accounts, key=sort_key))


def _run_bounded(
    accounts: tuple,
    *,
    concurrency: int,
    breaker: UsageAuthBreaker,
    worker: Callable[[object], str],
) -> UsageRunSummary:
    """有界并发提交全部账号。熔断不再中断当天队列。"""
    counts = {
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "lock_busy": 0,
        "circuit_blocked": 0,
    }
    iterator = iter(accounts)
    futures: set[Future[str]] = set()
    submitted = 0

    def submit_next(executor: ThreadPoolExecutor) -> bool:
        nonlocal submitted
        try:
            account = next(iterator)
        except StopIteration:
            return False
        futures.add(executor.submit(worker, account))
        submitted += 1
        return True

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        while len(futures) < max(1, concurrency) and submit_next(executor):
            pass
        while futures:
            done, futures = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                status = future.result()
                counts[status] = counts.get(status, 0) + 1
            while len(futures) < max(1, concurrency) and submit_next(executor):
                pass
    remaining = len(accounts) - submitted
    if remaining > 0:
        counts["circuit_blocked"] = remaining
    return UsageRunSummary(**counts)


def _make_dependencies(
    resolver: AccountResolver | None,
    store: UsageSnapshotStore | None,
    collector: UsageSnapshotCollector | None,
    breaker: UsageAuthBreaker | None,
    sync_log: SyncLogStore | None,
) -> tuple[AccountResolver, UsageSnapshotStore, UsageSnapshotCollector, UsageAuthBreaker, SyncLogStore]:
    """补齐可注入依赖的默认实现。"""
    runtime = get_usage_runtime() if any(
        value is None for value in (store, collector, breaker)
    ) else None
    actual_store = store or runtime.store
    return (
        resolver or AccountResolver(actual_store, get_default_store()),
        actual_store,
        collector or runtime.collector,
        breaker or runtime.breaker,
        sync_log or get_default_sync_log_store(),
    )


def run_usage_periodic(
    *,
    now: datetime | None = None,
    emails: tuple[str, ...] | None = None,
    resolver: AccountResolver | None = None,
    store: UsageSnapshotStore | None = None,
    collector: UsageSnapshotCollector | None = None,
    breaker: UsageAuthBreaker | None = None,
    sync_log: SyncLogStore | None = None,
    concurrency: int | None = None,
    max_attempts: int | None = None,
) -> UsageRunSummary:
    """执行当前上海时区 periodic 槽的有界账号采集。"""
    current = _utc_now(now)
    slot = periodic_slot(current, SETTINGS.usage_periodic_interval_hours)
    trigger = usage_periodic_trigger_type(slot)
    resolver, store, collector, breaker, sync_log = _make_dependencies(
        resolver, store, collector, breaker, sync_log
    )
    accounts = resolver.resolve().collectable_accounts
    if emails is not None:
        wanted = {email.strip().lower() for email in emails}
        accounts = tuple(
            item for item in accounts if item.account.email in wanted
        )
    token_store = getattr(resolver, "token_store", None)
    accounts = _prioritize_accounts(accounts, token_store)
    run_id = f"{trigger}:{uuid4().hex}"
    _call_if_present(
        sync_log, "create_run",
        run_id=run_id, biz_date=current.date().isoformat(),
        trigger_type=trigger, account_total=len(accounts),
        account_snapshot_total=len(accounts), new_account_count=0,
    )

    def worker(item: MonitoredAccount) -> str:
        email = item.account.email
        state = sync_log.get_account_attempt_state(
            account_email=email, trigger_type=trigger
        )
        limit = max_attempts or SETTINGS.usage_periodic_max_attempts_per_slot
        if (
            store.has_periodic_slot(email, slot)
            or state.succeeded
            or state.attempts >= limit
        ):
            return "skipped"
        started_at = int(time.time())
        with usage_account_lock(
            email, SETTINGS.usage_account_lock_dir,
            SETTINGS.usage_account_lock_timeout_sec,
        ) as locked:
            if not locked:
                return "lock_busy"
            if store.has_periodic_slot(email, slot):
                return "skipped"
            result = collector.collect(
                item.account, snapshot_type=SnapshotType.PERIODIC,
                snapshot_slot=slot, auth_policy=breaker, collected_at=current,
            )
            _record_breaker_outcome(
                breaker, result, email, now=current, token_store=token_store,
            )
            status = _collect_status(result)
            if status == "success":
                store.reconcile_and_write(result.snapshot)
                _record_account_log(
                    sync_log, run_id=run_id, email=email, status="success",
                    started_at=started_at,
                )
                return "success"
            if status == "circuit_blocked":
                return "circuit_blocked"
            _record_account_log(
                sync_log, run_id=run_id, email=email, status="failed",
                started_at=started_at, error_message=result.error_message,
            )
            return "failed"

    with usage_task_lock(SETTINGS.usage_periodic_lock_file) as locked:
        if not locked:
            return UsageRunSummary(lock_busy=1)
        summary = _run_bounded(
            accounts, concurrency=concurrency or SETTINGS.usage_snapshot_concurrency,
            breaker=breaker, worker=worker,
        )
    if summary.failed or summary.circuit_blocked:
        run_status = "partial_failed" if summary.success else "failed"
    else:
        run_status = "success"
    _call_if_present(
        sync_log, "finish_run",
        run_id=run_id, status=run_status,
        account_success=summary.success, account_failed=summary.failed,
        event_total=summary.success + summary.failed, ods_rows=summary.success,
    )
    return summary


def run_usage_pre_reset_due(
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    resolver: AccountResolver | None = None,
    store: UsageSnapshotStore | None = None,
    collector: UsageSnapshotCollector | None = None,
    breaker: UsageAuthBreaker | None = None,
    sync_log: SyncLogStore | None = None,
    concurrency: int | None = None,
) -> UsageRunSummary:
    """扫描最新账期，在 pre-reset 目标窗口内采集最终快照。"""
    current = _utc_now(now)
    resolver, store, collector, breaker, sync_log = _make_dependencies(
        resolver, store, collector, breaker, sync_log
    )
    due: list[tuple[MonitoredAccount, dict, datetime, datetime]] = []
    for item in resolver.resolve().collectable_accounts:
        cycle = store.get_latest_cycle(item.account.email)
        is_due, target, closes = _cycle_due(cycle, current)
        if is_due:
            due.append((item, cycle, target, closes))
    if dry_run:
        return UsageRunSummary(
            dry_run_items=tuple(
                {
                    "email": item.account.email,
                    "cycle_start": cycle["billing_cycle_start"],
                    "cycle_end": cycle["billing_cycle_end"],
                    "target_at": target,
                    "window_closes_at": closes,
                    "reason": "进入 pre-reset 目标窗口",
                }
                for item, cycle, target, closes in due
            )
        )

    run_id = f"usage_pre_reset_due:{uuid4().hex}"
    _call_if_present(
        sync_log, "create_run",
        run_id=run_id, biz_date=current.date().isoformat(),
        trigger_type="usage_pre_reset_due", account_total=len(due),
        account_snapshot_total=len(due), new_account_count=0,
    )

    def worker(entry: tuple[MonitoredAccount, dict, datetime, datetime]) -> str:
        item, cycle, _, _ = entry
        email = item.account.email
        cycle_start = _as_utc(cycle["billing_cycle_start"])
        if store.has_pre_reset_slot(email, cycle_start):
            return "skipped"
        with usage_account_lock(
            email, SETTINGS.usage_account_lock_dir,
            SETTINGS.usage_account_lock_timeout_sec,
        ) as locked:
            if not locked:
                return "lock_busy"
            refreshed = store.get_latest_cycle(email)
            is_due, _, _ = _cycle_due(refreshed, current)
            if (
                not is_due
                or store.has_pre_reset_slot(email, cycle_start)
            ):
                return "skipped"
            started_at = int(time.time())
            result = collector.collect(
                item.account, snapshot_type=SnapshotType.PRE_RESET,
                snapshot_slot=cycle_start, auth_policy=breaker,
                collected_at=current,
            )
            _record_breaker_outcome(
                breaker, result, email, now=current,
                token_store=getattr(resolver, "token_store", None),
            )
            status = _collect_status(result)
            if status == "success":
                store.reconcile_and_write(result.snapshot)
                _record_account_log(
                    sync_log, run_id=run_id,
                    email=email, status="success", started_at=started_at,
                )
                return "success"
            if status == "circuit_blocked":
                return "circuit_blocked"
            _record_account_log(
                sync_log, run_id=run_id, email=email, status="failed",
                started_at=started_at, error_message=result.error_message,
            )
            return "failed"

    accounts = tuple(due)
    with usage_task_lock(SETTINGS.usage_pre_reset_lock_file) as locked:
        if not locked:
            return UsageRunSummary(lock_busy=1)
        summary = _run_bounded(
            accounts, concurrency=concurrency or SETTINGS.usage_snapshot_concurrency,
            breaker=breaker, worker=worker,
        )
    _call_if_present(
        sync_log, "finish_run",
        run_id=run_id, status="success" if not summary.failed else "partial_failed",
        account_success=summary.success, account_failed=summary.failed,
        event_total=summary.success + summary.failed, ods_rows=summary.success,
    )
    return summary


def run_usage_manual_collect(
    email: str,
    *,
    now: datetime | None = None,
    resolver: AccountResolver | None = None,
    store: UsageSnapshotStore | None = None,
    collector: UsageSnapshotCollector | None = None,
    breaker: UsageAuthBreaker | None = None,
) -> CollectionResult:
    """强制采集单个账号当前用量（忽略已有 periodic 槽位，不占全局任务锁）。"""
    normalized = _normalized_email(email, "手动采集")
    current = _utc_now(now)
    slot = periodic_slot(current, SETTINGS.usage_periodic_interval_hours)
    resolver, store, collector, breaker, _sync_log = _make_dependencies(
        resolver, store, collector, breaker, None,
    )
    mapping = resolver.resolve()
    target = next(
        (
            item
            for item in mapping.collectable_accounts
            if item.account.email == normalized
        ),
        None,
    )
    if target is None:
        return CollectionResult(
            email=normalized,
            status=CollectionStatus.NOT_COLLECTABLE,
            error_message="账号不存在或无法采集",
        )

    with usage_account_lock(
        normalized,
        SETTINGS.usage_account_lock_dir,
        SETTINGS.usage_account_lock_timeout_sec,
    ) as locked:
        if not locked:
            return CollectionResult(
                email=normalized,
                status=CollectionStatus.LOCK_BUSY,
                error_message="该账号正在采集，请稍后",
            )
        result = collector.collect(
            target.account,
            snapshot_type=SnapshotType.PERIODIC,
            snapshot_slot=slot,
            auth_policy=breaker,
            collected_at=current,
        )
        _record_breaker_outcome(
            breaker, result, normalized, now=current,
            token_store=getattr(resolver, "token_store", None),
        )
        if result.status is CollectionStatus.SUCCESS:
            store.reconcile_and_write(result.snapshot)
        return result
