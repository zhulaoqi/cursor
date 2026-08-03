"""用量快照解析测试。"""

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from cam.usage_snapshot_models import SnapshotType


class UsageSnapshotParserTests(unittest.TestCase):
    """验证 Cursor 用量接口的核心字段解析。"""

    def test_parse_usage_snapshot_reads_cycle_percent_and_plan_name(self):
        from cam.usage_snapshot_parser import parse_usage_snapshot

        snapshot = parse_usage_snapshot(
            email="user@example.com",
            usage_payload={
                "billingCycleStart": 1751328000000,
                "billingCycleEnd": 1754006400000,
                "planUsage": {
                    "totalPercentUsed": 12.5,
                    "autoPercentUsed": 8.25,
                    "apiPercentUsed": 3.5,
                },
            },
            plan_payload={"planName": "Pro+"},
            stripe_payload={"subscriptionStatus": "active"},
            snapshot_type=SnapshotType.PERIODIC,
            snapshot_slot=datetime(2025, 7, 1, tzinfo=timezone.utc),
            collected_at=datetime(2025, 7, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(snapshot.plan_tier, "pro_plus")
        self.assertEqual(snapshot.plan_status, "active")
        self.assertEqual(snapshot.total_used_pct, Decimal("12.5"))
        self.assertEqual(snapshot.auto_used_pct, Decimal("8.25"))
        self.assertEqual(snapshot.api_used_pct, Decimal("3.5"))
        self.assertEqual(snapshot.parser_version, "usage-v2")
        self.assertEqual(
            snapshot.billing_cycle_start,
            datetime(2025, 7, 1, tzinfo=timezone.utc),
        )

    def test_parse_usage_snapshot_allows_missing_auto_api_percent(self):
        from cam.usage_snapshot_parser import parse_usage_snapshot

        snapshot = parse_usage_snapshot(
            email="user@example.com",
            usage_payload={
                "billingCycleStart": 1751328000000,
                "billingCycleEnd": 1754006400000,
                "planUsage": {"totalPercentUsed": 12},
            },
            plan_payload={"planName": "Pro"},
            stripe_payload={},
            snapshot_type=SnapshotType.PERIODIC,
            snapshot_slot=datetime(2025, 7, 1, tzinfo=timezone.utc),
            collected_at=datetime(2025, 7, 2, tzinfo=timezone.utc),
        )

        self.assertIsNone(snapshot.auto_used_pct)
        self.assertIsNone(snapshot.api_used_pct)

    def test_plan_amount_without_explicit_name_remains_unknown(self):
        from cam.usage_snapshot_parser import normalize_plan_tier

        self.assertEqual(normalize_plan_tier("$60"), ("unknown", "$60"))

    def test_plan_name_normalization_covers_known_and_unknown_tiers(self):
        from cam.usage_snapshot_parser import normalize_plan_tier

        cases = {
            "Pro": "pro",
            "Pro+": "pro_plus",
            "Free": "free",
            "自定义套餐": "unknown",
        }
        for raw_name, expected_tier in cases.items():
            with self.subTest(raw_name=raw_name):
                self.assertEqual(
                    normalize_plan_tier(raw_name),
                    (expected_tier, raw_name),
                )

    def test_raw_payload_masks_nested_credentials(self):
        from cam.usage_snapshot_parser import parse_usage_snapshot

        snapshot = parse_usage_snapshot(
            email="user@example.com",
            usage_payload={
                "billingCycleStart": 1751328000000,
                "billingCycleEnd": 1754006400000,
                "planUsage": {"totalPercentUsed": 12},
                "authorization": "Bearer secret",
            },
            plan_payload={"planName": "Pro", "nested": {"refresh_token": "secret"}},
            stripe_payload={"cookie": "secret"},
            snapshot_type=SnapshotType.PERIODIC,
            snapshot_slot=datetime(2025, 7, 1, tzinfo=timezone.utc),
            collected_at=datetime(2025, 7, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(snapshot.raw_payload["usage"]["authorization"], "<已脱敏>")
        self.assertEqual(
            snapshot.raw_payload["plan"]["nested"]["refresh_token"],
            "<已脱敏>",
        )
        self.assertEqual(snapshot.raw_payload["stripe"]["cookie"], "<已脱敏>")

    def test_parse_rejects_missing_cycle_or_unknown_percent_unit(self):
        from cam.usage_snapshot_parser import parse_usage_snapshot

        with self.assertRaises(ValueError):
            parse_usage_snapshot(
                email="user@example.com",
                usage_payload={"planUsage": {"totalPercentUsed": 0.2}},
                plan_payload={},
                stripe_payload={},
                snapshot_type=SnapshotType.PERIODIC,
                snapshot_slot=datetime(2025, 7, 1, tzinfo=timezone.utc),
                collected_at=datetime(2025, 7, 2, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
