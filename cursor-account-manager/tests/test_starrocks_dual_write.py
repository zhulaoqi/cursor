"""BI 双写装载器单测。"""

import unittest
from unittest.mock import MagicMock, patch

from cam import starrocks_loader as sl


class DualStarRocksLoaderTests(unittest.TestCase):
    def test_create_bi_sync_loader_returns_dual_when_enabled(self):
        def _fake_loader(**kwargs):
            m = MagicMock()
            m.label = kwargs.get("label", "primary")
            m.host = "h"
            m.port = 9030
            m.db = kwargs.get("label", "db")
            return m

        with (
            patch.object(sl, "BI_SYNC_DUAL_WRITE", True),
            patch.object(sl, "StarRocksLoader", side_effect=_fake_loader),
        ):
            loader = sl.create_bi_sync_loader()
        self.assertIsInstance(loader, sl.DualStarRocksLoader)
        self.assertIsNotNone(loader._backup)

    def test_create_bi_sync_loader_returns_single_when_disabled(self):
        fake = MagicMock(label="primary")
        with (
            patch.object(sl, "BI_SYNC_DUAL_WRITE", False),
            patch.object(sl, "StarRocksLoader", return_value=fake),
        ):
            loader = sl.create_bi_sync_loader()
        self.assertIs(loader, fake)

    def test_dual_write_backup_failure_does_not_block_primary(self):
        primary = MagicMock()
        primary.replace_ods_rows_for_account.return_value = 3
        backup = MagicMock()
        backup.replace_ods_rows_for_account.side_effect = RuntimeError("backup down")

        dual = sl.DualStarRocksLoader.__new__(sl.DualStarRocksLoader)
        dual.primary = primary
        dual._backup = backup

        n = dual.replace_ods_rows_for_account(
            biz_date="2026-05-13",
            account_email="a@x.com",
            rows=[{"dt": "2026-05-13"}],
        )
        self.assertEqual(n, 3)
        primary.replace_ods_rows_for_account.assert_called_once()
        backup.replace_ods_rows_for_account.assert_called_once()


if __name__ == "__main__":
    unittest.main()
