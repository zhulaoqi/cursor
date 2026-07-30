"""用量快照认证熔断器测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
import unittest

from cam.usage_auth_breaker import UsageAuthBreaker
from cam.usage_snapshot_models import AuthOutcome


UTC_NOW = datetime(2026, 7, 29, 8, tzinfo=timezone.utc)


class MutableClock:
    """供时间窗口测试使用的可控 UTC 时钟。"""

    def __init__(self, value: datetime = UTC_NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def make_breaker(**overrides) -> UsageAuthBreaker:
    """创建参数明确的熔断器实例。"""
    values = {
        "min_samples": 3,
        "failure_ratio": 0.5,
        "cooldown": timedelta(minutes=5),
        "window_size": 5,
        "window_duration": timedelta(minutes=10),
        "clock": MutableClock(),
    }
    values.update(overrides)
    return UsageAuthBreaker(**values)


class UsageAuthBreakerTests(unittest.TestCase):
    """验证认证熔断状态机和并发安全性。"""

    def test_rejects_invalid_constructor_parameters(self):
        invalid_cases = (
            {"min_samples": 0},
            {"min_samples": True},
            {"failure_ratio": 0},
            {"failure_ratio": 1.1},
            {"failure_ratio": float("nan")},
            {"cooldown": timedelta(0)},
            {"window_size": 0},
            {"window_size": False},
            {"window_duration": timedelta(0)},
            {"window_size": 2, "min_samples": 3},
        )

        for overrides in invalid_cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    make_breaker(**overrides)

    def test_cached_token_is_always_allowed(self):
        breaker = make_breaker(min_samples=1, failure_ratio=1)
        breaker.record(AuthOutcome.AUTH_FAILURE, "a@example.com")

        self.assertEqual(breaker.snapshot().state, "open")
        self.assertTrue(breaker.allow_cached_token())

    def test_opens_at_threshold_after_minimum_samples(self):
        breaker = make_breaker()
        breaker.record(AuthOutcome.AUTH_FAILURE, "a@example.com")
        breaker.record(AuthOutcome.SUCCESS, "b@example.com")
        self.assertEqual(breaker.snapshot().state, "closed")

        breaker.record(AuthOutcome.AUTH_FAILURE, "c@example.com")
        snapshot = breaker.snapshot()

        self.assertEqual(snapshot.state, "open")
        self.assertEqual(snapshot.sample_count, 3)
        self.assertEqual(snapshot.auth_failure_count, 2)
        self.assertEqual(snapshot.opened_at, UTC_NOW)
        self.assertEqual(snapshot.retry_at, UTC_NOW + timedelta(minutes=5))

    def test_prunes_samples_by_time_and_keeps_recent_window_size(self):
        clock = MutableClock()
        breaker = make_breaker(
            min_samples=2,
            failure_ratio=1,
            window_size=2,
            window_duration=timedelta(minutes=2),
            clock=clock,
        )
        breaker.record(AuthOutcome.SUCCESS, "a@example.com")
        clock.value += timedelta(minutes=1)
        breaker.record(AuthOutcome.AUTH_FAILURE, "b@example.com")
        clock.value += timedelta(minutes=1)
        breaker.record(AuthOutcome.SUCCESS, "c@example.com")

        snapshot = breaker.snapshot()
        self.assertEqual(snapshot.sample_count, 2)
        self.assertEqual(snapshot.auth_failure_count, 1)

        clock.value += timedelta(minutes=3)
        snapshot = breaker.snapshot()
        self.assertEqual(snapshot.sample_count, 0)
        self.assertEqual(snapshot.auth_failure_count, 0)

    def test_open_denies_refresh_until_cooldown_then_allows_one_probe(self):
        clock = MutableClock()
        breaker = make_breaker(
            min_samples=1,
            failure_ratio=1,
            cooldown=timedelta(seconds=10),
            clock=clock,
        )
        breaker.record(AuthOutcome.AUTH_FAILURE, "a@example.com")

        self.assertFalse(breaker.allow_refresh_or_login())
        self.assertEqual(breaker.snapshot().state, "open")

        clock.value += timedelta(seconds=10)
        self.assertTrue(breaker.allow_refresh_or_login())
        self.assertEqual(breaker.snapshot().state, "half_open")
        self.assertFalse(breaker.allow_refresh_or_login())

    def test_half_open_allows_exactly_one_probe_concurrently(self):
        clock = MutableClock()
        breaker = make_breaker(
            min_samples=1,
            failure_ratio=1,
            cooldown=timedelta(seconds=1),
            clock=clock,
        )
        breaker.record(AuthOutcome.AUTH_FAILURE, "a@example.com")
        clock.value += timedelta(seconds=1)
        barrier = threading.Barrier(8)
        results: list[bool] = []
        results_lock = threading.Lock()

        def attempt_probe() -> None:
            barrier.wait()
            result = breaker.allow_refresh_or_login()
            with results_lock:
                results.append(result)

        threads = [threading.Thread(target=attempt_probe) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive(), "探针线程未在时限内退出")

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 7)

    def test_half_open_success_closes_and_clears_samples(self):
        clock = MutableClock()
        breaker = make_breaker(
            min_samples=1,
            failure_ratio=1,
            cooldown=timedelta(seconds=1),
            clock=clock,
        )
        breaker.record(AuthOutcome.AUTH_FAILURE, "a@example.com")
        clock.value += timedelta(seconds=1)
        self.assertTrue(breaker.allow_refresh_or_login())

        breaker.record(AuthOutcome.SUCCESS, "probe@example.com")
        snapshot = breaker.snapshot()

        self.assertEqual(snapshot.state, "closed")
        self.assertEqual(snapshot.sample_count, 0)
        self.assertEqual(snapshot.auth_failure_count, 0)
        self.assertIsNone(snapshot.opened_at)
        self.assertIsNone(snapshot.retry_at)
        self.assertTrue(breaker.allow_refresh_or_login())

    def test_half_open_auth_failure_reopens_and_resets_alert_period(self):
        clock = MutableClock()
        breaker = make_breaker(
            min_samples=1,
            failure_ratio=1,
            cooldown=timedelta(seconds=1),
            clock=clock,
        )
        breaker.record(AuthOutcome.AUTH_FAILURE, "a@example.com")
        self.assertTrue(breaker.should_emit_open_alert())
        self.assertFalse(breaker.should_emit_open_alert())

        clock.value += timedelta(seconds=1)
        self.assertTrue(breaker.allow_refresh_or_login())
        breaker.record(AuthOutcome.AUTH_FAILURE, "probe@example.com")

        self.assertEqual(breaker.snapshot().state, "open")
        self.assertTrue(breaker.should_emit_open_alert())
        self.assertFalse(breaker.should_emit_open_alert())

    def test_non_auth_failure_and_skipped_do_not_change_ratio_or_state(self):
        breaker = make_breaker(min_samples=1, failure_ratio=1)
        breaker.record(AuthOutcome.NON_AUTH_FAILURE, "a@example.com")
        breaker.record(AuthOutcome.SKIPPED, "b@example.com")

        snapshot = breaker.snapshot()
        self.assertEqual(snapshot.state, "closed")
        self.assertEqual(snapshot.sample_count, 0)
        self.assertEqual(snapshot.auth_failure_count, 0)

    def test_record_rejects_invalid_outcome_and_blank_email(self):
        breaker = make_breaker()
        with self.assertRaises(ValueError):
            breaker.record("auth_failure", "a@example.com")
        with self.assertRaises(ValueError):
            breaker.record(AuthOutcome.SUCCESS, " \n ")

    def test_concurrent_recording_is_atomic(self):
        breaker = make_breaker(
            min_samples=100,
            failure_ratio=1,
            window_size=200,
        )
        barrier = threading.Barrier(40)

        def record(outcome: AuthOutcome, index: int) -> None:
            barrier.wait()
            breaker.record(outcome, f"user-{index}@example.com")

        threads = [
            threading.Thread(
                target=record,
                args=(
                    AuthOutcome.AUTH_FAILURE if index % 2 else AuthOutcome.SUCCESS,
                    index,
                ),
            )
            for index in range(40)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive(), "record 线程未在时限内退出")

        snapshot = breaker.snapshot()
        self.assertEqual(snapshot.sample_count, 40)
        self.assertEqual(snapshot.auth_failure_count, 20)

    def test_periodic_and_pre_reset_contexts_share_samples_on_same_instance(self):
        breaker = make_breaker(min_samples=2, failure_ratio=1)

        def periodic_context() -> None:
            breaker.record(AuthOutcome.AUTH_FAILURE, "a@example.com")

        def pre_reset_context() -> None:
            breaker.record(AuthOutcome.AUTH_FAILURE, "b@example.com")

        periodic_context()
        self.assertEqual(breaker.snapshot().state, "closed")
        pre_reset_context()
        self.assertEqual(breaker.snapshot().state, "open")


if __name__ == "__main__":
    unittest.main()
