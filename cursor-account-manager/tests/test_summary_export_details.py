import datetime
import unittest

from cam.web_server import (
    _fetch_targets_for_run,
    _has_zip_output_requested,
    _parse_date_range_to_utc_ts,
    _should_mark_accounts_done_after_export,
)


class SummaryExportDetailsTests(unittest.TestCase):
    def test_date_range_uses_beijing_day_boundaries(self):
        start_ts, end_ts = _parse_date_range_to_utc_ts("2026-04-20", "2026-04-26")

        self.assertEqual(
            datetime.datetime.fromtimestamp(start_ts, datetime.timezone.utc).isoformat(),
            "2026-04-19T16:00:00+00:00",
        )
        self.assertEqual(
            datetime.datetime.fromtimestamp(end_ts, datetime.timezone.utc).isoformat(),
            "2026-04-26T15:59:59+00:00",
        )

    def test_fetch_targets_skip_api_data_when_only_invoices_are_enabled(self):
        targets = _fetch_targets_for_run(with_summary=False, with_invoices=True, with_raw=False)

        self.assertEqual(targets, ())

    def test_fetch_targets_skip_invoices_when_only_summary_is_enabled(self):
        targets = _fetch_targets_for_run(with_summary=True, with_invoices=False, with_raw=False)

        self.assertIn("usage_events", targets)
        self.assertNotIn("invoices", targets)

    def test_fetch_targets_include_raw_api_dump_when_raw_is_enabled(self):
        targets = _fetch_targets_for_run(with_summary=False, with_invoices=False, with_raw=True)

        self.assertIn("usage_events", targets)
        self.assertNotIn("invoices", targets)

    def test_fetch_targets_skip_usage_events_when_summary_is_disabled(self):
        targets = _fetch_targets_for_run(with_summary=False, with_invoices=True, with_raw=False)

        self.assertNotIn("usage_events", targets)

    def test_fetch_targets_include_usage_events_when_summary_is_enabled(self):
        targets = _fetch_targets_for_run(with_summary=True, with_invoices=False, with_raw=False)

        self.assertIn("usage_events", targets)

    def test_web_marks_accounts_done_when_invoice_stage_is_disabled(self):
        self.assertTrue(_should_mark_accounts_done_after_export(with_invoices=False))

    def test_web_does_not_double_mark_accounts_done_when_invoice_stage_runs(self):
        self.assertFalse(_should_mark_accounts_done_after_export(with_invoices=True))

    def test_zip_output_is_available_for_any_requested_export_file(self):
        self.assertTrue(_has_zip_output_requested(with_summary=True, with_invoices=False, with_raw=False))
        self.assertTrue(_has_zip_output_requested(with_summary=False, with_invoices=True, with_raw=False))
        self.assertTrue(_has_zip_output_requested(with_summary=False, with_invoices=False, with_raw=True))
        self.assertFalse(_has_zip_output_requested(with_summary=False, with_invoices=False, with_raw=False))


if __name__ == "__main__":
    unittest.main()
