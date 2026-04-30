import os
import unittest

from cam import email_client


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


if __name__ == "__main__":
    unittest.main()
