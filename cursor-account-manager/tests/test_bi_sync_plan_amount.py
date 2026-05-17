import sys
import types
import unittest
from decimal import Decimal
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


class BiSyncPlanAmountTests(unittest.TestCase):
    def test_extract_plan_amount_keeps_only_number(self):
        self.assertEqual(bi_sync._extract_plan_amount("Ultra $20/mo"), Decimal("20"))
        self.assertEqual(bi_sync._extract_plan_amount("$60/mo"), Decimal("60"))
        self.assertEqual(bi_sync._extract_plan_amount("Ultra 20"), Decimal("20"))
        self.assertIsNone(bi_sync._extract_plan_amount("Free"))
        self.assertIsNone(bi_sync._extract_plan_amount(""))

    def test_usage_csv_rows_include_feishu_email_and_plan_amount(self):
        rows = bi_sync._rows_from_usage_csv(
            run_id="run1",
            biz_date="2026-05-13",
            email="cursor@example.com",
            feishu_email="owner@example.com",
            plan_amount=Decimal("20"),
            csv_text=(
                "Date,Kind,Model,Input (w/o Cache Write),Input (w/ Cache Write),"
                "Output Tokens,Total Tokens,Cost\n"
                "2026-05-13T01:00:00Z,Usage,gpt-4,1,2,3,6,$0.10\n"
            ),
        )

        self.assertEqual(rows[0]["feishu_email"], "owner@example.com")
        self.assertEqual(rows[0]["plan_amount"], Decimal("20"))

    def test_snapshot_accounts_does_not_fallback_to_csv_when_db_is_empty(self):
        store = SimpleNamespace(list_accounts=lambda: [])
        with patch.object(bi_sync, "get_default_store", return_value=store):
            self.assertEqual(bi_sync._snapshot_accounts(biz_date="2026-05-13"), [])

    def test_missing_feishu_email_fails_before_fetch(self):
        item = bi_sync.SnapshotAccount(
            account=Account(
                email="cursor@example.com",
                imap_password="pw",
                imap_host="imap.feishu.cn",
                imap_port=993,
            ),
            source="db",
            is_new=False,
            feishu_email="",
        )

        with patch.object(bi_sync.fetcher, "fetch_one") as fetch_one:
            result = bi_sync._fetch_account_usage_rows(
                item,
                run_id="run1",
                biz_date="2026-05-13",
                start_ts=1,
                end_ts=2,
            )

        fetch_one.assert_not_called()
        self.assertEqual(result.rows, [])
        self.assertIn("E_ACCOUNT_METADATA", result.error)

    def test_unparsed_plan_treated_as_not_enabled_skips_usage(self):
        """看板无法解析套餐金额时视为未开通，不记 E_PLAN_AMOUNT、不拉 usage。"""
        item = bi_sync.SnapshotAccount(
            account=Account(
                email="cursor@example.com",
                imap_password="pw",
                imap_host="imap.feishu.cn",
                imap_port=993,
                feishu_email="owner@example.com",
            ),
            source="db",
            is_new=False,
            feishu_email="owner@example.com",
        )

        snap = SimpleNamespace(
            errors={},
            usage_csv_text=(
                "Date,Kind,Model,Input (w/o Cache Write),Input (w/ Cache Write),"
                "Output Tokens,Total Tokens,Cost\n"
                "2026-05-13T01:00:00Z,Usage,gpt-4,1,2,3,6,$0.10\n"
            ),
            usage_events=[],
            plan={"name": "Free"},
        )

        with (
            patch.object(bi_sync.fetcher, "fetch_one", return_value=snap),
            patch.object(
                bi_sync,
                "fetch_plan_info_from_dashboard",
                return_value=PlanInfo(
                    status="not_enabled",
                    amount=None,
                    error="消费页未解析到套餐信息，按未开通处理",
                ),
            ),
        ):
            result = bi_sync._fetch_account_usage_rows(
                item,
                run_id="run1",
                biz_date="2026-05-13",
                start_ts=1,
                end_ts=2,
            )

        self.assertEqual(result.rows, [])
        self.assertEqual(result.error, "")
        self.assertEqual(result.plan_status, "not_enabled")

    def test_not_enabled_plan_skips_without_fetching_usage(self):
        item = bi_sync.SnapshotAccount(
            account=Account(
                email="cursor@example.com",
                imap_password="pw",
                imap_host="imap.feishu.cn",
                imap_port=993,
                feishu_email="owner@example.com",
            ),
            source="db",
            is_new=False,
            feishu_email="owner@example.com",
        )
        store = SimpleNamespace(update_account_plan=lambda **_kwargs: None)

        with (
            patch.object(
                bi_sync,
                "fetch_plan_info_from_dashboard",
                return_value=PlanInfo(status="not_enabled", amount=None, error="Requires a paid plan"),
            ),
            patch.object(bi_sync.fetcher, "fetch_one") as fetch_one,
            patch.object(bi_sync, "get_default_store", return_value=store),
        ):
            result = bi_sync._fetch_account_usage_rows(
                item,
                run_id="run1",
                biz_date="2026-05-13",
                start_ts=1,
                end_ts=2,
            )

        fetch_one.assert_not_called()
        self.assertEqual(result.error, "")
        self.assertEqual(result.rows, [])
        self.assertEqual(result.plan_status, "not_enabled")

    def test_plan_fetch_runtime_error_still_surfaces_as_plan_failure(self):
        item = bi_sync.SnapshotAccount(
            account=Account(
                email="cursor@example.com",
                imap_password="pw",
                imap_host="imap.feishu.cn",
                imap_port=993,
                feishu_email="owner@example.com",
            ),
            source="db",
            is_new=False,
            feishu_email="owner@example.com",
        )
        snap = SimpleNamespace(
            errors={},
            usage_csv_text=(
                "Date,Kind,Model,Input (w/o Cache Write),Input (w/ Cache Write),"
                "Output Tokens,Total Tokens,Cost\n"
                "2026-05-13T01:00:00Z,Usage,gpt-4,1,2,3,6,$0.10\n"
            ),
            usage_events=[],
        )
        store = SimpleNamespace(update_account_plan=lambda **_kwargs: None)

        with (
            patch.object(bi_sync, "SETTINGS", SimpleNamespace(bi_sync_retry_times=2)),
            patch.object(bi_sync.fetcher, "fetch_one", return_value=snap),
            patch.object(
                bi_sync,
                "fetch_plan_info_from_dashboard",
                side_effect=RuntimeError("transient dashboard failure"),
            ),
            patch.object(bi_sync, "get_default_store", return_value=store),
        ):
            result = bi_sync._fetch_account_usage_rows(
                item,
                run_id="run1",
                biz_date="2026-05-13",
                start_ts=1,
                end_ts=2,
            )

        self.assertEqual(result.rows, [])
        self.assertIn("E_PLAN_AMOUNT", result.error)

    def test_fetch_account_usage_rows_uses_dashboard_plan_scraper_not_plan_api(self):
        item = bi_sync.SnapshotAccount(
            account=Account(
                email="cursor@example.com",
                imap_password="pw",
                imap_host="imap.feishu.cn",
                imap_port=993,
                feishu_email="owner@example.com",
            ),
            source="db",
            is_new=False,
            feishu_email="owner@example.com",
        )
        seen_what = []

        def fake_fetch_one(_account, *, what, start_ts, end_ts):
            seen_what.append(tuple(what))
            return SimpleNamespace(
                errors={},
                usage_csv_text=(
                    "Date,Kind,Model,Input (w/o Cache Write),Input (w/ Cache Write),"
                    "Output Tokens,Total Tokens,Cost\n"
                    "2026-05-13T01:00:00Z,Usage,gpt-4,1,2,3,6,$0.10\n"
                ),
                usage_events=[],
            )

        with (
            patch.object(bi_sync.fetcher, "fetch_one", side_effect=fake_fetch_one),
            patch.object(
                bi_sync,
                "fetch_plan_info_from_dashboard",
                return_value=PlanInfo(status="active", amount=Decimal("200")),
            ),
        ):
            result = bi_sync._fetch_account_usage_rows(
                item,
                run_id="run1",
                biz_date="2026-05-13",
                start_ts=1,
                end_ts=2,
            )

        self.assertEqual(result.error, "")
        self.assertEqual(result.rows[0]["plan_amount"], Decimal("200"))
        self.assertEqual(seen_what, [("usage_events",)])

    def test_plan_retry_does_not_refetch_usage_csv(self):
        item = bi_sync.SnapshotAccount(
            account=Account(
                email="cursor@example.com",
                imap_password="pw",
                imap_host="imap.feishu.cn",
                imap_port=993,
                feishu_email="owner@example.com",
            ),
            source="db",
            is_new=False,
            feishu_email="owner@example.com",
        )
        snap = SimpleNamespace(
            errors={},
            usage_csv_text=(
                "Date,Kind,Model,Input (w/o Cache Write),Input (w/ Cache Write),"
                "Output Tokens,Total Tokens,Cost\n"
                "2026-05-13T01:00:00Z,Usage,gpt-4,1,2,3,6,$0.10\n"
            ),
            usage_events=[],
        )

        with (
            patch.object(bi_sync, "SETTINGS", SimpleNamespace(bi_sync_retry_times=2)),
            patch.object(bi_sync.fetcher, "fetch_one", return_value=snap) as fetch_one,
            patch.object(
                bi_sync,
                "fetch_plan_info_from_dashboard",
                side_effect=[RuntimeError("dashboard timeout"), PlanInfo(status="active", amount=Decimal("200"))],
            ),
        ):
            result = bi_sync._fetch_account_usage_rows(
                item,
                run_id="run1",
                biz_date="2026-05-13",
                start_ts=1,
                end_ts=2,
            )

        self.assertEqual(result.error, "")
        self.assertEqual(result.rows[0]["plan_amount"], Decimal("200"))
        self.assertEqual(fetch_one.call_count, 1)


if __name__ == "__main__":
    unittest.main()
