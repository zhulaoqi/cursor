"""用量运维 CLI 的参数与服务委派测试。"""

from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from click.testing import CliRunner

from cam.cli import cli
from cam.usage_snapshot_refresh import UsageRunSummary


class UsageCliTests(unittest.TestCase):
    """只验证 CLI 参数和输出，不重复测试服务层业务逻辑。"""

    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_usage_snapshot_rejects_all_and_email_together(self) -> None:
        """--all 和 --email 必须互斥。"""
        result = self.runner.invoke(
            cli,
            [
                "usage-snapshot",
                "--all",
                "--email",
                "user@example.com",
                "--type",
                "periodic",
            ],
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("不能同时", result.output)

    @patch("cam.cli.run_usage_periodic")
    def test_usage_snapshot_passes_selected_emails_to_service(
        self, periodic: Mock
    ) -> None:
        """多个 --email 原样传给 periodic 服务过滤账号。"""
        periodic.return_value = UsageRunSummary(success=2)

        result = self.runner.invoke(
            cli,
            [
                "usage-snapshot",
                "--email",
                "one@example.com",
                "--email",
                "two@example.com",
                "--type",
                "periodic",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        periodic.assert_called_once_with(
            emails=("one@example.com", "two@example.com")
        )

    @patch("cam.cli.run_usage_pre_reset_due")
    def test_pre_reset_dry_run_only_delegates_dry_run(
        self, pre_reset_due: Mock
    ) -> None:
        """dry-run 只调用服务预览，不在 CLI 发起写入。"""
        pre_reset_due.return_value = UsageRunSummary(
            dry_run_items=({"email": "user@example.com"},)
        )

        result = self.runner.invoke(cli, ["usage-pre-reset-due", "--dry-run"])

        self.assertEqual(result.exit_code, 0, result.output)
        pre_reset_due.assert_called_once_with(dry_run=True)

    def test_usage_finalize_requires_all_audit_arguments(self) -> None:
        """账期修复必须提供邮箱、账期、操作者和原因。"""
        result = self.runner.invoke(cli, ["usage-finalize"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Missing option", result.output)

    @patch("cam.cli.run_usage_periodic")
    def test_usage_snapshot_outputs_service_result_as_json(
        self, periodic: Mock
    ) -> None:
        """服务返回的汇总以 JSON 输出，便于运维采集。"""
        periodic.return_value = UsageRunSummary(success=1, skipped=2)

        result = self.runner.invoke(
            cli, ["usage-snapshot", "--all", "--type", "periodic"]
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            json.loads(result.output),
            {
                "success": 1,
                "failed": 0,
                "skipped": 2,
                "lock_busy": 0,
                "dry_run_items": [],
            },
        )


if __name__ == "__main__":
    unittest.main()
