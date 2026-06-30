import unittest
from decimal import Decimal
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.modules.setdefault("requests", types.SimpleNamespace(post=lambda *args, **kwargs: None))

from cam import billing_ledger_refresh


class BillingLedgerRefreshTests(unittest.TestCase):
    @patch("cam.billing_ledger_refresh.get_default_manager")
    @patch("cam.billing_ledger_refresh.get_ledger_store")
    @patch("cam.billing_ledger_refresh.scrape_billing_ledger_batch")
    @patch("cam.billing_ledger_refresh.get_default_sync_log_store")
    @patch("cam.billing_ledger_refresh.get_default_store")
    def test_scheduled_refresh_uses_current_month_and_records_run(
        self,
        mock_get_store: MagicMock,
        mock_get_log_store: MagicMock,
        mock_scrape: MagicMock,
        mock_get_ledger_store: MagicMock,
        mock_get_manager: MagicMock,
    ) -> None:
        store = MagicMock()
        store.list_accounts.return_value = [
            {
                "email": "a@example.com",
                "imap_password": "pw",
                "imap_host": "imap.example.com",
                "imap_port": 993,
                "feishu_email": "owner@example.com",
                "source": "db",
            }
        ]
        mock_get_store.return_value = store
        log_store = MagicMock()
        mock_get_log_store.return_value = log_store
        ledger_store = MagicMock()
        ledger_store.upsert_summaries.return_value = 1
        mock_get_ledger_store.return_value = ledger_store
        summary = SimpleNamespace(
            email="a@example.com",
            feishu_email="owner@example.com",
            billing_month="2026-06",
            amount_total_usd=Decimal("10"),
            refund_total_usd=Decimal("0"),
            net_spend_usd=Decimal("10"),
            row_count=1,
        )

        def fake_scrape(accounts, invoice_month, *, manager, progress_cb):
            progress_cb("a@example.com", "done", "净支出 10 USD（1 行）")
            return [summary], []

        mock_scrape.side_effect = fake_scrape

        out = billing_ledger_refresh.run_daily_billing_ledger_refresh_scheduled(
            trigger_type="billing_ledger_scheduler",
            trigger_date="2026-06-30",
        )

        self.assertEqual(out["billing_month"], "2026-06")
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["account_success"], 1)
        self.assertEqual(out["account_failed"], 0)
        log_store.create_run.assert_called_once()
        create_kwargs = log_store.create_run.call_args.kwargs
        self.assertEqual(create_kwargs["biz_date"], "2026-06-30")
        self.assertEqual(create_kwargs["trigger_type"], "billing_ledger_scheduler")
        mock_scrape.assert_called_once()
        self.assertEqual(mock_scrape.call_args.args[1], "2026-06")
        ledger_store.ensure_tables.assert_called_once()
        ledger_store.upsert_summaries.assert_called_once_with([summary])
        log_store.finish_run.assert_called_once()
        finish_kwargs = log_store.finish_run.call_args.kwargs
        self.assertEqual(finish_kwargs["status"], "success")
        self.assertEqual(finish_kwargs["account_success"], 1)
        self.assertEqual(finish_kwargs["account_failed"], 0)
        log_store.add_account_log.assert_called_once()


if __name__ == "__main__":
    unittest.main()
