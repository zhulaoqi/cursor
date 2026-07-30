"""独立用量调度器的线程与生命周期测试。"""

from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from cam import scheduler
from cam.usage_scheduler import (
    UsageSchedulerCoordinator,
    next_usage_periodic_at,
)


UTC = timezone.utc
SHANGHAI = ZoneInfo("Asia/Shanghai")


class UsagePeriodicDailyAtTests(unittest.TestCase):
    def test_next_periodic_before_daily_at_is_today(self) -> None:
        # 北京时间 2026-07-29 05:00 → 今天 06:00
        now = datetime(2026, 7, 28, 21, 0, tzinfo=UTC)
        due = next_usage_periodic_at(now, daily_at="06:00", tz=SHANGHAI)
        self.assertEqual(due.astimezone(SHANGHAI), datetime(2026, 7, 29, 6, 0, tzinfo=SHANGHAI))

    def test_next_periodic_after_daily_at_is_tomorrow(self) -> None:
        # 北京时间 2026-07-29 10:00 → 明天 06:00
        now = datetime(2026, 7, 29, 2, 0, tzinfo=UTC)
        due = next_usage_periodic_at(now, daily_at="06:00", tz=SHANGHAI)
        self.assertEqual(due.astimezone(SHANGHAI), datetime(2026, 7, 30, 6, 0, tzinfo=SHANGHAI))

    def test_next_periodic_exactly_at_daily_at_is_tomorrow(self) -> None:
        now = datetime(2026, 7, 28, 22, 0, tzinfo=UTC)  # 北京 06:00
        due = next_usage_periodic_at(now, daily_at="06:00", tz=SHANGHAI)
        self.assertEqual(due.astimezone(SHANGHAI), datetime(2026, 7, 30, 6, 0, tzinfo=SHANGHAI))


class UsageSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            usage_snapshot_enable=True,
            usage_periodic_interval_hours=24,
            usage_periodic_daily_at="06:00",
            bi_sync_biz_tz="Asia/Shanghai",
            usage_pre_reset_scan_interval_min=15,
        )

    def test_startup_before_daily_at_does_not_run_periodic_immediately(self) -> None:
        """启动时未到每日固定时刻，不应立刻跑日常采集。"""
        periodic = MagicMock()
        pre_reset = MagicMock()
        # 北京 05:00
        now = datetime(2026, 7, 28, 21, 0, tzinfo=UTC)

        with (
            patch("cam.usage_scheduler.SETTINGS", self.settings),
            patch("cam.usage_scheduler.run_usage_periodic", periodic),
            patch("cam.usage_scheduler.run_usage_pre_reset_due", pre_reset),
        ):
            coordinator = UsageSchedulerCoordinator(poll_interval_sec=1)
            coordinator.tick(now)
            time.sleep(0.05)
            periodic.assert_not_called()
            pre_reset.assert_called_once()
            self.assertEqual(
                coordinator._next_periodic_at.astimezone(SHANGHAI),
                datetime(2026, 7, 29, 6, 0, tzinfo=SHANGHAI),
            )
            coordinator.stop(timeout_sec=1)

    def test_periodic_runs_at_daily_at_then_schedules_tomorrow(self) -> None:
        periodic_started = threading.Event()

        def periodic(**_kwargs) -> None:
            periodic_started.set()

        with (
            patch("cam.usage_scheduler.SETTINGS", self.settings),
            patch("cam.usage_scheduler.run_usage_periodic", periodic),
            patch("cam.usage_scheduler.run_usage_pre_reset_due"),
        ):
            coordinator = UsageSchedulerCoordinator(poll_interval_sec=1)
            before = datetime(2026, 7, 28, 21, 0, tzinfo=UTC)  # 北京 05:00
            coordinator.tick(before)
            self.assertFalse(periodic_started.is_set())

            at_due = datetime(2026, 7, 28, 22, 0, tzinfo=UTC)  # 北京 06:00
            coordinator.tick(at_due)
            self.assertTrue(periodic_started.wait(0.5))
            self.assertEqual(
                coordinator._next_periodic_at.astimezone(SHANGHAI),
                datetime(2026, 7, 30, 6, 0, tzinfo=SHANGHAI),
            )
            coordinator.stop(timeout_sec=1)

    def test_periodic_and_pre_reset_use_independent_executors(self) -> None:
        periodic_started = threading.Event()
        pre_reset_started = threading.Event()
        release = threading.Event()

        def periodic(**_kwargs) -> None:
            periodic_started.set()
            release.wait(1)

        def pre_reset(**_kwargs) -> None:
            pre_reset_started.set()
            release.wait(1)

        with (
            patch("cam.usage_scheduler.SETTINGS", self.settings),
            patch("cam.usage_scheduler.run_usage_periodic", periodic),
            patch("cam.usage_scheduler.run_usage_pre_reset_due", pre_reset),
        ):
            coordinator = UsageSchedulerCoordinator(poll_interval_sec=1)
            # 直接到每日时刻，确保两个执行器都能并发启动
            coordinator.tick(datetime(2026, 7, 28, 22, 0, tzinfo=UTC))
            self.assertTrue(periodic_started.wait(0.5))
            self.assertTrue(pre_reset_started.wait(0.5))
            release.set()
            coordinator.stop(timeout_sec=1)

    def test_blocked_periodic_does_not_block_pre_reset_tick(self) -> None:
        periodic_started = threading.Event()
        pre_reset_started = threading.Event()
        release_periodic = threading.Event()

        def periodic(**_kwargs) -> None:
            periodic_started.set()
            release_periodic.wait(1)

        def pre_reset(**_kwargs) -> None:
            pre_reset_started.set()

        with (
            patch("cam.usage_scheduler.SETTINGS", self.settings),
            patch("cam.usage_scheduler.run_usage_periodic", periodic),
            patch("cam.usage_scheduler.run_usage_pre_reset_due", pre_reset),
        ):
            coordinator = UsageSchedulerCoordinator(poll_interval_sec=1)
            now = datetime(2026, 7, 29, tzinfo=UTC)
            coordinator._submit_periodic(now)
            self.assertTrue(periodic_started.wait(0.5))
            coordinator._submit_pre_reset(now)
            self.assertTrue(pre_reset_started.wait(0.5))
            release_periodic.set()
            coordinator.stop(timeout_sec=1)

    def test_repeated_start_returns_same_process_singleton(self) -> None:
        from cam import usage_scheduler

        with (
            patch("cam.usage_scheduler.SETTINGS", self.settings),
            patch("cam.usage_scheduler.run_usage_periodic"),
            patch("cam.usage_scheduler.run_usage_pre_reset_due"),
        ):
            usage_scheduler.stop_usage_scheduler(timeout_sec=0.1)
            first = usage_scheduler.start_usage_scheduler_once()
            second = usage_scheduler.start_usage_scheduler_once()
            self.assertIs(first, second)
            self.assertTrue(first.is_running)
            usage_scheduler.stop_usage_scheduler(timeout_sec=1)

    def test_stop_joins_timer_and_cancels_pending_periodic(self) -> None:
        with (
            patch("cam.usage_scheduler.SETTINGS", self.settings),
            patch("cam.usage_scheduler.run_usage_periodic"),
            patch("cam.usage_scheduler.run_usage_pre_reset_due"),
        ):
            coordinator = UsageSchedulerCoordinator(poll_interval_sec=1)
            coordinator.start()
            coordinator.stop(timeout_sec=1)
            self.assertFalse(coordinator.is_running)
            self.assertTrue(coordinator._stop.is_set())

    def test_worker_exception_does_not_kill_timer_thread(self) -> None:
        calls: list[str] = []

        def periodic(**_kwargs) -> None:
            calls.append("periodic")
            raise RuntimeError("预期异常")

        def pre_reset(**_kwargs) -> None:
            calls.append("pre_reset")

        with (
            patch("cam.usage_scheduler.SETTINGS", self.settings),
            patch("cam.usage_scheduler.run_usage_periodic", periodic),
            patch("cam.usage_scheduler.run_usage_pre_reset_due", pre_reset),
        ):
            coordinator = UsageSchedulerCoordinator(poll_interval_sec=1)
            coordinator.start()
            coordinator.tick(datetime(2026, 7, 29, tzinfo=UTC))
            deadline = time.monotonic() + 0.5
            while "pre_reset" not in calls and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(coordinator.is_running)
            self.assertIn("pre_reset", calls)
            coordinator.stop(timeout_sec=1)

    def test_usage_only_enable_starts_coordinator_without_legacy_jobs(self) -> None:
        stopper = threading.Event()
        stopper.set()
        usage_only_settings = SimpleNamespace(
            usage_snapshot_enable=True,
            bi_sync_enable=False,
            spending_refresh_enable=False,
            billing_ledger_refresh_enable=False,
            bi_sync_cron="30 1 * * *",
            spending_refresh_cron="0 2 * * *",
            billing_ledger_refresh_cron="0 3 * * *",
        )
        coordinator = MagicMock()

        with (
            patch("cam.scheduler.SETTINGS", usage_only_settings),
            patch("cam.scheduler.start_usage_scheduler_once", return_value=coordinator) as start,
        ):
            scheduler.run_scheduler_loop(stop_event=stopper)

        start.assert_called_once_with()
        coordinator.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
