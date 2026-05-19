import threading
import time
import unittest
import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("requests", types.ModuleType("requests"))
patchright_mod = types.ModuleType("patchright")
patchright_sync_api = types.ModuleType("patchright.sync_api")
patchright_sync_api.Page = object
patchright_sync_api.Playwright = object
patchright_sync_api.sync_playwright = lambda: None
sys.modules.setdefault("patchright", patchright_mod)
sys.modules.setdefault("patchright.sync_api", patchright_sync_api)
sys.modules.setdefault("pymysql", types.ModuleType("pymysql"))

from cam import bi_sync
from cam.models import Account
from cam.plan_scraper import PlanInfo


class FakeSyncLogStore:
    def __init__(self):
        self.stages = []

    def create_run(self, **_kwargs):
        pass

    def add_stage(self, **kwargs):
        self.stages.append(kwargs)

    def add_account_log(self, **_kwargs):
        pass

    def finish_run(self, **_kwargs):
        pass


class FakeLoader:
    active_loads = 0
    max_active_loads = 0
    load_lock = threading.Lock()

    def ensure_tables(self):
        pass

    def check_connection(self):
        pass

    def ensure_biz_date_partitions_ready(self, *, biz_date):
        pass

    def normalize_decimal_fields(self, row):
        return row

    def replace_ods_rows_for_account(self, *, biz_date, account_email, rows):
        with self.load_lock:
            type(self).active_loads += 1
            type(self).max_active_loads = max(type(self).max_active_loads, type(self).active_loads)
        time.sleep(0.005)
        with self.load_lock:
            type(self).active_loads -= 1
        return len(rows)


class BiSyncConcurrencyTests(unittest.TestCase):
    def test_run_daily_sync_fetches_accounts_concurrently_before_serial_load(self):
        accounts = [
            bi_sync.SnapshotAccount(
                account=Account(
                    email=f"cursor{i}@eclicktech.com.cn",
                    imap_password="pw",
                    imap_host="imap.feishu.cn",
                    imap_port=993,
                ),
                source="db",
                is_new=False,
                feishu_email="owner@example.com",
            )
            for i in range(4)
        ]
        settings = SimpleNamespace(
            bi_sync_enable=True,
            bi_sync_retry_times=1,
            api_concurrency=4,
        )
        active = 0
        max_active = 0
        lock = threading.Lock()
        FakeLoader.active_loads = 0
        FakeLoader.max_active_loads = 0
        log_store = FakeSyncLogStore()

        def fake_fetch_one(account, *, what, start_ts, end_ts):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return SimpleNamespace(
                errors={},
                usage_csv_text="csv",
                usage_events=[],
                plan={"name": "Ultra $20/mo"},
            )

        with (
            patch.object(bi_sync, "SETTINGS", settings),
            patch.object(bi_sync, "create_bi_sync_loader", lambda: FakeLoader()),
            patch.object(bi_sync, "_snapshot_accounts", return_value=accounts),
            patch.object(bi_sync.fetcher, "fetch_one", side_effect=fake_fetch_one),
            patch.object(bi_sync, "fetch_plan_info_from_dashboard", return_value=PlanInfo(status="active", amount=1)),
            patch.object(bi_sync, "_rows_from_usage_csv", return_value=[{"dt": "2026-05-13"}]),
            patch.object(bi_sync, "send_alert", lambda *_args, **_kwargs: None),
        ):
            result = bi_sync.run_daily_sync(
                biz_date="2026-05-13",
                trigger_type="manual",
                run_id="test_run",
                log_store=log_store,
            )

        self.assertEqual(result["status"], "success")
        self.assertGreaterEqual(max_active, 2)
        self.assertEqual(FakeLoader.max_active_loads, 1)
        self.assertTrue(
            any("load_queue=single_writer" in s.get("message", "") for s in log_store.stages)
        )


if __name__ == "__main__":
    unittest.main()
