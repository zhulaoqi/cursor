import io
import unittest

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from cam.web_server import app


class AccountTemplateDownloadTests(unittest.TestCase):
    def test_download_excel_template_contains_required_columns(self):
        client = TestClient(app)

        res = client.get("/api/accounts/template.xlsx")

        self.assertEqual(res.status_code, 200)
        self.assertIn(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            res.headers.get("content-type", ""),
        )
        workbook = load_workbook(io.BytesIO(res.content))
        sheet = workbook.active
        headers = [cell.value for cell in sheet[1]]
        self.assertEqual(
            headers,
            ["email", "imap_password", "imap_host", "imap_port", "feishu_email"],
        )
        self.assertEqual(sheet["E2"].value, "owner@example.com")


if __name__ == "__main__":
    unittest.main()
