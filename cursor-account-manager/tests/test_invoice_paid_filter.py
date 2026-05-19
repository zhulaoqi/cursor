import unittest

from cam.exporter import (
    _BILLING_LIST_JS,
    _BILLING_URLS,
    _BILLING_MONTH_PROBE_SELECT_JS,
    _BILLING_MONTH_REFRESH_STATE_JS,
    _BILLING_MONTH_SELECT_JS,
    _STATUS_JS,
    _billing_month_key,
    _billing_month_select_payload,
    _fetch_billing_items_in_ctx,
    _filter_paid_billing_items,
    _month_distance_descending,
)

_BILLING_PARSE_SCRIPTS = frozenset({_STATUS_JS, _BILLING_LIST_JS})


class InvoicePaidFilterTests(unittest.IsolatedAsyncioTestCase):
    def test_keeps_paid_and_refunded_but_rejects_unpaid(self):
        """Stripe 的 'Refunded' 表示已支付后再退款，账单 PDF 真实存在，
        语义上属于已支付，应一并保留并下载。
        但 open/unpaid/void/draft/uncollectible 等真未支付状态必须排除。
        """
        items = [
            ("https://invoice.stripe.com/paid", "paid"),
            ("https://invoice.stripe.com/open", "open"),
            ("https://invoice.stripe.com/unpaid-en", "unpaid"),
            ("https://invoice.stripe.com/unpaid", "未支付"),
            ("https://invoice.stripe.com/cn-paid", "已支付"),
            ("https://invoice.stripe.com/refunded", "refunded"),
            ("https://invoice.stripe.com/cn-refunded", "已退款"),
            ("https://invoice.stripe.com/refunded-with-amount", "Refunded (16.60 USD)"),
            ("https://invoice.stripe.com/void", "void"),
            ("https://invoice.stripe.com/draft", "draft"),
        ]

        self.assertEqual(
            _filter_paid_billing_items(items),
            [
                ("https://invoice.stripe.com/paid", "paid"),
                ("https://invoice.stripe.com/cn-paid", "paid"),
                ("https://invoice.stripe.com/refunded", "refunded"),
                ("https://invoice.stripe.com/cn-refunded", "refunded"),
                ("https://invoice.stripe.com/refunded-with-amount", "refunded"),
            ],
        )

    def test_keeps_only_paid_invoice_rows_for_selected_billing_month(self):
        items = [
            ("https://invoice.stripe.com/feb", "paid", "2026年2月21日"),
            ("https://invoice.stripe.com/mar", "paid", "March 21, 2026"),
            ("https://invoice.stripe.com/apr", "paid", "2026-04-21"),
        ]

        self.assertEqual(
            _filter_paid_billing_items(items, invoice_month="2026-02"),
            [("https://invoice.stripe.com/feb", "paid")],
        )

    def test_deduplicates_paid_invoice_urls(self):
        items = [
            ("https://invoice.stripe.com/i/acct/test/", "paid"),
            (" https://invoice.stripe.com/i/acct/test ", "已支付"),
        ]

        self.assertEqual(
            _filter_paid_billing_items(items),
            [("https://invoice.stripe.com/i/acct/test/", "paid")],
        )

    def test_billing_month_key_accepts_common_date_formats(self):
        self.assertEqual(_billing_month_key("2026-02"), "2026-02")
        self.assertEqual(_billing_month_key("2026.03"), "2026-03")
        self.assertEqual(_billing_month_key("2026年2月21日"), "2026-02")
        self.assertEqual(_billing_month_key("2026年3月26日"), "2026-03")
        self.assertEqual(_billing_month_key("March 21, 2026"), "2026-03")
        self.assertEqual(_billing_month_key("Mar 21, 2026"), "2026-03")

    def test_billing_month_select_payload_matches_cursor_month_labels(self):
        self.assertEqual(
            _billing_month_select_payload("2026-01"),
            {
                "value": "2026-01",
                "year": 2026,
                "month": 1,
                "labels": ["2026年1月", "2026年01月", "2026-01", "Jan 2026", "January 2026"],
            },
        )

    def test_billing_month_select_script_handles_radix_open_state(self):
        self.assertIn("data-state", _BILLING_MONTH_SELECT_JS)
        self.assertIn("aria-expanded", _BILLING_MONTH_SELECT_JS)
        self.assertIn("data-radix-collection-item", _BILLING_MONTH_SELECT_JS)
        self.assertIn("!isOpen", _BILLING_MONTH_SELECT_JS)

    def test_billing_month_probe_select_opens_trigger_and_reads_portal_options(self):
        self.assertIn("pointerdown", _BILLING_MONTH_PROBE_SELECT_JS)
        self.assertIn("optionTexts", _BILLING_MONTH_PROBE_SELECT_JS)
        self.assertIn("[data-radix-collection-item]", _BILLING_MONTH_PROBE_SELECT_JS)
        self.assertIn("triggerText", _BILLING_MONTH_PROBE_SELECT_JS)
        self.assertIn("matched portal option", _BILLING_MONTH_PROBE_SELECT_JS)

    def test_billing_month_refresh_state_rejects_stale_invoice_rows(self):
        self.assertIn("staleRowDates", _BILLING_MONTH_REFRESH_STATE_JS)
        self.assertIn("targetRowDates", _BILLING_MONTH_REFRESH_STATE_JS)
        self.assertIn("selectedIndicator", _BILLING_MONTH_REFRESH_STATE_JS)
        self.assertIn("ready", _BILLING_MONTH_REFRESH_STATE_JS)

    def test_billing_pages_dashboard_tried_first_for_speed(self):
        self.assertEqual(_BILLING_URLS[0], "https://cursor.com/dashboard/billing",
                         "dashboard/billing 应首先尝试，因其月份控件可靠")
        self.assertIn("https://cursor.com/settings/billing", _BILLING_URLS,
                      "settings/billing 应作为备用保留")
        self.assertNotIn("https://cursor.com/cn/dashboard/billing", _BILLING_URLS)

    def test_month_distance_for_keyboard_dropdown_fallback(self):
        self.assertEqual(_month_distance_descending("2026年4月", "2026-02"), 2)
        self.assertEqual(_month_distance_descending("2026-04", "2026年4月"), 0)
        self.assertEqual(_month_distance_descending("2026年2月", "2026-04"), -2)

    async def test_fetch_billing_items_selects_requested_month_before_parsing_rows(self):
        """月份选择必须在解析 rows 之前发生，且选择失败时跳过该 URL 不解析。"""
        from cam import exporter

        class FakePage:
            def __init__(self):
                self.calls = []

            async def goto(self, url, wait_until=None, timeout=None):
                self.calls.append(("goto", url))

            async def wait_for_selector(self, selector, timeout=None):
                self.calls.append(("wait_for_selector", selector))

            async def wait_for_function(self, script, *args, timeout=None):
                self.calls.append(("wait_for_function", script[:40]))

            async def wait_for_timeout(self, timeout):
                self.calls.append(("wait_for_timeout", timeout))

            async def evaluate(self, script, *args):
                if script in _BILLING_PARSE_SCRIPTS:
                    self.calls.append(("parse_rows", None))
                    return [{
                        "url": "https://invoice.stripe.com/i/test",
                        "status": "Paid",
                        "date": "2026年1月30日",
                        "description": "",
                        "amountText": "10.00 USD",
                    }]
                raise AssertionError(f"unexpected script: {script[:30]}")

        page = FakePage()
        select_calls: list = []

        async def fake_select(_page, invoice_month):
            select_calls.append(invoice_month)
            return True

        original = exporter._select_billing_month_in_ctx
        exporter._select_billing_month_in_ctx = fake_select
        try:
            items = await _fetch_billing_items_in_ctx(page, invoice_month="2026-01")
        finally:
            exporter._select_billing_month_in_ctx = original

        self.assertEqual(items, [("https://invoice.stripe.com/i/test", "paid", "2026年1月30日")])
        self.assertEqual(select_calls, ["2026-01"])
        # 解析 rows 必须发生在月份选择之后
        parse_idx = page.calls.index(("parse_rows", None))
        # 任何 goto 都应在 parse_rows 之前
        for i, c in enumerate(page.calls[:parse_idx]):
            self.assertNotEqual(c[0], "parse_rows", "rows 解析早于月份选择")

    async def test_fetch_billing_items_skips_url_when_month_select_fails(self):
        """当 _select_billing_month_in_ctx 返回 False 时，不应解析当前 URL 的列表。"""
        from cam import exporter

        class FakePage:
            def __init__(self):
                self.parsed = False

            async def goto(self, url, wait_until=None, timeout=None):
                pass

            async def wait_for_selector(self, selector, timeout=None):
                pass

            async def wait_for_function(self, script, *args, timeout=None):
                pass

            async def wait_for_timeout(self, timeout):
                pass

            async def evaluate(self, script, *args):
                if script in _BILLING_PARSE_SCRIPTS:
                    self.parsed = True
                    return []
                raise AssertionError(f"unexpected script: {script[:30]}")

        page = FakePage()

        async def fake_select(_page, invoice_month):
            return False

        original = exporter._select_billing_month_in_ctx
        exporter._select_billing_month_in_ctx = fake_select
        try:
            items = await _fetch_billing_items_in_ctx(page, invoice_month="2026-01")
        finally:
            exporter._select_billing_month_in_ctx = original

        self.assertEqual(items, [])
        self.assertFalse(page.parsed, "月份选择失败后不应解析行")

    def test_playwright_skips_trigger_click_when_dropdown_already_open(self):
        """probe JS 用合成事件可能已打开下拉。若直接 await trigger.click()
        会 toggle 关闭，导致 Radix portal 选项消失。
        修复后必须先用 _BILLING_MONTH_OPTIONS_VISIBLE_JS 探测 portal 中
        是否已存在目标月份的可见选项；若已可见则跳过 trigger click 直接
        点击选项；若不可见再 click trigger 打开。"""
        import inspect
        from cam import exporter
        self.assertTrue(
            hasattr(exporter, "_BILLING_MONTH_OPTIONS_VISIBLE_JS"),
            "应有专门的 JS 探测目标月份选项是否已可见",
        )
        probe_js = exporter._BILLING_MONTH_OPTIONS_VISIBLE_JS
        self.assertIn('[role="option"]', probe_js)
        self.assertIn('[data-radix-collection-item]', probe_js)
        self.assertIn("labels", probe_js)

        fn_src = inspect.getsource(exporter._select_billing_month_via_playwright)
        self.assertIn("_BILLING_MONTH_OPTIONS_VISIBLE_JS", fn_src,
                      "_select_billing_month_via_playwright 必须使用该探测脚本")
        # 关键契约：当选项已可见时，要跳过 trigger click
        self.assertRegex(
            fn_src,
            r"options_already_visible|already_open|dropdown_open",
            "应有变量明确表达 '下拉已打开/选项已可见' 状态",
        )

    def test_playwright_uses_strict_short_month_trigger_filter(self):
        """新方案核心契约：用 ^YYYY年M月$ 精确正则 + 排除 'Cycle Starting' 噪声词，
        避免误命中订阅周期管理弹窗等非 Invoices 过滤器的下拉。"""
        import inspect
        from cam import exporter
        fn_src = inspect.getsource(exporter._select_billing_month_via_playwright)
        self.assertIn("_INVOICE_TRIGGER_EXACT_MONTH_RE", fn_src,
                      "必须使用精确月份正则匹配 trigger")
        self.assertIn("_CYCLE_OR_NOISE_RE", fn_src,
                      "必须排除 Cycle Starting / Cancel 等噪声词")
        self.assertIn("has_not_text", fn_src,
                      "选项点击需用 has_not_text 过滤掉噪声选项")

        regex_pattern = exporter._INVOICE_TRIGGER_EXACT_MONTH_RE
        self.assertIsNotNone(regex_pattern.match("2026年1月"))
        self.assertIsNotNone(regex_pattern.match("2026年01月"))
        self.assertIsNone(regex_pattern.match("Cycle Starting 2026年1月22日"),
                          "不应匹配带 Cycle Starting 前缀的文本")
        self.assertIsNone(regex_pattern.match("2026年1月 选择月份"),
                          "不应匹配带额外文字的文本")

        cycle_re = exporter._CYCLE_OR_NOISE_RE
        self.assertIsNotNone(cycle_re.search("Cycle Starting 2026年4月21日"))
        self.assertIsNotNone(cycle_re.search("Adjust plan"))
        self.assertIsNotNone(cycle_re.search("Manage in Stripe"))
        self.assertIsNone(cycle_re.search("2026年1月"))

    def test_playwright_always_clicks_option_probe_only_discovers(self):
        """probe JS 只负责诊断收集，不再合成点击；点击全部由 Playwright 完成。"""
        import inspect
        from cam import exporter
        probe_js = exporter._BILLING_MONTH_PROBE_SELECT_JS
        self.assertNotIn("firePointer(option.el)", probe_js,
                         "probe JS 不应合成点击选项")

    def test_fetch_billing_items_waits_for_billing_page_ready(self):
        """账单页解析前必须先等核心区域 ready，不能被导航按钮提前误判。"""
        import inspect
        from cam import exporter

        fn_src = inspect.getsource(exporter._fetch_billing_list_in_ctx)
        self.assertIn("_wait_billing_page_ready", fn_src)
        self.assertNotIn('wait_for_selector("button, a[href]"', fn_src)

    def test_download_invoices_all_uses_single_browser_multi_context(self):
        """并发模型契约：单 async_playwright + 单 browser + semaphore 调度账号任务。"""
        import inspect
        from cam import exporter

        fn_src = inspect.getsource(exporter._download_invoices_all)
        self.assertIn("async_playwright", fn_src)
        self.assertIn("asyncio.Semaphore", fn_src)
        self.assertIn("asyncio.create_task", fn_src)
        self.assertIn("await browser.close()", fn_src)
        self.assertIn("INVOICE_ACTIVE_CONTEXT_LIMIT", fn_src)
        self.assertIn("active_limit_cfg", fn_src)
        self.assertNotIn("ThreadPoolExecutor", fn_src)
        self.assertNotIn("pool.submit", fn_src)

    def test_download_account_all_pdfs_supports_external_browser_reuse(self):
        """下载函数必须支持复用外部 browser，避免每账号重复 launch。"""
        import inspect
        from cam import exporter

        fn_src = inspect.getsource(exporter._download_account_all_pdfs)
        self.assertIn("browser=None", fn_src)
        self.assertIn("if browser is not None", fn_src)
        self.assertIn("return await _run_with_browser(browser)", fn_src)
        self.assertIn("local_browser = await pw.chromium.launch", fn_src)


if __name__ == "__main__":
    unittest.main()
