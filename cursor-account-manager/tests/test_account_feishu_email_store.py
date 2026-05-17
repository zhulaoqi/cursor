import sqlite3
import tempfile
import unittest
from pathlib import Path

from cam.token_store import TokenStore


class AccountFeishuEmailStoreTests(unittest.TestCase):
    def test_init_adds_feishu_email_column_to_existing_accounts_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tokens.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE accounts (
                    email TEXT PRIMARY KEY,
                    imap_password TEXT NOT NULL DEFAULT '',
                    imap_host TEXT NOT NULL DEFAULT 'imap.feishu.cn',
                    imap_port INTEGER NOT NULL DEFAULT 993,
                    added_at INTEGER DEFAULT 0,
                    updated_at INTEGER DEFAULT 0,
                    source TEXT DEFAULT ''
                )
                """
            )
            conn.commit()
            conn.close()

            TokenStore(db_path)

            conn = sqlite3.connect(db_path)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
            conn.close()
            self.assertIn("feishu_email", cols)
            self.assertIn("plan_amount", cols)
            self.assertIn("plan_status", cols)
            self.assertIn("plan_checked_at", cols)
            self.assertIn("plan_error", cols)
            self.assertIn("plan_name", cols)
            self.assertIn("on_demand_enabled", cols)
            self.assertIn("on_demand_historical", cols)
            self.assertIn("spending_checked_at", cols)
            self.assertIn("spending_error", cols)

    def test_upsert_account_persists_and_searches_feishu_email(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TokenStore(Path(tmp) / "tokens.db")

            store.upsert_account(
                email="cursor@example.com",
                imap_password="pw",
                imap_host="imap.feishu.cn",
                imap_port=993,
                feishu_email="owner@example.com",
                source="upload",
            )

            row = store.get_account("cursor@example.com")
            self.assertEqual(row["feishu_email"], "owner@example.com")
            results = store.search_accounts("cursor", limit=10)
            self.assertEqual(results[0]["feishu_email"], "owner@example.com")

    def test_update_account_plan_persists_status_amount_and_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TokenStore(Path(tmp) / "tokens.db")
            store.upsert_account(
                email="cursor@example.com",
                imap_password="pw",
                imap_host="imap.feishu.cn",
                imap_port=993,
                feishu_email="owner@example.com",
                source="upload",
            )

            store.update_account_plan(
                email="cursor@example.com",
                plan_status="active",
                plan_amount="200",
                plan_error="",
            )

            row = store.get_account("cursor@example.com")
            self.assertEqual(row["plan_status"], "active")
            self.assertEqual(row["plan_amount"], "200")
            self.assertGreater(row["plan_checked_at"], 0)
            self.assertEqual(row["plan_error"], "")

    def test_update_account_spending_snapshot_persists_plan_name_and_on_demand(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TokenStore(Path(tmp) / "tokens.db")
            store.upsert_account(
                email="cursor@example.com",
                imap_password="pw",
                imap_host="imap.feishu.cn",
                imap_port=993,
                feishu_email="owner@example.com",
                source="upload",
            )
            store.update_account_spending_snapshot(
                email="cursor@example.com",
                plan_name="Ultra",
                on_demand_enabled=False,
                on_demand_historical=True,
                spending_error="",
            )
            row = store.get_account("cursor@example.com")
            self.assertEqual(row["plan_name"], "Ultra")
            self.assertEqual(row["on_demand_enabled"], 0)
            self.assertEqual(row["on_demand_historical"], 1)
            self.assertGreater(row["spending_checked_at"], 0)
            self.assertEqual(row["spending_error"], "")


if __name__ == "__main__":
    unittest.main()
