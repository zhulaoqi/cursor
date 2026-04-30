import unittest

from cam.web_server import _normalize_email, _parse_csv_bytes


class WebServerCsvNormalizationTests(unittest.TestCase):
    def test_normalize_email_trims_and_lowercases(self):
        raw = "  Cursor123@Example.COM\u200b "
        self.assertEqual(_normalize_email(raw), "cursor123@example.com")

    def test_parse_csv_supports_header_spaces_and_case(self):
        data = (
            " Email , IMAP_PASSWORD , imap_host , imap_port \n"
            " Cursor123@Example.COM , secret123 , imap.feishu.cn , 993 \n"
        ).encode("utf-8")
        rows = _parse_csv_bytes(data)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].email, "cursor123@example.com")
        self.assertEqual(rows[0].imap_password, "secret123")
        self.assertEqual(rows[0].imap_host, "imap.feishu.cn")
        self.assertEqual(rows[0].imap_port, 993)


if __name__ == "__main__":
    unittest.main()
