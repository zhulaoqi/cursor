import sys
import types
import unittest
from decimal import Decimal

sys.modules.setdefault("requests", types.ModuleType("requests"))
patchright_mod = types.ModuleType("patchright")
patchright_async_api = types.ModuleType("patchright.async_api")
patchright_async_api.async_playwright = lambda: None
sys.modules.setdefault("patchright", patchright_mod)
sys.modules.setdefault("patchright.async_api", patchright_async_api)

from cam import plan_scraper


class PlanScraperTests(unittest.TestCase):
    def test_plan_page_urls_include_spending_page_first(self):
        self.assertEqual(
            plan_scraper._plan_page_urls(),
            [
                "https://cursor.com/cn/dashboard/spending",
                "https://cursor.com/en/dashboard/spending",
            ],
        )

    def test_extract_current_plan_amount_prefers_value_near_current_plan(self):
        text = "Current Plan Team $200 / month Included usage 40000 credits"

        self.assertEqual(
            plan_scraper.extract_current_plan_amount_from_text(text),
            Decimal("200"),
        )

    def test_extract_current_plan_amount_from_current_plan_card_html(self):
        html = """
        <div class="dashboard-sections">
          <div class="text-base font-medium uppercase tracking-wide text-tertiary mb-2">Current Plan</div>
          <div class="flex items-baseline gap-2 mb-1">
            <p class="text-lg font-semibold text-primary">Ultra</p>
            <p class="text-base text-secondary">$200/mo</p>
          </div>
          <p>Resets on 5月20日 (7 days)</p>
        </div>
        """

        self.assertEqual(
            plan_scraper.extract_current_plan_amount_from_text(html),
            Decimal("200"),
        )

    def test_extract_current_plan_amount_requires_current_plan_label(self):
        text = "Included usage 40000 credits"

        self.assertIsNone(plan_scraper.extract_current_plan_amount_from_text(text))

    def test_extract_current_plan_info_marks_free_account_not_enabled(self):
        text = """
        On-Demand Usage
        Enable on-demand usage to go beyond your plan's included usage.
        Requires a paid plan.
        Upgrade to Pro
        Free
        """

        info = plan_scraper.extract_current_plan_info_from_text(text)

        self.assertEqual(info.status, "not_enabled")
        self.assertIsNone(info.amount)
        self.assertIn("paid plan", info.error)

    def test_extract_current_plan_info_marks_active_account(self):
        info = plan_scraper.extract_current_plan_info_from_text("Current Plan Ultra $200/mo")

        self.assertEqual(info.status, "active")
        self.assertEqual(info.amount, Decimal("200"))
        self.assertEqual(info.error, "")

    def test_format_plan_diagnostics_compacts_page_context(self):
        diagnostics = plan_scraper._format_plan_diagnostics([
            {
                "target_url": "https://cursor.com/cn/dashboard/spending",
                "final_url": "https://cursor.com/cn/login",
                "status": 200,
                "text_snippet": "Sign in to continue",
            }
        ])

        self.assertIn("target_url=https://cursor.com/cn/dashboard/spending", diagnostics)
        self.assertIn("final_url=https://cursor.com/cn/login", diagnostics)
        self.assertIn("status=200", diagnostics)
        self.assertIn("text_snippet='Sign in to continue'", diagnostics)

    def test_extract_plan_name_from_spending_text_finds_ultra(self):
        text = "CURRENT PLAN Ultra $200/mo Resets on May 20"
        self.assertEqual(plan_scraper.extract_plan_name_from_spending_text(text), "Ultra")

    def test_extract_on_demand_enabled_disabled(self):
        text = "On-Demand Usage On-demand spending is currently disabled Monthly Limit Disabled"
        self.assertIs(plan_scraper.extract_on_demand_enabled_from_text(text), False)

    def test_extract_on_demand_enabled_enabled(self):
        text = "Something On-demand spending is currently Enabled more"
        self.assertIs(plan_scraper.extract_on_demand_enabled_from_text(text), True)

    def test_extract_on_demand_space_not_hyphen(self):
        text = "On-Demand Usage On demand spending is currently disabled"
        self.assertIs(plan_scraper.extract_on_demand_enabled_from_text(text), False)

    def test_extract_on_demand_from_monthly_limit_disabled(self):
        text = (
            "On-Demand Spending On-demand spending is currently disabled $125 "
            "Monthly Limit Set a fixed amount or make it unlimited. Disabled Save"
        )
        self.assertIs(plan_scraper.extract_on_demand_enabled_from_text(text), False)

    def test_extract_on_demand_from_monthly_limit_enabled(self):
        text = "Monthly Limit foo bar Enabled Save"
        self.assertIs(plan_scraper.extract_on_demand_enabled_from_text(text), True)

    def test_extract_on_demand_monthly_limit_without_sentence(self):
        text = "Some header Monthly Limit Set a fixed amount or make it unlimited. Disabled Save"
        self.assertIs(plan_scraper.extract_on_demand_enabled_from_text(text), False)

    def test_run_playwright_coroutine_in_sync_context(self):
        async def _coro():
            return 42

        self.assertEqual(plan_scraper._run_playwright_coroutine(_coro()), 42)

    def test_plan_snapshot_from_spending_full_text_active(self):
        text = "Current Plan Ultra $200 / month On-Demand"
        snap = plan_scraper.plan_snapshot_from_spending_full_text(text)
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertEqual(snap.status, "active")
        self.assertEqual(snap.amount, Decimal("200"))

    def test_plan_snapshot_from_spending_full_text_not_enabled(self):
        text = "Current Plan Free upgrade to pro requires a paid plan"
        snap = plan_scraper.plan_snapshot_from_spending_full_text(text)
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertEqual(snap.status, "not_enabled")

    def test_plan_snapshot_from_spending_full_text_skips_ambiguous(self):
        text = "On-Demand Usage only no current plan block"
        self.assertIsNone(plan_scraper.plan_snapshot_from_spending_full_text(text))

    def test_parse_on_demand_historical_when_disabled_but_dollar(self):
        text = (
            "On-Demand Spending On-demand spending is currently disabled $135.72 "
            "Monthly Limit Set a fixed amount or make it unlimited. Disabled Save"
        )
        od = plan_scraper.parse_on_demand_panel_from_text(text)
        self.assertIs(od.currently_enabled, False)
        self.assertTrue(od.had_historical_spend)
        self.assertEqual(od.spend_amount, Decimal("135.72"))

    def test_parse_on_demand_monthly_limit_enabled_overrides_sentence(self):
        text = (
            "On-demand spending is currently disabled $10 "
            "Monthly Limit Set a fixed amount or make it unlimited. Enabled Save"
        )
        od = plan_scraper.parse_on_demand_panel_from_text(text)
        self.assertIs(od.currently_enabled, True)
        self.assertFalse(od.had_historical_spend)

    def test_monthly_limit_off_token_means_disabled(self):
        text = "Monthly Limit Set a fixed amount or make it unlimited Off Save"
        self.assertIs(plan_scraper.extract_on_demand_enabled_from_text(text), False)

    def test_describe_on_demand_parse_includes_tokens(self):
        text = "Monthly Limit foo Disabled Save"
        d = plan_scraper.describe_on_demand_parse_for_log(text)
        self.assertIn("ml_tokens", d)
        self.assertIn("disabled", d)

    def test_monthly_limit_fixed_option_means_enabled(self):
        text = "Monthly Limit Set a fixed amount or make it unlimited. Fixed Save"
        self.assertIs(plan_scraper.extract_on_demand_enabled_from_text(text), True)

    def test_monthly_limit_unlimited_option_means_enabled(self):
        text = "Monthly Limit Set a fixed amount or make it unlimited. Unlimited Save"
        self.assertIs(plan_scraper.extract_on_demand_enabled_from_text(text), True)

    def test_extract_on_demand_relaxed_currently_linebreak(self):
        text = "Heading On-demand spending extra words currently disabled footer"
        self.assertIs(plan_scraper.extract_on_demand_enabled_from_text(text), False)


if __name__ == "__main__":
    unittest.main()
