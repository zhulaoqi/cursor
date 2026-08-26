"""用量快照 periodic 与 pre-reset 批次编排测试。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from cam.models import Account
from cam.usage_snapshot_models import (
    AccountMappingResult,
    AuthOutcome,
    CollectionResult,
    CollectionStatus,
    MonitoredAccount,
    SnapshotType,
    UsageSnapshot,
)
from cam.usage_snapshot_refresh import (
    periodic_slot,
    pre_reset_target,
    run_usage_manual_collect,
    run_usage_periodic,
    run_usage_pre_reset_due,
)


UTC = timezone.utc
SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 29, 4, 45, tzinfo=UTC)


def monitored(email: str) -> MonitoredAccount:
    """构造可采集账号。"""
    return MonitoredAccount(
        account=Account(email, "secret", "imap.example.com", 993),
        applicant="申请人",
        department="研发",
    )


def snapshot(
    email: str,
    snapshot_type: SnapshotType,
    slot: datetime,
    *,
    cycle_end: datetime = datetime(2026, 8, 1, tzinfo=UTC),
) -> UsageSnapshot:
    """构造最小成功快照。"""
    return UsageSnapshot(
        email=email,
        plan_tier="pro",
        plan_tier_raw="Pro",
        plan_status="active",
        plan_source="api",
        billing_cycle_start=datetime(2026, 7, 1, tzinfo=UTC),
        billing_cycle_end=cycle_end,
        total_used_pct=Decimal("20"),
        snapshot_type=snapshot_type,
        snapshot_slot=slot,
        collected_at=NOW,
        source_endpoint="/usage",
        parser_version="test",
        raw_payload={},
    )


class FakeResolver:
    """返回指定监控账号。"""

    def __init__(self, accounts: tuple[MonitoredAccount, ...]) -> None:
        self.accounts = accounts

    def resolve(self) -> AccountMappingResult:
        return AccountMappingResult(collectable_accounts=self.accounts)


class FakeStore:
    """记录只读查询与协调写入。"""

    def __init__(self) -> None:
        self.periodic_slots: set[tuple[str, datetime]] = set()
        self.latest_cycles: dict[str, dict] = {}
        self.pre_reset_slots: set[tuple[str, datetime]] = set()
        self.writes: list[UsageSnapshot] = []

    def has_periodic_slot(self, email: str, slot: datetime) -> bool:
        return (email, slot) in self.periodic_slots

    def get_latest_cycle(self, email: str):
        return self.latest_cycles.get(email)

    def has_pre_reset_slot(self, email: str, cycle_start: datetime) -> bool:
        return (email, cycle_start) in self.pre_reset_slots

    def reconcile_and_write(self, value: UsageSnapshot):
        self.writes.append(value)


class FakeCollector:
    """按邮箱返回预设采集结果。"""

    def __init__(self, results: dict[str, CollectionResult]) -> None:
        self.results = results
        self.calls: list[str] = []

    def collect(self, account, **kwargs) -> CollectionResult:
        self.calls.append(account.email)
        return self.results[account.email]


class FakeSyncLog:
    """记录账号槽状态并提供可控重试历史。"""

    def __init__(self, attempts=None) -> None:
        self.attempts = attempts or {}
        self.logs: list[dict] = []

    def get_account_attempt_state(self, *, account_email, trigger_type):
        return self.attempts.get(
            (account_email, trigger_type),
            type("State", (), {"attempts": 0, "last_failed_at": None, "succeeded": False})(),
        )

    def add_account_log(self, **kwargs) -> None:
        self.logs.append(kwargs)


class ClosedBreaker:
    """始终关闭的最小熔断器。"""

    def __init__(self) -> None:
        self.outcomes = []

    def snapshot(self):
        return type("Snapshot", (), {"state": "closed", "retry_at": None})()

    def record(self, outcome, email, now=None) -> None:
        self.outcomes.append((outcome, email))

    def allow_refresh_or_login(self) -> bool:
        return True

    def allows_new_submission(self, now=None) -> bool:
        return True


class OpeningBreaker(ClosedBreaker):
    """首个认证失败后切换为开启。"""

    def snapshot(self):
        state = "open" if self.outcomes else "closed"
        return type("Snapshot", (), {"state": state, "retry_at": None})()

    def allows_new_submission(self, now=None) -> bool:
        return self.snapshot().state != "open"


class AlwaysOpenBreaker(ClosedBreaker):
    """始终开启且仍在冷却，模拟日常采集被静默跳过的故障。"""

    def snapshot(self):
        return type(
            "Snapshot",
            (),
            {
                "state": "open",
                "retry_at": NOW + timedelta(hours=1),
            },
        )()

    def allows_new_submission(self, now=None) -> bool:
        return False


class CooldownExpiredOpenBreaker(ClosedBreaker):
    """开启但冷却已过：批次应继续提交以便探针恢复。"""

    def __init__(self) -> None:
        super().__init__()
        self._half_open = False

    def snapshot(self):
        state = "half_open" if self._half_open else "open"
        return type(
            "Snapshot",
            (),
            {"state": state, "retry_at": NOW - timedelta(seconds=1)},
        )()

    def allows_new_submission(self, now=None) -> bool:
        return True

    def allow_refresh_or_login(self) -> bool:
        self._half_open = True
        return True


@contextmanager
def unlocked(*args, **kwargs):
    """测试用始终可用锁。"""
    yield True


@contextmanager
def busy(*args, **kwargs):
    """测试用忙碌锁。"""
    yield False


class UsageSnapshotRefreshTests(unittest.TestCase):
    """验证槽计算和批次最小编排约束。"""

    def test_periodic_slot_按上海时区对齐(self):
        now = datetime(2026, 7, 29, 7, 59, tzinfo=UTC)
        self.assertEqual(
            periodic_slot(now, 6, SHANGHAI),
            datetime(2026, 7, 29, 4, tzinfo=UTC),
        )

    def test_pre_reset_target_从账期结束时间倒推(self):
        cycle_end = datetime(2026, 8, 1, tzinfo=UTC)
        self.assertEqual(
            pre_reset_target(cycle_end, 180),
            datetime(2026, 7, 31, 21, tzinfo=UTC),
        )

    def test_periodic_已有槽位跳过采集(self):
        account = monitored("a@example.com")
        store = FakeStore()
        slot = periodic_slot(NOW, 24, SHANGHAI)
        store.periodic_slots.add((account.account.email, slot))
        collector = FakeCollector({})

        summary = run_usage_periodic(
            now=NOW,
            resolver=FakeResolver((account,)),
            store=store,
            collector=collector,
            sync_log=FakeSyncLog(),
            breaker=ClosedBreaker(),
        )

        self.assertEqual(summary.skipped, 1)
        self.assertEqual(collector.calls, [])

    def test_pre_reset_dry_run_不获取令牌或写库(self):
        account = monitored("a@example.com")
        store = FakeStore()
        store.latest_cycles[account.account.email] = {
            "billing_cycle_start": datetime(2026, 7, 1, tzinfo=UTC),
            "billing_cycle_end": NOW + timedelta(minutes=120),
        }
        collector = FakeCollector({})

        due = run_usage_pre_reset_due(
            now=NOW,
            dry_run=True,
            resolver=FakeResolver((account,)),
            store=store,
            collector=collector,
            sync_log=FakeSyncLog(),
            breaker=ClosedBreaker(),
        )

        self.assertEqual(len(due.dry_run_items), 1)
        self.assertEqual(collector.calls, [])
        self.assertEqual(store.writes, [])

    def test_pre_reset_仅在目标和安全窗口之间执行(self):
        account = monitored("a@example.com")
        store = FakeStore()
        cycle_end = NOW + timedelta(minutes=120)
        store.latest_cycles[account.account.email] = {
            "billing_cycle_start": datetime(2026, 7, 1, tzinfo=UTC),
            "billing_cycle_end": cycle_end,
        }
        collector = FakeCollector(
            {
                account.account.email: CollectionResult(
                    email=account.account.email,
                    status=CollectionStatus.SUCCESS,
                    snapshot=snapshot(
                        account.account.email,
                        SnapshotType.PRE_RESET,
                        datetime(2026, 7, 1, tzinfo=UTC),
                        cycle_end=cycle_end,
                    ),
                    auth_outcome=AuthOutcome.SUCCESS,
                )
            }
        )

        summary = run_usage_pre_reset_due(
            now=NOW,
            resolver=FakeResolver((account,)),
            store=store,
            collector=collector,
            sync_log=FakeSyncLog(),
            breaker=ClosedBreaker(),
        )

        self.assertEqual(summary.success, 1)
        self.assertEqual(len(store.writes), 1)

    def test_账号锁忙碌时不采集(self):
        account = monitored("a@example.com")
        collector = FakeCollector({})
        with patch("cam.usage_snapshot_refresh.usage_account_lock", busy):
            summary = run_usage_periodic(
                now=NOW,
                resolver=FakeResolver((account,)),
                store=FakeStore(),
                collector=collector,
                sync_log=FakeSyncLog(),
                breaker=ClosedBreaker(),
            )

        self.assertEqual(summary.lock_busy, 1)
        self.assertEqual(collector.calls, [])

    def test_单账号失败不阻断其他账号(self):
        failed, succeeded = monitored("a@example.com"), monitored("b@example.com")
        slot = periodic_slot(NOW, 24, SHANGHAI)
        collector = FakeCollector(
            {
                failed.account.email: CollectionResult(
                    email=failed.account.email,
                    status=CollectionStatus.FAILED,
                    error_message="网络错误",
                    auth_outcome=AuthOutcome.NON_AUTH_FAILURE,
                ),
                succeeded.account.email: CollectionResult(
                    email=succeeded.account.email,
                    status=CollectionStatus.SUCCESS,
                    snapshot=snapshot(succeeded.account.email, SnapshotType.PERIODIC, slot),
                    auth_outcome=AuthOutcome.SUCCESS,
                ),
            }
        )
        summary = run_usage_periodic(
            now=NOW,
            resolver=FakeResolver((failed, succeeded)),
            store=FakeStore(),
            collector=collector,
            sync_log=FakeSyncLog(),
            breaker=ClosedBreaker(),
        )

        self.assertEqual((summary.failed, summary.success), (1, 1))
        self.assertCountEqual(
            collector.calls,
            ["a@example.com", "b@example.com"],
        )

    def test_熔断开启后停止提交后续账号(self):
        first, second = monitored("a@example.com"), monitored("b@example.com")
        collector = FakeCollector(
            {
                first.account.email: CollectionResult(
                    email=first.account.email,
                    status=CollectionStatus.FAILED,
                    auth_outcome=AuthOutcome.AUTH_FAILURE,
                ),
                second.account.email: CollectionResult(
                    email=second.account.email,
                    status=CollectionStatus.FAILED,
                    auth_outcome=AuthOutcome.AUTH_FAILURE,
                ),
            }
        )
        summary = run_usage_periodic(
            now=NOW,
            resolver=FakeResolver((first, second)),
            store=FakeStore(),
            collector=collector,
            sync_log=FakeSyncLog(),
            breaker=OpeningBreaker(),
            concurrency=1,
        )

        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.circuit_blocked, 1)
        self.assertEqual(collector.calls, ["a@example.com"])

    def test_熔断冷却中开跑不得静默全零(self):
        """回归：熔断 open 时日常采集曾直接返回全 0 并误报成功。"""
        first, second = monitored("a@example.com"), monitored("b@example.com")
        collector = FakeCollector({})
        summary = run_usage_periodic(
            now=NOW,
            resolver=FakeResolver((first, second)),
            store=FakeStore(),
            collector=collector,
            sync_log=FakeSyncLog(),
            breaker=AlwaysOpenBreaker(),
            concurrency=2,
        )

        self.assertEqual(summary.success, 0)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(summary.circuit_blocked, 2)
        self.assertEqual(collector.calls, [])

    def test_熔断冷却结束后日常采集应提交探针账号(self):
        """冷却到期后不得因 snapshot.state=open 永久卡死提交。"""
        account = monitored("a@example.com")
        slot = periodic_slot(NOW, 24, SHANGHAI)
        collector = FakeCollector(
            {
                account.account.email: CollectionResult(
                    email=account.account.email,
                    status=CollectionStatus.SUCCESS,
                    snapshot=snapshot(
                        account.account.email, SnapshotType.PERIODIC, slot
                    ),
                    auth_outcome=AuthOutcome.SUCCESS,
                )
            }
        )
        summary = run_usage_periodic(
            now=NOW,
            resolver=FakeResolver((account,)),
            store=FakeStore(),
            collector=collector,
            sync_log=FakeSyncLog(),
            breaker=CooldownExpiredOpenBreaker(),
            concurrency=1,
        )

        self.assertEqual(summary.success, 1)
        self.assertEqual(summary.circuit_blocked, 0)
        self.assertEqual(collector.calls, [account.account.email])

    def test_同槽超过重试限额跳过(self):
        account = monitored("a@example.com")
        slot = periodic_slot(NOW, 24, SHANGHAI)
        from cam.usage_snapshot_refresh import usage_periodic_trigger_type

        trigger = usage_periodic_trigger_type(slot)
        state = type(
            "State",
            (),
            {"attempts": 3, "last_failed_at": 0, "succeeded": False},
        )()
        collector = FakeCollector({})
        summary = run_usage_periodic(
            now=NOW,
            resolver=FakeResolver((account,)),
            store=FakeStore(),
            collector=collector,
            sync_log=FakeSyncLog({(account.account.email, trigger): state}),
            breaker=ClosedBreaker(),
            max_attempts=3,
        )

        self.assertEqual(summary.skipped, 1)
        self.assertEqual(collector.calls, [])

    def test_manual_collect_forces_even_when_slot_exists(self):
        """行内强制采集忽略已有 periodic 槽位。"""
        account = monitored("a@example.com")
        store = FakeStore()
        slot = periodic_slot(NOW, 24, SHANGHAI)
        store.periodic_slots.add((account.account.email, slot))
        collector = FakeCollector(
            {
                account.account.email: CollectionResult(
                    email=account.account.email,
                    status=CollectionStatus.SUCCESS,
                    snapshot=snapshot(
                        account.account.email, SnapshotType.PERIODIC, slot
                    ),
                    auth_outcome=AuthOutcome.SUCCESS,
                )
            }
        )

        with patch("cam.usage_snapshot_refresh.usage_account_lock", unlocked):
            result = run_usage_manual_collect(
                account.account.email,
                now=NOW,
                resolver=FakeResolver((account,)),
                store=store,
                collector=collector,
                breaker=ClosedBreaker(),
            )

        self.assertEqual(result.status, CollectionStatus.SUCCESS)
        self.assertEqual(collector.calls, [account.account.email])
        self.assertEqual(len(store.writes), 1)

    def test_manual_collect_returns_not_collectable_for_unknown_email(self):
        """不在可采集交集中的邮箱不得调用采集器。"""
        collector = FakeCollector({})
        result = run_usage_manual_collect(
            "missing@example.com",
            now=NOW,
            resolver=FakeResolver(()),
            store=FakeStore(),
            collector=collector,
            breaker=ClosedBreaker(),
        )
        self.assertEqual(result.status, CollectionStatus.NOT_COLLECTABLE)
        self.assertEqual(collector.calls, [])

    def test_manual_collect_lock_busy(self):
        """账号锁忙碌时返回 LOCK_BUSY。"""
        account = monitored("a@example.com")
        collector = FakeCollector({})
        with patch("cam.usage_snapshot_refresh.usage_account_lock", busy):
            result = run_usage_manual_collect(
                account.account.email,
                now=NOW,
                resolver=FakeResolver((account,)),
                store=FakeStore(),
                collector=collector,
                breaker=ClosedBreaker(),
            )
        self.assertEqual(result.status, CollectionStatus.LOCK_BUSY)
        self.assertEqual(collector.calls, [])

    def test_manual_collect_failed_does_not_write(self):
        """采集失败不写库。"""
        account = monitored("a@example.com")
        store = FakeStore()
        collector = FakeCollector(
            {
                account.account.email: CollectionResult(
                    email=account.account.email,
                    status=CollectionStatus.FAILED,
                    error_message="网络错误",
                    auth_outcome=AuthOutcome.NON_AUTH_FAILURE,
                )
            }
        )
        with patch("cam.usage_snapshot_refresh.usage_account_lock", unlocked):
            result = run_usage_manual_collect(
                account.account.email,
                now=NOW,
                resolver=FakeResolver((account,)),
                store=store,
                collector=collector,
                breaker=ClosedBreaker(),
            )
        self.assertEqual(result.status, CollectionStatus.FAILED)
        self.assertEqual(store.writes, [])
