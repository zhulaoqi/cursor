import os
import sys
import types
import unittest
from unittest.mock import patch

sys.modules.setdefault("requests", types.ModuleType("requests"))
patchright_mod = types.ModuleType("patchright")
patchright_async_api = types.ModuleType("patchright.async_api")
patchright_async_api.async_playwright = lambda: None
sys.modules.setdefault("patchright", patchright_mod)
sys.modules.setdefault("patchright.async_api", patchright_async_api)

from cam import config, plan_scraper


class WebFetchWorkersTests(unittest.TestCase):
    def test_no_hard_cap_at_30_when_web_fetch_max_workers_zero(self):
        with patch.dict(
            os.environ,
            {
                "API_CONCURRENCY": "40",
                "BROWSER_LOGIN_CONCURRENCY": "5",
                "WEB_FETCH_MAX_WORKERS": "0",
            },
            clear=False,
        ):
            with patch.object(config, "SETTINGS", config.load_settings()):
                self.assertEqual(config.web_fetch_thread_workers(500), 80)

    def test_web_fetch_max_workers_caps_boosted_pool(self):
        with patch.dict(
            os.environ,
            {
                "API_CONCURRENCY": "40",
                "BROWSER_LOGIN_CONCURRENCY": "5",
                "WEB_FETCH_MAX_WORKERS": "50",
            },
            clear=False,
        ):
            with patch.object(config, "SETTINGS", config.load_settings()):
                self.assertEqual(config.web_fetch_thread_workers(500), 50)


class SpendingBatchParallelTests(unittest.TestCase):
    def test_max_parallel_respects_active_context_limit(self):
        with patch.object(plan_scraper, "SETTINGS") as mock_settings:
            mock_settings.spending_refresh_concurrency = 10
            mock_settings.invoice_active_context_limit = 6
            self.assertEqual(plan_scraper._spending_batch_max_parallel(100), 6)

    def test_max_parallel_unlimited_when_active_limit_zero(self):
        with patch.object(plan_scraper, "SETTINGS") as mock_settings:
            mock_settings.spending_refresh_concurrency = 10
            mock_settings.invoice_active_context_limit = 0
            self.assertEqual(plan_scraper._spending_batch_max_parallel(100), 10)


if __name__ == "__main__":
    unittest.main()
