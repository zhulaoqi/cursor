import os
import unittest
from pathlib import Path
from unittest.mock import patch

from cam import config


class ConfigConcurrencyTests(unittest.TestCase):
    def test_load_settings_reads_independent_invoice_download_concurrency(self):
        with patch.dict(
            os.environ,
            {
                "BROWSER_LOGIN_CONCURRENCY": "5",
                "INVOICE_DOWNLOAD_CONCURRENCY": "8",
                "INVOICE_ACTIVE_CONTEXT_LIMIT": "6",
                "API_CONCURRENCY": "30",
            },
            clear=False,
        ):
            settings = config.load_settings()

        self.assertEqual(settings.browser_login_concurrency, 5)
        self.assertEqual(settings.invoice_download_concurrency, 8)
        self.assertEqual(settings.invoice_active_context_limit, 6)
        self.assertEqual(settings.api_concurrency, 30)

    def test_concurrency_defaults_match_recommended_values(self):
        with patch.dict(
            os.environ,
            {
                "BROWSER_LOGIN_CONCURRENCY": "",
                "INVOICE_DOWNLOAD_CONCURRENCY": "",
                "INVOICE_ACTIVE_CONTEXT_LIMIT": "",
                "API_CONCURRENCY": "",
                "BILLING_LEDGER_RETRY_TIMES": "",
            },
            clear=False,
        ):
            settings = config.load_settings()

        self.assertEqual(settings.browser_login_concurrency, 8)
        self.assertEqual(settings.invoice_download_concurrency, 10)
        self.assertEqual(settings.invoice_active_context_limit, 6)
        self.assertEqual(settings.api_concurrency, 30)
        self.assertEqual(settings.billing_ledger_retry_times, 2)
        self.assertEqual(settings.billing_ledger_concurrency, 3)

    def test_bi_sync_related_defaults(self):
        with patch.dict(
            os.environ,
            {
                "BI_SYNC_ENABLE": "",
                "BI_SYNC_CRON": "",
                "BI_SYNC_LOCK_FILE": "",
                "BI_SYNC_DB_QUERY_TIMEOUT_SEC": "",
                "ALERT_BOT_ENABLE": "",
                "ALERT_TO_EMAILS": "",
            },
            clear=False,
        ):
            settings = config.load_settings()

        self.assertFalse(settings.bi_sync_enable)
        self.assertEqual(settings.bi_sync_cron, "30 1 * * *")
        self.assertEqual(settings.bi_sync_lock_file, "/tmp/cam_bi_sync.lock")
        self.assertEqual(settings.bi_sync_db_query_timeout_sec, 120)
        self.assertTrue(settings.spending_refresh_enable)
        self.assertEqual(settings.spending_refresh_cron, "0 3 * * *")
        self.assertEqual(settings.spending_refresh_lock_file, "/tmp/cam_spending_refresh.lock")
        self.assertTrue(settings.billing_ledger_refresh_enable)
        self.assertEqual(settings.billing_ledger_refresh_cron, "0 5 * * *")
        self.assertEqual(settings.billing_ledger_refresh_lock_file, "/tmp/cam_billing_ledger_refresh.lock")
        self.assertFalse(settings.alert_bot_enable)
        self.assertEqual(settings.alert_to_emails, "")

    def test_chrome_profiles_dir_defaults_to_home_on_non_windows(self):
        with patch.object(config.os, "name", "posix"):
            with patch.dict(os.environ, {"CAM_CHROME_PROFILES_DIR": ""}, clear=False):
                settings = config.load_settings()
        self.assertEqual(settings.chrome_profiles_dir, Path.home() / ".cam" / "chrome-profiles")

    def test_windows_account_files_are_on_d_drive(self):
        with patch.object(config.os, "name", "nt"):
            settings = config.load_settings()
        self.assertEqual(settings.tokens_db, config.WINDOWS_DATA_DIR / "tokens.db")
        self.assertEqual(settings.accounts_csv, config.WINDOWS_DATA_DIR / "accounts.csv")

    def test_chrome_profiles_dir_on_windows_is_always_d_drive(self):
        with patch.object(config.os, "name", "nt"):
            with patch.dict(
                os.environ,
                {"CAM_CHROME_PROFILES_DIR": r"C:\Users\x\.cam\chrome-profiles"},
                clear=False,
            ):
                settings = config.load_settings()
        self.assertEqual(settings.chrome_profiles_dir, config.WINDOWS_CHROME_PROFILES_DIR)

    def test_chrome_profiles_dir_reads_absolute_env_on_non_windows(self):
        with patch.object(config.os, "name", "posix"):
            with patch.dict(
                os.environ,
                {"CAM_CHROME_PROFILES_DIR": "/tmp/cam-chrome-profiles"},
                clear=False,
            ):
                settings = config.load_settings()
        self.assertEqual(settings.chrome_profiles_dir, Path("/tmp/cam-chrome-profiles"))


if __name__ == "__main__":
    unittest.main()
