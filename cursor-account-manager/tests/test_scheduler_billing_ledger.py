import unittest
import sys
import types
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.modules.setdefault("requests", types.SimpleNamespace(post=lambda *args, **kwargs: None))
sys.modules.setdefault(
    "cam.bi_sync",
    types.SimpleNamespace(run_daily_sync=lambda *args, **kwargs: {}),
)
sys.modules.setdefault(
    "cam.spending_refresh",
    types.SimpleNamespace(run_daily_spending_refresh_scheduled=lambda *args, **kwargs: {}),
)

from cam import scheduler


class SchedulerBillingLedgerTests(unittest.TestCase):
    @patch("cam.scheduler.run_daily_billing_ledger_refresh_scheduled")
    def test_billing_ledger_refresh_runs_once_after_due_time(self, mock_run: MagicMock) -> None:
        now = datetime(2026, 6, 30, 5, 0, 1, tzinfo=timezone(timedelta(hours=8)))
        log_store = MagicMock()
        log_store.has_run_for_trigger.return_value = False

        @contextmanager
        def fake_lock(_path):
            yield True

        settings = SimpleNamespace(
            billing_ledger_refresh_enable=True,
            billing_ledger_refresh_cron="0 5 * * *",
            billing_ledger_refresh_lock_file="/tmp/ledger.lock",
        )
        with patch("cam.scheduler.SETTINGS", settings), patch("cam.scheduler._try_lock", fake_lock):
            last = scheduler._run_billing_ledger_refresh_if_due(
                now,
                last_trigger_date="",
                log_store=log_store,
            )

        self.assertEqual(last, "2026-06-30")
        log_store.has_run_for_trigger.assert_called_once_with(
            biz_date="2026-06-30",
            trigger_type=scheduler.BILLING_LEDGER_TRIGGER_TYPE,
        )
        mock_run.assert_called_once_with(
            trigger_type=scheduler.BILLING_LEDGER_TRIGGER_TYPE,
            trigger_date="2026-06-30",
        )


if __name__ == "__main__":
    unittest.main()
