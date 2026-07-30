import unittest

from cam.config import SETTINGS
from cam.usage_snapshot_models import AccountMappingResult, MonitoredAccount
from cam.usage_snapshot_refresh import AccountResolver


class FakeUsageStore:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def list_monitor_accounts(self):
        self.calls += 1
        return list(self.rows)


class FakeTokenStore:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def list_accounts(self):
        self.calls += 1
        return list(self.rows)


def mysql_row(email, applicant="申请人", department="研发部"):
    return {
        "id": 1,
        "email": email,
        "applicant": applicant,
        "department": department,
    }


def local_row(
    email,
    *,
    password="imap-secret",
    host="imap.example.com",
    port=1993,
    feishu_email="owner@example.com",
):
    return {
        "email": email,
        "imap_password": password,
        "imap_host": host,
        "imap_port": port,
        "feishu_email": feishu_email,
        "plan_status": "active",
        "on_demand_enabled": 1,
    }


class AccountResolverTests(unittest.TestCase):
    def _resolve(self, mysql_rows, local_rows):
        usage_store = FakeUsageStore(mysql_rows)
        token_store = FakeTokenStore(local_rows)
        result = AccountResolver(usage_store, token_store).resolve()
        self.assertEqual(usage_store.calls, 1)
        self.assertEqual(token_store.calls, 1)
        return result

    def test_intersection_builds_monitored_account_from_local_fields(self):
        result = self._resolve(
            [mysql_row("user@example.com", "张三", "平台部")],
            [
                local_row(
                    "user@example.com",
                    password="pw",
                    host="imap.local.test",
                    port=2993,
                    feishu_email="zhangsan@example.com",
                )
            ],
        )

        self.assertIsInstance(result, AccountMappingResult)
        self.assertIsInstance(result.collectable_accounts, tuple)
        self.assertEqual(len(result.collectable_accounts), 1)
        monitored = result.collectable_accounts[0]
        self.assertIsInstance(monitored, MonitoredAccount)
        self.assertEqual(monitored.applicant, "张三")
        self.assertEqual(monitored.department, "平台部")
        self.assertEqual(monitored.account.email, "user@example.com")
        self.assertEqual(monitored.account.imap_password, "pw")
        self.assertEqual(monitored.account.imap_host, "imap.local.test")
        self.assertEqual(monitored.account.imap_port, 2993)
        self.assertEqual(
            monitored.account.feishu_email,
            "zhangsan@example.com",
        )

    def test_normalizes_email_and_sorts_both_difference_sets(self):
        result = self._resolve(
            [
                mysql_row(" Shared@Example.com "),
                mysql_row(" Z-Missing@example.com "),
                mysql_row("a-missing@example.com"),
            ],
            [
                local_row("shared@example.com"),
                local_row(" Z-Orphan@example.com "),
                local_row("a-orphan@example.com"),
            ],
        )

        self.assertEqual(
            tuple(
                item.account.email
                for item in result.collectable_accounts
            ),
            ("shared@example.com",),
        )
        self.assertEqual(
            result.not_collectable_emails,
            ("a-missing@example.com", "z-missing@example.com"),
        )
        self.assertEqual(
            result.orphan_local_emails,
            ("a-orphan@example.com", "z-orphan@example.com"),
        )
        self.assertIsInstance(result.not_collectable_emails, tuple)
        self.assertIsInstance(result.orphan_local_emails, tuple)

    def test_collectable_accounts_are_sorted_by_normalized_email(self):
        result = self._resolve(
            [mysql_row("z@example.com"), mysql_row("a@example.com")],
            [local_row("z@example.com"), local_row("a@example.com")],
        )

        self.assertEqual(
            tuple(item.account.email for item in result.collectable_accounts),
            ("a@example.com", "z@example.com"),
        )

    def test_mysql_duplicate_after_normalization_is_rejected(self):
        secret = "should-not-leak"

        with self.assertRaises(ValueError) as caught:
            self._resolve(
                [
                    mysql_row("User@example.com"),
                    mysql_row(" user@example.com "),
                ],
                [local_row("user@example.com", password=secret)],
            )

        self.assertIn("MySQL", str(caught.exception))
        self.assertNotIn(secret, str(caught.exception))

    def test_local_duplicate_after_normalization_is_rejected_without_password(self):
        secret = "top-secret-password"

        with self.assertRaises(ValueError) as caught:
            self._resolve(
                [mysql_row("user@example.com")],
                [
                    local_row("User@example.com", password=secret),
                    local_row(" user@example.com ", password="other-secret"),
                ],
            )

        self.assertIn("SQLite", str(caught.exception))
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn("other-secret", str(caught.exception))

    def test_empty_mysql_returns_all_local_accounts_as_orphans(self):
        result = self._resolve(
            [],
            [local_row("b@example.com"), local_row("a@example.com")],
        )

        self.assertEqual(result.collectable_accounts, ())
        self.assertEqual(result.not_collectable_emails, ())
        self.assertEqual(
            result.orphan_local_emails,
            ("a@example.com", "b@example.com"),
        )

    def test_empty_local_returns_all_mysql_accounts_as_not_collectable(self):
        result = self._resolve(
            [mysql_row("b@example.com"), mysql_row("a@example.com")],
            [],
        )

        self.assertEqual(result.collectable_accounts, ())
        self.assertEqual(
            result.not_collectable_emails,
            ("a@example.com", "b@example.com"),
        )
        self.assertEqual(result.orphan_local_emails, ())

    def test_none_personnel_fields_are_converted_to_empty_strings(self):
        result = self._resolve(
            [mysql_row("user@example.com", None, None)],
            [local_row("user@example.com")],
        )

        monitored = result.collectable_accounts[0]
        self.assertEqual(monitored.applicant, "")
        self.assertEqual(monitored.department, "")

    def test_missing_optional_local_fields_use_existing_defaults(self):
        result = self._resolve(
            [mysql_row("user@example.com")],
            [
                {
                    "email": "user@example.com",
                    "imap_password": "pw",
                    "imap_host": None,
                    "imap_port": None,
                    "feishu_email": None,
                }
            ],
        )

        account = result.collectable_accounts[0].account
        self.assertEqual(account.imap_host, SETTINGS.default_imap_host)
        self.assertEqual(account.imap_port, SETTINGS.default_imap_port)
        self.assertEqual(account.feishu_email, "")

    def test_missing_imap_port_uses_existing_default(self):
        local = local_row("user@example.com")
        local.pop("imap_port")

        result = self._resolve(
            [mysql_row("user@example.com")],
            [local],
        )

        self.assertEqual(
            result.collectable_accounts[0].account.imap_port,
            SETTINGS.default_imap_port,
        )

    def test_valid_decimal_string_imap_port_is_converted(self):
        result = self._resolve(
            [mysql_row("user@example.com")],
            [local_row("user@example.com", port="1993")],
        )

        self.assertEqual(
            result.collectable_accounts[0].account.imap_port,
            1993,
        )

    def test_invalid_imap_ports_are_rejected_without_password_leak(self):
        secret = "imap-password-must-not-leak"
        invalid_ports = (
            0,
            "0",
            True,
            False,
            "not-a-number",
            "1.5",
            1.5,
            -1,
            "-1",
            65536,
            "65536",
            " 993 ",
        )

        for port in invalid_ports:
            with self.subTest(port=port):
                with self.assertRaises(ValueError) as caught:
                    self._resolve(
                        [mysql_row("user@example.com")],
                        [
                            local_row(
                                "user@example.com",
                                password=secret,
                                port=port,
                            )
                        ],
                    )
                self.assertNotIn(secret, str(caught.exception))

    def test_resolver_does_not_query_nonexistent_enable_fields(self):
        mysql = mysql_row("user@example.com")
        local = local_row("user@example.com")
        mysql.pop("id")
        local.pop("plan_status")
        local.pop("on_demand_enabled")

        result = self._resolve([mysql], [local])

        self.assertEqual(
            result.collectable_accounts[0].account.email,
            "user@example.com",
        )


if __name__ == "__main__":
    unittest.main()
