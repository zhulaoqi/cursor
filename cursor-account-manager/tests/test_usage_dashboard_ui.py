"""用量监控看板静态界面契约测试。"""

from pathlib import Path
import unittest


HTML_PATH = Path(__file__).resolve().parents[1] / "cam" / "static" / "index.html"


class UsageDashboardUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML_PATH.read_text(encoding="utf-8")

    def test_top_navigation_exposes_usage_monitor(self):
        self.assertIn("用量监控", self.html)
        self.assertIn('@click="goUsage()"', self.html)

    def test_usage_view_contains_dashboard_content(self):
        self.assertIn("view === 'usage'", self.html)
        self.assertIn("按 Cursor 滚动账期统计", self.html)
        self.assertIn("账号与人员", self.html)
        self.assertIn("最近完整账期", self.html)

    def test_usage_dashboard_requests_expected_api(self):
        self.assertIn("fetch('/api/usage-monitor/dashboard')", self.html)

    def test_usage_levels_and_accessible_controls_are_present(self):
        for label in ("L3", "L2", "L1", "L0", "待确认"):
            self.assertIn(label, self.html)
        self.assertIn('aria-label="搜索邮箱、申请人、部门或套餐"', self.html)
        self.assertIn('aria-live="polite"', self.html)

    def test_usage_table_avoids_fixed_overflow_and_formats_cycles(self):
        """账期斜杠范围；用量进度条；数据状态原因走悬停。"""
        self.assertIn("usage-col-cycle", self.html)
        self.assertIn("usageFormatDate", self.html)
        self.assertIn("usageCycleRangeText", self.html)
        self.assertIn("${parts.start} / ${parts.end}", self.html)
        self.assertIn("usage-meter", self.html)
        self.assertIn("usage-bars", self.html)
        self.assertIn("usageSplitBars", self.html)
        self.assertIn("usageMeterWidth", self.html)
        self.assertIn("完整账期用量", self.html)
        self.assertIn("usage-col-final-usage", self.html)
        self.assertIn("数据状态", self.html)
        self.assertIn("usageDataStatusTooltip", self.html)
        self.assertIn("usage-status-badge", self.html)
        self.assertIn("usageLevelDetail", self.html)
        self.assertIn("overflow-x: auto", self.html)

    def test_usage_history_drawer_entry_and_api_path(self):
        """每行提供历史账期入口，并请求单账号 cycles 接口。"""
        self.assertIn("历史账期", self.html)
        self.assertIn("openUsageCyclesDrawer", self.html)
        self.assertIn("/api/usage-monitor/accounts/", self.html)
        self.assertIn("/cycles", self.html)
        self.assertIn("showUsageCyclesDrawer", self.html)
        self.assertIn("usage-cycles-backdrop", self.html)
        self.assertIn("暂无已结算完整账期", self.html)
        self.assertIn("usage-cycles-modal.is-open", self.html)
        self.assertIn("usage-cycles-cycle-range", self.html)
        self.assertIn("uc-col-cycle", self.html)
        self.assertIn("usage-cycles-open", self.html)

    def test_usage_guide_explains_levels_and_schedule(self):
        """页面展示等级计算、调度刷新与列表阅读说明。"""
        self.assertIn("用量监控规则说明", self.html)
        self.assertIn("浪费等级怎么算", self.html)
        self.assertIn("调度与刷新", self.html)
        self.assertIn("列表怎么读", self.html)
        self.assertIn("usageGuideOpen", self.html)
        self.assertIn("已结算完整账期", self.html)
        self.assertIn("账期结束前", self.html)

    def test_usage_toolbar_selects_use_custom_chevron(self):
        """筛选下拉隐藏系统箭头，使用统一自定义 chevron。"""
        self.assertIn(".usage-select", self.html)
        self.assertIn("appearance: none", self.html)
        self.assertIn("background-position: right 12px center", self.html)

    def test_usage_toolbar_has_plan_tier_filter(self):
        """工具栏提供套餐下拉筛选，并支持按套餐关键字搜索。"""
        self.assertIn('id="usage-plan"', self.html)
        self.assertIn("usagePlanFilter", self.html)
        self.assertIn("usagePlanTiers", self.html)
        self.assertIn("全部套餐", self.html)
        self.assertIn("搜索邮箱、申请人、部门或套餐", self.html)

    def test_usage_row_collect_button_and_api(self):
        """操作列提供行内强制采集入口，并支持多账号并行采集状态。"""
        self.assertIn("collectUsageAccount", self.html)
        self.assertIn("/collect", self.html)
        self.assertIn("usageCollecting", self.html)
        self.assertIn("采集中", self.html)
        self.assertIn("行内「采集」会向 Cursor 拉取该账号当前用量", self.html)

    def test_usage_plan_column_shows_amount_when_available(self):
        """套餐列优先展示档位 + 金额（来自账号库 plan_amount）。"""
        self.assertIn("usagePlanLabel", self.html)
        self.assertIn("plan_amount", self.html)
        self.assertIn("${tier} · ${normalized}", self.html)


if __name__ == "__main__":
    unittest.main()
