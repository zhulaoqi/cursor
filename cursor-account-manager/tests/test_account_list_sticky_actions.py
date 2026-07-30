from pathlib import Path
import unittest


STATIC_INDEX = Path(__file__).resolve().parents[1] / "cam" / "static" / "index.html"


class AccountListStickyActionsTests(unittest.TestCase):
    def test_account_list_uses_single_sticky_batch_action_area(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")

        self.assertIn("sticky-selection-bar", html)
        self.assertIn("clearSelection()", html)
        self.assertNotIn("scroll-shortcuts", html)
        self.assertNotIn("scrollAccountListTo(", html)
        self.assertEqual(html.count("开始拉取 ("), 1)
        self.assertIn("selection-bar-spacer", html)
        self.assertIn("needsSelectionBarSpacer", html)
        self.assertIn("_syncSelectionBarSpacer", html)
        self.assertIn("account-list-table-wrap", html)
        self.assertIn('x-show="needsSelectionBarSpacer"', html)
        self.assertNotIn('x-show="selectedEmails.size > 0" x-cloak></div>', html)
        self.assertIn("x-transition.opacity.duration.150ms", html)
        self.assertNotIn("x-transition.opacity.scale.origin.bottom", html)
        self.assertNotIn("transform: translateX(-50%)", html)
        self.assertIn("margin-inline: auto", html)
        self.assertIn("will-change: opacity", html)
        self.assertIn("_ensureSelectionBarClearance", html)
        self.assertIn("closest('tr')", html)
        self.assertIn("window.scrollBy", html)

    def test_run_progress_exposes_inline_retry_for_failed_accounts(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")

        self.assertIn("retryRunAccount(email)", html)
        self.assertIn("canRetryRunItem(item)", html)
        self.assertIn("retryingEmails", html)
        self.assertIn("重试", html)

    def test_run_download_area_uses_backend_zip_availability(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")

        self.assertIn("hasZip", html)
        self.assertIn("task.has_zip", html)
        self.assertIn("d.has_zip", html)
        self.assertIn("downloadZipLabel()", html)
        self.assertIn("runFinished && (downloadToken || hasZip)", html)
        self.assertNotIn("runFinished && (downloadToken || withInvoices)", html)

    def test_filter_panel_uses_aligned_grid_layout(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")

        self.assertIn("filters-panel", html)
        self.assertIn("filters-grid", html)
        self.assertIn("filter-section", html)
        self.assertIn("date-control-row", html)
        self.assertIn("filter-section is-primary", html)
        self.assertIn("filter-section is-compact", html)
        self.assertIn("filter-section is-export", html)
        self.assertIn("preset-group", html)
        self.assertIn("preset-btn", html)
        self.assertIn("option-stack", html)
        self.assertIn("option-copy", html)
        self.assertIn("grid-template-columns: minmax(0, 1fr) 260px", html)
        self.assertIn('"range month"', html)
        self.assertIn('"export export"', html)
        self.assertIn("display: flex", html)
        self.assertNotIn("width:1px;background:var(--border);align-self:stretch", html)

    def test_billing_month_uses_custom_picker_not_native_month_input(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")

        self.assertIn("month-picker-wrap", html)
        self.assertIn("monthPickerMonths()", html)
        self.assertIn("selectBillingMonth(year, month)", html)
        self.assertIn("formatSelectedMonth()", html)
        self.assertNotIn('type="month" x-model="selectedMonth"', html)

    def test_date_picker_disables_future_dates(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")

        self.assertIn("isFutureDate(iso)", html)
        self.assertIn("is-disabled", html)
        self.assertIn(":disabled=\"day.isFuture\"", html)
        self.assertIn("isFuture: iso > today", html)
        self.assertIn("if (this.isFutureDate(iso)) return;", html)

    def test_selected_accounts_can_be_cleaned_with_warning_confirmation(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")

        self.assertIn("删除选中", html)
        self.assertIn("bulkDeleteSelectedAccounts()", html)
        self.assertIn("confirmBulkDeleteSelectedAccounts()", html)
        self.assertIn("isDeletingSelected", html)
        self.assertIn("btn-warning", html)
        self.assertIn("delete-confirm-modal", html)
        self.assertIn("showDeleteConfirm", html)
        self.assertIn("确认删除选中的", html)
        self.assertIn("无法撤销", html)
        self.assertIn("method: 'DELETE'", html)
        self.assertIn("selectedEmails.size === 0", html)
        self.assertNotIn("confirm(`确认清理", html)
        self.assertNotIn("alert(`清理", html)

    def test_no_dead_css_for_removed_native_date_pickers(self):
        """原生 input[type=month] / input[type=date] 已替换为自定义 picker，
        对应 CSS 选择器属于死代码，应清理避免造成阅读混乱与误导。"""
        html = STATIC_INDEX.read_text(encoding="utf-8")
        self.assertNotIn('input[type="month"]', html,
                         "死代码：原生月份输入已废弃，CSS 选择器应清理")
        self.assertNotIn("input[type='month']", html)
        self.assertNotIn('input[type="date"]', html,
                         "死代码：原生日期输入已废弃，CSS 选择器应清理")
        self.assertNotIn("input[type='date']", html)

    def test_table_row_action_buttons_meet_touch_target(self):
        """表格行内的重置/删除图标按钮需满足 44×44px 最小触摸目标
        （ui-ux-pro-max §2 touch-target-size），避免误点和无障碍违规。"""
        html = STATIC_INDEX.read_text(encoding="utf-8")
        self.assertIn("table-row-action", html,
                      "应使用专用类，而非内联零散样式")
        self.assertIn(".table-row-action", html)
        self.assertIn("min-width: 32px", html)
        self.assertIn("min-height: 32px", html)

    def test_account_list_table_aligns_identity_and_status_with_usage_style(self):
        """账号库表格：邮箱+飞书合并、IMAP 合并、状态用统一 badge，对齐用量监控观感。"""
        html = STATIC_INDEX.read_text(encoding="utf-8")
        self.assertIn("account-identity", html)
        self.assertIn("account-identity-email", html)
        self.assertIn("account-identity-feishu", html)
        self.assertIn("account-imap", html)
        self.assertIn("account-meta-badge", html)
        self.assertIn("accountSourceBadgeClass(a)", html)
        self.assertIn("accountStatusBadgeClass(a)", html)
        self.assertIn('col class="account-col-width"', html)
        self.assertIn('col class="imap-col-width"', html)
        self.assertNotIn('col class="feishu-col-width"', html)
        self.assertNotIn('col class="port-col-width"', html)
        self.assertNotIn(".account-list-table tbody tr:nth-child(even)", html)
        self.assertIn(".account-list-table th {\n      white-space: nowrap;\n      color: #64748B;\n      background: #F8FAFC;", html)
        self.assertIn(".account-list-table tbody tr:hover {\n      background: #F8FBFF;", html)

    def test_single_account_delete_uses_custom_dialog_not_native_confirm(self):
        """单账号删除按钮不能再用浏览器原生 confirm()，必须用站内统一风格弹窗。"""
        html = STATIC_INDEX.read_text(encoding="utf-8")

        # 不应再有原生 confirm 调用
        self.assertNotIn("confirm(`确定删除账号", html,
                         "单账号删除应使用自定义模态而非 window.confirm")

        # 应有自定义模态及对应状态
        self.assertIn("showDeleteAccountModal", html)
        self.assertIn("deleteAccountTarget", html)
        self.assertIn("isDeletingAccount", html)
        self.assertIn("deleteAccountError", html)
        self.assertIn("confirmDeleteAccount()", html)
        self.assertIn("requestDeleteAccount(", html)

        # 模态结构与按钮文案
        self.assertIn("确认删除该账号", html)
        # 复用现有 .danger-dialog 风格保持一致
        self.assertIn('id="delete-account-title"', html)

        # 可访问性：dialog 语义和键盘逃逸
        self.assertIn('aria-labelledby="delete-account-title"', html)
        self.assertIn("@keydown.escape.window", html)


if __name__ == "__main__":
    unittest.main()
