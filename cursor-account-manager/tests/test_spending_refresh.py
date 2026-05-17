import unittest
from unittest.mock import MagicMock, patch

from cam import spending_refresh


class SpendingRefreshSilentTests(unittest.TestCase):
    @patch("cam.spending_refresh.send_alert")
    @patch("cam.spending_refresh._run_spending_refresh_core")
    @patch("cam.spending_refresh.get_default_sync_log_store")
    @patch("cam.spending_refresh.get_default_store")
    def test_scheduled_refresh_sends_success_alert(
        self,
        mock_get_store: MagicMock,
        mock_get_log_store: MagicMock,
        mock_core: MagicMock,
        mock_alert: MagicMock,
    ) -> None:
        store = MagicMock()
        store.list_accounts.return_value = [{"email": "a@x.com"}]
        mock_get_store.return_value = store
        log_store = MagicMock()
        mock_get_log_store.return_value = log_store
        mock_core.return_value = {
            "ok": 1,
            "failed": 0,
            "total": 1,
            "on_demand_open": 0,
            "on_demand_historical": 0,
        }

        mock_settings = MagicMock()
        mock_settings.spending_refresh_alert_enable = True
        mock_settings.alert_bot_enable = True
        with patch("cam.spending_refresh.SETTINGS", mock_settings):
            out = spending_refresh.run_daily_spending_refresh_scheduled(
                trigger_type="spending_scheduler",
                trigger_date="2026-05-17",
            )

        self.assertEqual(out["ok"], 1)
        mock_alert.assert_called_once()
        title, content = mock_alert.call_args[0][0], mock_alert.call_args[0][1]
        self.assertIn("调度刷新成功", title)
        self.assertIn("on_demand_open=0", content)
        self.assertEqual(mock_alert.call_args[1].get("level"), "success")
        log_store.create_run.assert_called_once()
        log_store.finish_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
