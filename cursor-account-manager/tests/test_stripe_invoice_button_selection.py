import unittest

from cam.exporter import (
    _is_receipt_download_text,
    _stripe_invoice_download_selectors,
)


class StripeInvoiceButtonSelectionTests(unittest.TestCase):
    def test_invoice_button_selectors_prefer_invoice_before_generic_pdf(self):
        selectors = _stripe_invoice_download_selectors()

        self.assertLess(selectors.index('button:has-text("下载账单")'), selectors.index(':text("PDF")'))
        self.assertIn('button:has-text("Download invoice")', selectors)
        self.assertNotIn('button:has-text("下载收据")', selectors)
        self.assertNotIn('button:has-text("Download receipt")', selectors)

    def test_receipt_download_text_is_excluded(self):
        self.assertTrue(_is_receipt_download_text("下载收据"))
        self.assertTrue(_is_receipt_download_text("Download receipt"))
        self.assertFalse(_is_receipt_download_text("下载账单"))
        self.assertFalse(_is_receipt_download_text("Download invoice"))


if __name__ == "__main__":
    unittest.main()
