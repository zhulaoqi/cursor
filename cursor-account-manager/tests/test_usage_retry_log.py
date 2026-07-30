import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cam.sync_log_store import AccountAttemptState, SyncLogStore
from cam.usage_snapshot_refresh import (
    usage_periodic_trigger_type,
    usage_pre_reset_trigger_type,
)


class UsageRetryLogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "tokens.db"
        self.store = SyncLogStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_run(self, run_id, trigger_type, status="failed"):
        self.store.create_run(
            run_id=run_id,
            biz_date="2026-07-29",
            trigger_type=trigger_type,
            account_total=1,
            account_snapshot_total=1,
            new_account_count=0,
        )
        self.store.finish_run(
            run_id=run_id,
            status=status,
            account_success=0,
            account_failed=1,
            event_total=0,
            ods_rows=0,
        )

    def _add_account_log(self, run_id, status, ended_at):
        self.store.add_account_log(
            run_id=run_id,
            account_email="user@example.com",
            account_source="db",
            is_new_account=False,
            status=status,
            started_at=ended_at - 1,
            ended_at=ended_at,
            fetch_rows=0,
            load_rows=0,
        )

    def test_attempt_state_counts_terminal_rows_for_exact_slot(self):
        trigger = "usage_periodic:20260729T080000.000000Z"
        self._create_run("run-1", trigger)
        self._create_run("run-2", trigger)
        self._create_run("run-3", trigger)
        self._add_account_log("run-1", "failed", 101)
        self._add_account_log("run-2", "skipped", 202)
        self._add_account_log("run-3", "running", 303)

        state = self.store.get_account_attempt_state(
            account_email="user@example.com",
            trigger_type=trigger,
        )

        self.assertEqual(state.attempts, 2)
        self.assertEqual(state.last_failed_at, 101)
        self.assertFalse(state.succeeded)

    def test_success_account_log_marks_slot_complete_regardless_of_run_status(self):
        trigger = "usage_periodic:20260729T080000.000000Z"
        self._create_run("run-1", trigger, status="failed")
        self._add_account_log("run-1", "success", 100)

        state = self.store.get_account_attempt_state(
            account_email="user@example.com",
            trigger_type=trigger,
        )

        self.assertEqual(state.attempts, 1)
        self.assertTrue(state.succeeded)

    def test_run_without_account_log_does_not_mark_account_success(self):
        trigger = "usage_periodic:20260729T080000.000000Z"
        self._create_run("run-1", trigger, status="success")

        state = self.store.get_account_attempt_state(
            account_email="user@example.com",
            trigger_type=trigger,
        )

        self.assertEqual(state, AccountAttemptState(0, None, False))

    def test_different_slot_and_account_do_not_affect_state(self):
        slot_one = "usage_periodic:20260729T080000.000000Z"
        slot_two = "usage_periodic:20260729T090000.000000Z"
        self._create_run("run-1", slot_one)
        self._create_run("run-2", slot_two)
        self._add_account_log("run-1", "failed", 101)
        self._add_account_log("run-2", "success", 202)
        self.store.add_account_log(
            run_id="run-1",
            account_email="other@example.com",
            account_source="db",
            is_new_account=False,
            status="success",
            started_at=299,
            ended_at=300,
            fetch_rows=0,
            load_rows=0,
        )

        state = self.store.get_account_attempt_state(
            account_email="user@example.com",
            trigger_type=slot_one,
        )

        self.assertEqual(state, AccountAttemptState(1, 101, False))

    def test_last_failed_at_only_uses_latest_failed_log(self):
        trigger = "usage_pre_reset:20260701T000000.000000Z"
        for index, status, ended_at in (
            (1, "failed", 100),
            (2, "skipped", 500),
            (3, "failed", 300),
            (4, "success", 600),
        ):
            run_id = f"run-{index}"
            self._create_run(run_id, trigger)
            self._add_account_log(run_id, status, ended_at)

        state = self.store.get_account_attempt_state(
            account_email="user@example.com",
            trigger_type=trigger,
        )

        self.assertEqual(state.attempts, 4)
        self.assertEqual(state.last_failed_at, 300)
        self.assertTrue(state.succeeded)

    def test_restart_reads_persisted_attempt_state(self):
        trigger = "usage_periodic:20260729T080000.000000Z"
        self._create_run("run-1", trigger)
        self._add_account_log("run-1", "failed", 123)

        restarted_store = SyncLogStore(self.db_path)

        self.assertEqual(
            restarted_store.get_account_attempt_state(
                account_email="user@example.com",
                trigger_type=trigger,
            ),
            AccountAttemptState(1, 123, False),
        )

    def test_query_rejects_blank_email_and_trigger_type(self):
        for email in ("", " ", "\t"):
            with self.subTest(email=email):
                with self.assertRaises(ValueError):
                    self.store.get_account_attempt_state(
                        account_email=email,
                        trigger_type="usage_periodic:slot",
                    )
        for trigger_type in ("", " "):
            with self.subTest(trigger_type=trigger_type):
                with self.assertRaises(ValueError):
                    self.store.get_account_attempt_state(
                        account_email="user@example.com",
                        trigger_type=trigger_type,
                    )

    def test_query_normalizes_non_empty_email_before_matching(self):
        trigger = "usage_periodic:20260729T080000.000000Z"
        self._create_run("run-1", trigger)
        self._add_account_log("run-1", "failed", 123)

        try:
            state = self.store.get_account_attempt_state(
                account_email="  User@Example.COM ",
                trigger_type=trigger,
            )
        except ValueError as exc:
            self.fail(f"非空邮箱应内部规范化，不应拒绝：{exc}")

        self.assertEqual(state, AccountAttemptState(1, 123, False))

    def test_query_passes_normalized_email_as_sql_parameter(self):
        captured_params = None

        class FakeResult:
            def fetchone(self):
                return {
                    "attempts": 0,
                    "last_failed_at": None,
                    "succeeded": 0,
                }

        class FakeConnection:
            def execute(self, _sql, params):
                nonlocal captured_params
                captured_params = params
                return FakeResult()

        @contextmanager
        def fake_conn():
            yield FakeConnection()

        with patch.object(self.store, "_conn", fake_conn):
            try:
                self.store.get_account_attempt_state(
                    account_email=" User@Example.COM ",
                    trigger_type="usage_periodic:slot",
                )
            except ValueError as exc:
                self.fail(f"非空邮箱应内部规范化，不应拒绝：{exc}")

        self.assertEqual(
            captured_params,
            ("user@example.com", "usage_periodic:slot"),
        )

    def test_account_attempt_state_is_frozen(self):
        state = AccountAttemptState(1, 123, False)

        with self.assertRaises(FrozenInstanceError):
            state.attempts = 2

    def test_trigger_keys_normalize_same_instant_to_utc(self):
        utc_value = datetime(
            2026, 7, 29, 8, 1, 2, 345678, tzinfo=timezone.utc
        )
        east_eight = utc_value.astimezone(
            timezone(timedelta(hours=8))
        )

        self.assertEqual(
            usage_periodic_trigger_type(utc_value),
            "usage_periodic:20260729T080102.345678Z",
        )
        self.assertEqual(
            usage_periodic_trigger_type(utc_value),
            usage_periodic_trigger_type(east_eight),
        )
        self.assertEqual(
            usage_pre_reset_trigger_type(utc_value),
            "usage_pre_reset:20260729T080102.345678Z",
        )
        self.assertEqual(
            usage_pre_reset_trigger_type(utc_value),
            usage_pre_reset_trigger_type(east_eight),
        )

    def test_trigger_keys_reject_naive_datetime(self):
        naive = datetime(2026, 7, 29, 8)

        with self.assertRaises(ValueError):
            usage_periodic_trigger_type(naive)
        with self.assertRaises(ValueError):
            usage_pre_reset_trigger_type(naive)

    def test_schema_upgrade_adds_indexes_idempotently(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DROP INDEX idx_sync_job_run_trigger")
            conn.execute("DROP INDEX idx_sync_job_account_email_run")

        SyncLogStore(self.db_path)
        SyncLogStore(self.db_path)

        with sqlite3.connect(self.db_path) as conn:
            run_indexes = {
                row[1]: tuple(
                    column[2]
                    for column in conn.execute(
                        f"PRAGMA index_info({row[1]})"
                    )
                )
                for row in conn.execute(
                    "PRAGMA index_list(sync_job_run)"
                )
            }
            account_indexes = {
                row[1]: tuple(
                    column[2]
                    for column in conn.execute(
                        f"PRAGMA index_info({row[1]})"
                    )
                )
                for row in conn.execute(
                    "PRAGMA index_list(sync_job_account_log)"
                )
            }

        self.assertEqual(
            run_indexes["idx_sync_job_run_trigger"],
            ("trigger_type", "run_id"),
        )
        self.assertEqual(
            account_indexes["idx_sync_job_account_email_run"],
            ("account_email", "run_id", "ended_at"),
        )


if __name__ == "__main__":
    unittest.main()
