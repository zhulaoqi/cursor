import os
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

USAGE_ENV_KEYS = (
    "USAGE_SNAPSHOT_ENABLE",
    "USAGE_PERIODIC_INTERVAL_HOURS",
    "USAGE_PERIODIC_DAILY_AT",
    "USAGE_SNAPSHOT_CONCURRENCY",
    "USAGE_BOOTSTRAP_STALE_HOURS",
    "USAGE_PERIODIC_RETRY_MINUTES",
    "USAGE_PERIODIC_MAX_ATTEMPTS_PER_SLOT",
    "USAGE_PRE_RESET_SCAN_INTERVAL_MIN",
    "USAGE_PRE_RESET_WINDOW_START_MIN",
    "USAGE_PRE_RESET_TARGET_OFFSET_MIN",
    "USAGE_PRE_RESET_WINDOW_END_MIN",
    "USAGE_LOW_THRESHOLD_PCT",
    "USAGE_CYCLE_CONTINUITY_TOLERANCE_HOURS",
    "USAGE_PERIODIC_LOCK_FILE",
    "USAGE_PRE_RESET_LOCK_FILE",
    "USAGE_ACCOUNT_LOCK_DIR",
    "USAGE_ACCOUNT_LOCK_TIMEOUT_SEC",
    "USAGE_AUTH_BREAKER_MIN_SAMPLES",
    "USAGE_AUTH_BREAKER_FAILURE_RATIO",
    "USAGE_AUTH_BREAKER_COOLDOWN_MIN",
    "USAGE_AUTH_BREAKER_WINDOW_SIZE",
    "USAGE_AUTH_BREAKER_WINDOW_MIN",
)

_MISSING_ENV = object()
_IMPORT_ENV_BEFORE = {
    key: os.environ.get(key, _MISSING_ENV)
    for key in USAGE_ENV_KEYS
}
with patch.dict(
    os.environ,
    {key: "" for key in USAGE_ENV_KEYS},
    clear=False,
):
    from cam import config

PROJECT_ROOT = config.PROJECT_ROOT


def load_with_env(**values: str):
    env = {
        "BILLING_LEDGER_REFRESH_ENABLE": "false",
        "LEDGER_DB_PASSWORD": "",
    }
    env.update(values)
    with patch.dict(os.environ, env, clear=True):
        return config.load_settings()


class UsageConfigTests(unittest.TestCase):
    def test_controlled_import_environment_is_restored(self):
        for key, original_value in _IMPORT_ENV_BEFORE.items():
            with self.subTest(key=key):
                if original_value is _MISSING_ENV:
                    self.assertNotIn(key, os.environ)
                else:
                    self.assertEqual(os.environ.get(key), original_value)

    def test_usage_configuration_api_is_available(self):
        self.assertTrue(
            hasattr(config, "validate_database_credentials"),
            "应提供数据库配置预检函数",
        )

    def test_database_validation_docstring_states_call_timing(self):
        docstring = config.validate_database_credentials.__doc__ or ""

        self.assertIn("由数据库任务启动入口调用", docstring)
        self.assertIn("不在 load_settings/import 时调用", docstring)

    def test_usage_defaults(self):
        settings = load_with_env(**{key: "" for key in USAGE_ENV_KEYS})

        expected = {
            "usage_snapshot_enable": True,
            "usage_periodic_interval_hours": 24,
            "usage_periodic_daily_at": "06:00",
            "usage_snapshot_concurrency": 10,
            "usage_bootstrap_stale_hours": 36,
            "usage_periodic_retry_minutes": 30,
            "usage_periodic_max_attempts_per_slot": 3,
            "usage_pre_reset_scan_interval_min": 15,
            "usage_pre_reset_window_start_min": 360,
            "usage_pre_reset_target_offset_min": 180,
            "usage_pre_reset_window_end_min": 30,
            "usage_low_threshold_pct": Decimal("30"),
            "usage_cycle_continuity_tolerance_hours": 48,
            "usage_periodic_lock_file": "data/cam_usage_periodic.lock",
            "usage_pre_reset_lock_file": "data/cam_usage_pre_reset.lock",
            "usage_account_lock_dir": PROJECT_ROOT / "data/usage-account-locks",
            "usage_account_lock_timeout_sec": 5,
            "usage_auth_breaker_min_samples": 10,
            "usage_auth_breaker_failure_ratio": 0.30,
            "usage_auth_breaker_cooldown_min": 30,
            "usage_auth_breaker_window_size": 50,
            "usage_auth_breaker_window_min": 10,
        }
        for field, expected_value in expected.items():
            with self.subTest(field=field):
                self.assertEqual(getattr(settings, field), expected_value)

    def test_usage_custom_values(self):
        settings = load_with_env(
            USAGE_SNAPSHOT_ENABLE="false",
            USAGE_PERIODIC_INTERVAL_HOURS="12",
            USAGE_PERIODIC_DAILY_AT="19:00",
            USAGE_SNAPSHOT_CONCURRENCY="7",
            USAGE_BOOTSTRAP_STALE_HOURS="18",
            USAGE_PERIODIC_RETRY_MINUTES="20",
            USAGE_PERIODIC_MAX_ATTEMPTS_PER_SLOT="4",
            USAGE_PRE_RESET_SCAN_INTERVAL_MIN="10",
            USAGE_PRE_RESET_WINDOW_START_MIN="300",
            USAGE_PRE_RESET_TARGET_OFFSET_MIN="120",
            USAGE_PRE_RESET_WINDOW_END_MIN="20",
            USAGE_LOW_THRESHOLD_PCT="25.5",
            USAGE_CYCLE_CONTINUITY_TOLERANCE_HOURS="24",
            USAGE_PERIODIC_LOCK_FILE="data/custom-periodic.lock",
            USAGE_PRE_RESET_LOCK_FILE="data/custom-pre-reset.lock",
            USAGE_ACCOUNT_LOCK_DIR="data/custom-account-locks",
            USAGE_ACCOUNT_LOCK_TIMEOUT_SEC="0",
            USAGE_AUTH_BREAKER_MIN_SAMPLES="8",
            USAGE_AUTH_BREAKER_FAILURE_RATIO="0.45",
            USAGE_AUTH_BREAKER_COOLDOWN_MIN="15",
            USAGE_AUTH_BREAKER_WINDOW_SIZE="40",
            USAGE_AUTH_BREAKER_WINDOW_MIN="5",
        )

        self.assertFalse(settings.usage_snapshot_enable)
        self.assertEqual(settings.usage_periodic_interval_hours, 12)
        self.assertEqual(settings.usage_periodic_daily_at, "19:00")
        self.assertEqual(settings.usage_snapshot_concurrency, 7)
        self.assertEqual(settings.usage_bootstrap_stale_hours, 18)
        self.assertEqual(settings.usage_periodic_retry_minutes, 20)
        self.assertEqual(settings.usage_periodic_max_attempts_per_slot, 4)
        self.assertEqual(settings.usage_pre_reset_scan_interval_min, 10)
        self.assertEqual(settings.usage_pre_reset_window_start_min, 300)
        self.assertEqual(settings.usage_pre_reset_target_offset_min, 120)
        self.assertEqual(settings.usage_pre_reset_window_end_min, 20)
        self.assertEqual(settings.usage_low_threshold_pct, Decimal("25.5"))
        self.assertEqual(settings.usage_cycle_continuity_tolerance_hours, 24)
        self.assertEqual(settings.usage_periodic_lock_file, "data/custom-periodic.lock")
        self.assertEqual(settings.usage_pre_reset_lock_file, "data/custom-pre-reset.lock")
        self.assertEqual(
            settings.usage_account_lock_dir,
            PROJECT_ROOT / "data/custom-account-locks",
        )
        self.assertEqual(settings.usage_account_lock_timeout_sec, 0)
        self.assertEqual(settings.usage_auth_breaker_min_samples, 8)
        self.assertEqual(settings.usage_auth_breaker_failure_ratio, 0.45)
        self.assertEqual(settings.usage_auth_breaker_cooldown_min, 15)
        self.assertEqual(settings.usage_auth_breaker_window_size, 40)
        self.assertEqual(settings.usage_auth_breaker_window_min, 5)

    def test_usage_account_lock_dir_preserves_absolute_path(self):
        settings = load_with_env(USAGE_ACCOUNT_LOCK_DIR="/tmp/cam-usage-locks")

        self.assertEqual(
            settings.usage_account_lock_dir,
            Path("/tmp/cam-usage-locks"),
        )

    def test_blank_usage_values_use_defaults(self):
        for blank in (" ", "\t", " \t "):
            with self.subTest(blank=repr(blank)):
                settings = load_with_env(
                    **{key: blank for key in USAGE_ENV_KEYS},
                )
                self.assertTrue(settings.usage_snapshot_enable)
                self.assertEqual(settings.usage_periodic_interval_hours, 24)
                self.assertEqual(
                    settings.usage_low_threshold_pct,
                    Decimal("30"),
                )
                self.assertEqual(
                    settings.usage_account_lock_dir,
                    PROJECT_ROOT / "data/usage-account-locks",
                )

    def test_explicit_invalid_usage_integers_fail(self):
        integer_keys = [
            "USAGE_PERIODIC_INTERVAL_HOURS",
            "USAGE_SNAPSHOT_CONCURRENCY",
            "USAGE_BOOTSTRAP_STALE_HOURS",
            "USAGE_PERIODIC_RETRY_MINUTES",
            "USAGE_PERIODIC_MAX_ATTEMPTS_PER_SLOT",
            "USAGE_PRE_RESET_SCAN_INTERVAL_MIN",
            "USAGE_PRE_RESET_WINDOW_START_MIN",
            "USAGE_PRE_RESET_TARGET_OFFSET_MIN",
            "USAGE_PRE_RESET_WINDOW_END_MIN",
            "USAGE_CYCLE_CONTINUITY_TOLERANCE_HOURS",
            "USAGE_ACCOUNT_LOCK_TIMEOUT_SEC",
            "USAGE_AUTH_BREAKER_MIN_SAMPLES",
            "USAGE_AUTH_BREAKER_COOLDOWN_MIN",
            "USAGE_AUTH_BREAKER_WINDOW_SIZE",
            "USAGE_AUTH_BREAKER_WINDOW_MIN",
        ]
        for key in integer_keys:
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, key):
                    load_with_env(**{key: "abc"})

    def test_explicit_invalid_usage_floats_fail(self):
        key = "USAGE_AUTH_BREAKER_FAILURE_RATIO"
        for value in ("abc", "NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, key):
                    load_with_env(**{key: value})

    def test_explicit_invalid_usage_decimals_fail(self):
        key = "USAGE_LOW_THRESHOLD_PCT"
        for value in ("abc", "NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, key):
                    load_with_env(**{key: value})

    def test_usage_boolean_accepts_mixed_case(self):
        valid_cases = [
            ("TrUe", True),
            ("YeS", True),
            ("oN", True),
            ("FaLsE", False),
            ("nO", False),
            ("OfF", False),
        ]
        for value, expected in valid_cases:
            with self.subTest(value=value):
                settings = load_with_env(USAGE_SNAPSHOT_ENABLE=value)
                self.assertEqual(settings.usage_snapshot_enable, expected)

    def test_explicit_invalid_usage_booleans_fail(self):
        key = "USAGE_SNAPSHOT_ENABLE"
        for value in ("sometimes", "TrUeX", "FaLsEy"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, key):
                    load_with_env(**{key: value})

    def test_usage_validation_boundaries(self):
        invalid_cases = [
            (
                "窗口起点必须大于目标偏移",
                {"USAGE_PRE_RESET_WINDOW_START_MIN": "180"},
                "USAGE_PRE_RESET_WINDOW_START_MIN",
            ),
            (
                "目标偏移必须大于窗口终点",
                {"USAGE_PRE_RESET_TARGET_OFFSET_MIN": "30"},
                "USAGE_PRE_RESET_TARGET_OFFSET_MIN",
            ),
            (
                "窗口终点不能为负数",
                {"USAGE_PRE_RESET_WINDOW_END_MIN": "-1"},
                "USAGE_PRE_RESET_WINDOW_END_MIN",
            ),
            (
                "扫描间隔必须为正数",
                {"USAGE_PRE_RESET_SCAN_INTERVAL_MIN": "0"},
                "USAGE_PRE_RESET_SCAN_INTERVAL_MIN",
            ),
            (
                "周期采集间隔必须为正数",
                {"USAGE_PERIODIC_INTERVAL_HOURS": "0"},
                "USAGE_PERIODIC_INTERVAL_HOURS",
            ),
            (
                "日常采集时刻格式非法",
                {"USAGE_PERIODIC_DAILY_AT": "25:99"},
                "USAGE_PERIODIC_DAILY_AT",
            ),
            (
                "重试间隔必须为正数",
                {"USAGE_PERIODIC_RETRY_MINUTES": "0"},
                "USAGE_PERIODIC_RETRY_MINUTES",
            ),
            (
                "熔断冷却时间必须为正数",
                {"USAGE_AUTH_BREAKER_COOLDOWN_MIN": "0"},
                "USAGE_AUTH_BREAKER_COOLDOWN_MIN",
            ),
            (
                "熔断窗口时间必须为正数",
                {"USAGE_AUTH_BREAKER_WINDOW_MIN": "0"},
                "USAGE_AUTH_BREAKER_WINDOW_MIN",
            ),
            (
                "首次补采阈值不能小于周期",
                {
                    "USAGE_PERIODIC_INTERVAL_HOURS": "24",
                    "USAGE_BOOTSTRAP_STALE_HOURS": "23",
                },
                "USAGE_BOOTSTRAP_STALE_HOURS",
            ),
            (
                "最大尝试次数至少为一",
                {"USAGE_PERIODIC_MAX_ATTEMPTS_PER_SLOT": "0"},
                "USAGE_PERIODIC_MAX_ATTEMPTS_PER_SLOT",
            ),
            (
                "采集并发至少为一",
                {"USAGE_SNAPSHOT_CONCURRENCY": "0"},
                "USAGE_SNAPSHOT_CONCURRENCY",
            ),
            (
                "最小样本数至少为一",
                {"USAGE_AUTH_BREAKER_MIN_SAMPLES": "0"},
                "USAGE_AUTH_BREAKER_MIN_SAMPLES",
            ),
            (
                "窗口大小至少为一",
                {
                    "USAGE_AUTH_BREAKER_MIN_SAMPLES": "1",
                    "USAGE_AUTH_BREAKER_WINDOW_SIZE": "0",
                },
                "USAGE_AUTH_BREAKER_WINDOW_SIZE",
            ),
            (
                "窗口大小不能小于最小样本数",
                {
                    "USAGE_AUTH_BREAKER_MIN_SAMPLES": "11",
                    "USAGE_AUTH_BREAKER_WINDOW_SIZE": "10",
                },
                "USAGE_AUTH_BREAKER_WINDOW_SIZE",
            ),
            (
                "低用量阈值不能小于零",
                {"USAGE_LOW_THRESHOLD_PCT": "-0.1"},
                "USAGE_LOW_THRESHOLD_PCT",
            ),
            (
                "低用量阈值不能大于一百",
                {"USAGE_LOW_THRESHOLD_PCT": "100.1"},
                "USAGE_LOW_THRESHOLD_PCT",
            ),
            (
                "账期连续容差不能为负数",
                {"USAGE_CYCLE_CONTINUITY_TOLERANCE_HOURS": "-1"},
                "USAGE_CYCLE_CONTINUITY_TOLERANCE_HOURS",
            ),
            (
                "认证失败比例必须大于零",
                {"USAGE_AUTH_BREAKER_FAILURE_RATIO": "0"},
                "USAGE_AUTH_BREAKER_FAILURE_RATIO",
            ),
            (
                "认证失败比例不能大于一",
                {"USAGE_AUTH_BREAKER_FAILURE_RATIO": "1.01"},
                "USAGE_AUTH_BREAKER_FAILURE_RATIO",
            ),
            (
                "账号锁超时不能为负数",
                {"USAGE_ACCOUNT_LOCK_TIMEOUT_SEC": "-1"},
                "USAGE_ACCOUNT_LOCK_TIMEOUT_SEC",
            ),
        ]
        for description, values, message in invalid_cases:
            with self.subTest(description=description):
                with self.assertRaisesRegex(ValueError, message):
                    load_with_env(**values)

    def test_usage_validation_accepts_inclusive_boundaries(self):
        settings = load_with_env(
            USAGE_PERIODIC_INTERVAL_HOURS="1",
            USAGE_BOOTSTRAP_STALE_HOURS="1",
            USAGE_PERIODIC_MAX_ATTEMPTS_PER_SLOT="1",
            USAGE_SNAPSHOT_CONCURRENCY="1",
            USAGE_PRE_RESET_WINDOW_END_MIN="0",
            USAGE_LOW_THRESHOLD_PCT="100",
            USAGE_CYCLE_CONTINUITY_TOLERANCE_HOURS="0",
            USAGE_ACCOUNT_LOCK_TIMEOUT_SEC="0",
            USAGE_AUTH_BREAKER_MIN_SAMPLES="1",
            USAGE_AUTH_BREAKER_FAILURE_RATIO="1",
            USAGE_AUTH_BREAKER_WINDOW_SIZE="1",
        )

        self.assertEqual(settings.usage_bootstrap_stale_hours, 1)
        self.assertEqual(settings.usage_low_threshold_pct, Decimal("100"))
        self.assertEqual(settings.usage_auth_breaker_failure_ratio, 1.0)

    def test_ledger_password_has_no_source_default(self):
        settings = load_with_env()

        self.assertEqual(settings.ledger_db_password, "")

    def test_database_credentials_are_not_checked_during_parsing(self):
        load_with_env(
            BILLING_LEDGER_REFRESH_ENABLE="true",
            USAGE_SNAPSHOT_ENABLE="true",
            LEDGER_DB_PASSWORD="",
        )

    def test_database_credentials_are_optional_when_tasks_are_disabled(self):
        settings = load_with_env(
            BILLING_LEDGER_REFRESH_ENABLE="false",
            USAGE_SNAPSHOT_ENABLE="false",
            LEDGER_DB_PASSWORD="",
        )

        config.validate_database_credentials(settings)

    def test_database_credentials_are_required_when_ledger_is_enabled(self):
        settings = load_with_env(
            BILLING_LEDGER_REFRESH_ENABLE="true",
            USAGE_SNAPSHOT_ENABLE="false",
            LEDGER_DB_PASSWORD="",
        )

        with self.assertRaisesRegex(ValueError, "LEDGER_DB_PASSWORD"):
            config.validate_database_credentials(settings)

    def test_database_credentials_are_required_when_usage_is_enabled(self):
        settings = load_with_env(
            BILLING_LEDGER_REFRESH_ENABLE="false",
            USAGE_SNAPSHOT_ENABLE="true",
            LEDGER_DB_PASSWORD="",
        )

        with self.assertRaisesRegex(ValueError, "LEDGER_DB_PASSWORD"):
            config.validate_database_credentials(settings)

    def test_database_credentials_pass_when_enabled_with_password(self):
        settings = load_with_env(
            BILLING_LEDGER_REFRESH_ENABLE="true",
            USAGE_SNAPSHOT_ENABLE="true",
            LEDGER_DB_PASSWORD="仅用于测试的密码",
        )

        config.validate_database_credentials(settings)


if __name__ == "__main__":
    unittest.main()
