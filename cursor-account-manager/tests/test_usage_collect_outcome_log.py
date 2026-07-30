"""用量采集终态日志查询：供看板数据状态悬停展示失败原因。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cam.sync_log_store import SyncLogStore, UsageCollectOutcome


class UsageCollectOutcomeLogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "tokens.db"
        self.store = SyncLogStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_run(self, run_id: str, trigger_type: str = "usage_periodic:slot") -> None:
        self.store.create_run(
            run_id=run_id,
            biz_date="2026-07-30",
            trigger_type=trigger_type,
            account_total=1,
            account_snapshot_total=1,
            new_account_count=0,
        )

    def _add_usage_log(
        self,
        run_id: str,
        *,
        email: str,
        status: str,
        ended_at: int,
        error_message: str = "",
        account_source: str = "usage_snapshot",
    ) -> None:
        self.store.add_account_log(
            run_id=run_id,
            account_email=email,
            account_source=account_source,
            is_new_account=False,
            status=status,
            started_at=ended_at - 1,
            ended_at=ended_at,
            fetch_rows=0,
            load_rows=0,
            error_message=error_message,
        )

    def test_latest_outcome_returns_most_recent_usage_snapshot_log(self) -> None:
        self._create_run("run-1")
        self._create_run("run-2")
        self._add_usage_log(
            "run-1",
            email="User@Example.com",
            status="failed",
            ended_at=100,
            error_message="old failure",
        )
        self._add_usage_log(
            "run-2",
            email="user@example.com",
            status="failed",
            ended_at=200,
            error_message="auth rejected",
        )

        outcome = self.store.get_latest_usage_collect_outcome("user@example.com")

        self.assertEqual(
            outcome,
            UsageCollectOutcome(
                email="user@example.com",
                status="failed",
                error_message="auth rejected",
                ended_at=200,
            ),
        )

    def test_latest_outcome_ignores_non_usage_sources(self) -> None:
        self._create_run("run-bi", trigger_type="bi_daily")
        self._create_run("run-usage")
        self._add_usage_log(
            "run-bi",
            email="user@example.com",
            status="failed",
            ended_at=300,
            error_message="bi fail",
            account_source="db",
        )
        self._add_usage_log(
            "run-usage",
            email="user@example.com",
            status="skipped",
            ended_at=100,
            error_message="auth_circuit_open",
        )

        outcome = self.store.get_latest_usage_collect_outcome("user@example.com")

        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(outcome.error_message, "auth_circuit_open")

    def test_map_latest_outcomes_batch_by_email(self) -> None:
        self._create_run("run-a")
        self._create_run("run-b")
        self._add_usage_log(
            "run-a",
            email="a@example.com",
            status="failed",
            ended_at=10,
            error_message="net timeout",
        )
        self._add_usage_log(
            "run-b",
            email="b@example.com",
            status="success",
            ended_at=20,
        )

        mapping = self.store.map_latest_usage_collect_outcomes(
            ["a@example.com", "b@example.com", "missing@example.com"]
        )

        self.assertEqual(set(mapping), {"a@example.com", "b@example.com"})
        self.assertEqual(mapping["a@example.com"].error_message, "net timeout")
        self.assertEqual(mapping["b@example.com"].status, "success")

    def test_latest_outcome_none_when_no_logs(self) -> None:
        self.assertIsNone(self.store.get_latest_usage_collect_outcome("nobody@example.com"))


if __name__ == "__main__":
    unittest.main()
