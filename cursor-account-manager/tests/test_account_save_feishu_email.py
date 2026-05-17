import unittest

from fastapi import HTTPException

from cam.web_server import AccountRow, SaveAccountsRequest, save_accounts_api


class AccountSaveFeishuEmailTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_accounts_rejects_missing_feishu_email(self):
        req = SaveAccountsRequest(
            accounts=[
                AccountRow(
                    email="cursor@example.com",
                    imap_password="pw",
                    feishu_email="",
                )
            ],
            overwrite=True,
            source="manual",
        )

        with self.assertRaises(HTTPException) as ctx:
            await save_accounts_api(req)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("缺少飞书邮箱", str(ctx.exception.detail))


if __name__ == "__main__":
    unittest.main()
