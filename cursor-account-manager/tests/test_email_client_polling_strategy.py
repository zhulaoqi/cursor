import email
import os
import tempfile
import unittest
from pathlib import Path

from cam import email_client
from cam.token_store import TokenStore


class EmailClientPollingStrategyTests(unittest.TestCase):
    def test_accept_old_date_when_new_arrival(self):
        # 新到达邮件（不在首轮基线）即使 Date 较旧也应允许，避免慢投递被丢弃。
        self.assertTrue(
            email_client._should_accept_by_cutoff(
                msg_ts=1000.0,
                cutoff_ts=2000.0,
                is_new_arrival=True,
            )
        )

    def test_reject_old_date_when_not_new_arrival(self):
        # 首轮基线里的旧邮件仍需按 cutoff 过滤，避免误用历史验证码。
        self.assertFalse(
            email_client._should_accept_by_cutoff(
                msg_ts=1000.0,
                cutoff_ts=2000.0,
                is_new_arrival=False,
            )
        )

    def test_search_folders_from_env(self):
        old = os.environ.get("IMAP_SEARCH_FOLDERS")
        try:
            os.environ["IMAP_SEARCH_FOLDERS"] = "INBOX, Junk, Spam, ,INBOX"
            folders = email_client._get_search_folders()
            self.assertEqual(folders, ["INBOX", "Junk", "Spam"])
        finally:
            if old is None:
                os.environ.pop("IMAP_SEARCH_FOLDERS", None)
            else:
                os.environ["IMAP_SEARCH_FOLDERS"] = old


def _mail(to: str, code: str = "449754") -> email.message.Message:
    raw = (
        "From: Cursor <no-reply@cursor.sh>\r\n"
        f"To: cursor <{to}>\r\n"
        "Subject: =?UTF-8?B?55m75b2VIEN1cnNvcg==?=\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        f"您请求登录 Cursor。您的一次性验证码是：\r\n{code}\r\n"
    ).encode()
    return email.message_from_bytes(raw)


class GroupMailboxTests(unittest.TestCase):
    def setUp(self):
        self._env = {
            key: os.environ.get(key)
            for key in ("IMAP_GROUP_MAILBOXES", "IMAP_GROUP_MAILBOX_ACCOUNTS")
        }
        os.environ.pop("IMAP_GROUP_MAILBOXES", None)
        os.environ.pop("IMAP_GROUP_MAILBOX_ACCOUNTS", None)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_only_listed_accounts_use_group_mailbox(self):
        self.assertEqual(
            email_client.group_mailbox_for("cursor119@eclicktech.com.cn"),
            "kinch.zhu@eclicktech.com.cn",
        )
        self.assertEqual(
            email_client.group_mailbox_for("cursor132@eclicktech.com.cn"),
            "kinch.zhu@eclicktech.com.cn",
        )
        # 同后缀但还没改授权码的账号，继续登录自己的邮箱
        self.assertIsNone(email_client.group_mailbox_for("cursor182@eclicktech.com.cn"))
        self.assertIsNone(email_client.group_mailbox_for("other@example.com"))

    def test_domain_wildcard_opts_in_the_whole_suffix(self):
        os.environ["IMAP_GROUP_MAILBOX_ACCOUNTS"] = "*@eclicktech.com.cn"
        self.assertEqual(
            email_client.group_mailbox_for("cursor182@eclicktech.com.cn"),
            "kinch.zhu@eclicktech.com.cn",
        )
        self.assertIsNone(email_client.group_mailbox_for("other@example.com"))

    def test_resolve_mailbox_filters_by_recipient(self):
        login, recipient = email_client._resolve_mailbox(
            "cursor119@eclicktech.com.cn", None, None
        )
        self.assertEqual(login, "kinch.zhu@eclicktech.com.cn")
        self.assertEqual(recipient, "cursor119@eclicktech.com.cn")
        login, recipient = email_client._resolve_mailbox("other@example.com", None, None)
        self.assertEqual(login, "other@example.com")
        self.assertIsNone(recipient)

    def test_addressed_to_matches_screenshot_recipient(self):
        msg = _mail("cursor182@eclicktech.com.cn")
        self.assertTrue(email_client._addressed_to(msg, "cursor182@eclicktech.com.cn"))
        self.assertFalse(email_client._addressed_to(msg, "cursor119@eclicktech.com.cn"))
        self.assertFalse(email_client._addressed_to(msg, "kinch.zhu@eclicktech.com.cn"))

    def test_extract_code_still_reads_body(self):
        msg = _mail("cursor119@eclicktech.com.cn", "449754")
        self.assertEqual(email_client._extract_code(msg.as_bytes()), "449754")

    def test_extract_code_ignores_gray_hex_before_real_code(self):
        # 线上这次把邮件样式里的 #414141 填进了登录框，真正的验证码是 593766
        raw = (
            "From: Cursor <no-reply@cursor.sh>\r\n"
            "To: cursor <cursor119@eclicktech.com.cn>\r\n"
            "Subject: =?UTF-8?B?55m75b2VIEN1cnNvcg==?=\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            "<html><body>"
            "<div style=\"color:#414141;font-size:16px\">"
            "您请求登录 Cursor。您的一次性验证码是："
            "</div>"
            "<p style=\"font-size:28px;color:#111111\">593766</p>"
            "</body></html>"
        ).encode()
        self.assertEqual(email_client._extract_code(raw), "593766")

    def test_update_imap_password_does_not_touch_other_fields(self):
        store = TokenStore(Path(tempfile.mkdtemp()) / "tokens.db")
        try:
            store.upsert_account(
                "cursor119@eclicktech.com.cn", "old-secret", "imap.feishu.cn", 993, "owner@x.com"
            )
            self.assertTrue(store.update_imap_password("Cursor119@eclicktech.com.cn", "new-secret"))
            row = store.get_account("cursor119@eclicktech.com.cn")
            self.assertEqual(row["imap_password"], "new-secret")
            self.assertEqual(row["feishu_email"], "owner@x.com")
            self.assertEqual(row["imap_host"], "imap.feishu.cn")
            self.assertFalse(store.update_imap_password("missing@x.com", "x"))
        finally:
            store.db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
