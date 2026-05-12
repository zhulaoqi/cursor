import os
import unittest
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
            },
            clear=False,
        ):
            settings = config.load_settings()

        self.assertEqual(settings.browser_login_concurrency, 5)
        self.assertEqual(settings.invoice_download_concurrency, 4)
        self.assertEqual(settings.invoice_active_context_limit, 3)
        self.assertEqual(settings.api_concurrency, 30)

    def test_bi_sync_related_defaults(self):
        with patch.dict(
            os.environ,
            {
                "BI_SYNC_ENABLE": "",
                "BI_SYNC_CRON": "",
                "BI_SYNC_LOCK_FILE": "",
                "ALERT_BOT_ENABLE": "",
            },
            clear=False,
        ):
            settings = config.load_settings()

        self.assertFalse(settings.bi_sync_enable)
        self.assertEqual(settings.bi_sync_cron, "30 1 * * *")
        self.assertEqual(settings.bi_sync_lock_file, "/tmp/cam_bi_sync.lock")
        self.assertFalse(settings.alert_bot_enable)
        self.assertEqual(settings.alert_to_emails, "")


if __name__ == "__main__":
    unittest.main()
