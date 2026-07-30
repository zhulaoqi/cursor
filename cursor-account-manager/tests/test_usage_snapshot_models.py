"""用量快照领域模型单元测试。"""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest
from typing import get_type_hints

from cam.models import Account
from cam.usage_snapshot_models import (
    AccountMappingResult,
    AuthOutcome,
    BreakerSnapshot,
    CollectionResult,
    CollectionStatus,
    FinalCycle,
    FinalSource,
    KnownCycle,
    MonitoredAccount,
    SnapshotType,
    UsageSnapshot,
    WasteAssessment,
    WasteLevel,
)


UTC_START = datetime(2026, 7, 1, tzinfo=timezone.utc)
UTC_END = datetime(2026, 8, 1, tzinfo=timezone.utc)


def make_snapshot(**overrides):
    values = {
        "email": "a@example.com",
        "plan_tier": "pro",
        "plan_tier_raw": "Pro",
        "plan_status": "active",
        "plan_source": "api",
        "billing_cycle_start": UTC_START,
        "billing_cycle_end": UTC_END,
        "total_used_pct": Decimal("10.00"),
        "snapshot_type": SnapshotType.PERIODIC,
        "snapshot_slot": UTC_START,
        "collected_at": UTC_START,
        "source_endpoint": "GetCurrentPeriodUsage",
        "parser_version": "usage-v1",
        "raw_payload": {},
    }
    values.update(overrides)
    return UsageSnapshot(**values)


def make_known_cycle(**overrides):
    values = {
        "email": "a@example.com",
        "plan_tier": "pro",
        "billing_cycle_start": UTC_START,
        "billing_cycle_end": UTC_END,
    }
    values.update(overrides)
    return KnownCycle(**values)


def make_final_cycle(**overrides):
    values = {
        "email": "a@example.com",
        "plan_tier": "pro",
        "billing_cycle_start": UTC_START,
        "billing_cycle_end": UTC_END,
        "total_used_pct": Decimal("8.5"),
        "final_source": FinalSource.PRE_RESET,
    }
    values.update(overrides)
    return FinalCycle(**values)


def make_account(email="a@example.com"):
    return Account(
        email=email,
        imap_password="secret",
        imap_host="imap.example.com",
        imap_port=993,
    )


class EnumContractTests(unittest.TestCase):
    def test_enum_values_match_domain_contract(self):
        self.assertEqual(SnapshotType.PERIODIC.value, "periodic")
        self.assertEqual(SnapshotType.PRE_RESET.value, "pre_reset")
        self.assertEqual(CollectionStatus.SUCCESS.value, "success")
        self.assertEqual(CollectionStatus.FAILED.value, "failed")
        self.assertEqual(CollectionStatus.SKIPPED.value, "skipped")
        self.assertEqual(
            CollectionStatus.NOT_COLLECTABLE.value,
            "not_collectable",
        )
        self.assertEqual(
            CollectionStatus.ORPHAN_LOCAL_ACCOUNT.value,
            "orphan_local_account",
        )
        self.assertEqual(CollectionStatus.LOCK_BUSY.value, "lock_busy")
        self.assertEqual(
            CollectionStatus.AUTH_CIRCUIT_OPEN.value,
            "auth_circuit_open",
        )
        self.assertEqual(AuthOutcome.SUCCESS.value, "success")
        self.assertEqual(AuthOutcome.AUTH_FAILURE.value, "auth_failure")
        self.assertEqual(
            AuthOutcome.NON_AUTH_FAILURE.value,
            "non_auth_failure",
        )
        self.assertEqual(AuthOutcome.SKIPPED.value, "skipped")
        self.assertEqual(FinalSource.PRE_RESET.value, "pre_reset")
        self.assertEqual(
            FinalSource.PERIODIC_FALLBACK.value,
            "periodic_fallback",
        )
        self.assertEqual(
            [level.value for level in WasteLevel],
            ["unknown", "l0", "l1", "l2", "l3"],
        )


class UsageSnapshotTests(unittest.TestCase):
    def test_snapshot_rejects_invalid_cycle(self):
        with self.assertRaisesRegex(ValueError, "billing_cycle_end"):
            make_snapshot(
                billing_cycle_start=UTC_END,
                billing_cycle_end=UTC_START,
            )

    def test_zero_percent_is_valid(self):
        snapshot = make_snapshot(total_used_pct=Decimal("0.00"))

        self.assertEqual(snapshot.total_used_pct, Decimal("0.00"))

    def test_hundred_percent_is_valid(self):
        snapshot = make_snapshot(total_used_pct=Decimal("100"))

        self.assertEqual(snapshot.total_used_pct, Decimal("100"))

    def test_percent_below_zero_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "total_used_pct"):
            make_snapshot(total_used_pct=Decimal("-0.01"))

    def test_percent_above_hundred_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "total_used_pct"):
            make_snapshot(total_used_pct=Decimal("100.01"))

    def test_percent_accepts_decimal_string_and_int(self):
        for value, expected in (
            (Decimal("10.25"), Decimal("10.25")),
            ("10.25", Decimal("10.25")),
            (10, Decimal("10")),
        ):
            with self.subTest(value=value):
                snapshot = make_snapshot(total_used_pct=value)
                self.assertEqual(snapshot.total_used_pct, expected)
                self.assertIsInstance(snapshot.total_used_pct, Decimal)

    def test_percent_rejects_float_bool_and_invalid_string(self):
        for value in (10.0, True, "不是数字"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "total_used_pct"):
                    make_snapshot(total_used_pct=value)

    def test_non_finite_percent_is_rejected(self):
        for value in (
            Decimal("NaN"),
            Decimal("sNaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
            "NaN",
            "sNaN",
            "Infinity",
            "-Infinity",
        ):
            with self.subTest(value=str(value)):
                with self.assertRaisesRegex(ValueError, "total_used_pct"):
                    make_snapshot(total_used_pct=value)

    def test_naive_datetime_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "billing_cycle_start"):
            make_snapshot(
                billing_cycle_start=datetime(2026, 7, 1),
            )

    def test_non_utc_aware_datetime_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "collected_at"):
            make_snapshot(
                collected_at=datetime(
                    2026,
                    7,
                    1,
                    tzinfo=timezone(timedelta(hours=8)),
                ),
            )

    def test_all_three_datetimes_must_be_utc(self):
        with self.assertRaisesRegex(ValueError, "snapshot_slot"):
            make_snapshot(snapshot_slot=datetime(2026, 7, 1))

        with self.assertRaisesRegex(ValueError, "billing_cycle_end"):
            make_snapshot(
                billing_cycle_end=datetime(
                    2026,
                    8,
                    1,
                    tzinfo=timezone(timedelta(hours=-5)),
                ),
            )

    def test_empty_email_is_rejected_after_trimming(self):
        with self.assertRaisesRegex(ValueError, "email"):
            make_snapshot(email=" \t ")

    def test_non_trimmed_email_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "email"):
            make_snapshot(email="  a@example.com \n")

    def test_non_normalized_email_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "email"):
            make_snapshot(email="A@example.com")

    def test_non_string_email_is_rejected_as_value_error(self):
        with self.assertRaisesRegex(ValueError, "email"):
            make_snapshot(email=123)

    def test_unknown_plan_is_valid_but_explicit(self):
        snapshot = make_snapshot(plan_tier="unknown")

        self.assertEqual(snapshot.plan_tier, "unknown")

    def test_empty_plan_tier_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "plan_tier"):
            make_snapshot(plan_tier=" ")

    def test_non_string_plan_tier_is_rejected_as_value_error(self):
        with self.assertRaisesRegex(ValueError, "plan_tier"):
            make_snapshot(plan_tier=123)

    def test_raw_payload_must_be_dict(self):
        with self.assertRaisesRegex(ValueError, "raw_payload"):
            make_snapshot(raw_payload=[])

    def test_raw_payload_is_defensively_copied(self):
        raw_payload = {"usage": {"items": [1, 2]}}
        snapshot = make_snapshot(raw_payload=raw_payload)

        raw_payload["usage"]["items"].append(3)
        raw_payload["usage"]["new_field"] = "changed"

        self.assertEqual(snapshot.raw_payload, {"usage": {"items": (1, 2)}})

    def test_raw_payload_is_deeply_immutable_and_json_serializable(self):
        snapshot = make_snapshot(
            raw_payload={"usage": {"items": [1, {"value": 2}]}},
        )

        self.assertIsInstance(snapshot.raw_payload, dict)
        self.assertEqual(
            json.loads(json.dumps(snapshot.raw_payload)),
            {"usage": {"items": [1, {"value": 2}]}},
        )
        with self.assertRaises(TypeError):
            snapshot.raw_payload["new"] = 1
        with self.assertRaises(TypeError):
            snapshot.raw_payload["usage"]["new"] = 1
        with self.assertRaises(TypeError):
            snapshot.raw_payload["usage"]["items"][0] = 3
        with self.assertRaises(TypeError):
            snapshot.raw_payload["usage"]["items"][1]["value"] = 3

    def test_raw_payload_rejects_set(self):
        with self.assertRaisesRegex(ValueError, "raw_payload"):
            make_snapshot(raw_payload={"values": {1, 2}})

    def test_constructor_and_stored_percentage_types_are_explicit(self):
        constructor_type = get_type_hints(
            UsageSnapshot.__init__,
        )["total_used_pct"]
        stored_type = get_type_hints(UsageSnapshot)["total_used_pct"]

        self.assertEqual(constructor_type, Decimal | str | int)
        self.assertIs(stored_type, Decimal)
        self.assertIsInstance(
            make_snapshot(total_used_pct="12.5").total_used_pct,
            Decimal,
        )

    def test_snapshot_type_must_be_enum(self):
        with self.assertRaisesRegex(ValueError, "snapshot_type"):
            make_snapshot(snapshot_type="periodic")

    def test_snapshot_is_frozen(self):
        snapshot = make_snapshot()

        with self.assertRaises(FrozenInstanceError):
            snapshot.email = "b@example.com"


class CollectionResultTests(unittest.TestCase):
    def test_status_must_be_collection_status(self):
        with self.assertRaisesRegex(ValueError, "status"):
            CollectionResult(email="a@example.com", status="failed")

    def test_auth_outcome_must_be_auth_outcome(self):
        with self.assertRaisesRegex(ValueError, "auth_outcome"):
            CollectionResult(
                email="a@example.com",
                status=CollectionStatus.FAILED,
                auth_outcome="skipped",
            )

    def test_email_must_be_normalized(self):
        for email in ("", " ", " a@example.com", "A@example.com", 123):
            with self.subTest(email=email):
                with self.assertRaisesRegex(ValueError, "email"):
                    CollectionResult(
                        email=email,
                        status=CollectionStatus.FAILED,
                    )

    def test_success_requires_snapshot(self):
        with self.assertRaisesRegex(ValueError, "SUCCESS"):
            CollectionResult(
                email="a@example.com",
                status=CollectionStatus.SUCCESS,
            )

    def test_non_success_rejects_snapshot(self):
        with self.assertRaisesRegex(ValueError, "snapshot"):
            CollectionResult(
                email="a@example.com",
                status=CollectionStatus.FAILED,
                snapshot=make_snapshot(),
            )

    def test_success_accepts_snapshot(self):
        snapshot = make_snapshot()
        result = CollectionResult(
            email="a@example.com",
            status=CollectionStatus.SUCCESS,
            snapshot=snapshot,
            auth_outcome=AuthOutcome.SUCCESS,
        )

        self.assertIs(result.snapshot, snapshot)

    def test_success_snapshot_email_must_match_result_email(self):
        with self.assertRaisesRegex(ValueError, "email"):
            CollectionResult(
                email="b@example.com",
                status=CollectionStatus.SUCCESS,
                snapshot=make_snapshot(email="a@example.com"),
                auth_outcome=AuthOutcome.SUCCESS,
            )

    def test_non_empty_snapshot_must_be_usage_snapshot(self):
        with self.assertRaisesRegex(ValueError, "snapshot"):
            CollectionResult(
                email="a@example.com",
                status=CollectionStatus.SUCCESS,
                snapshot=object(),
            )


class SupportingModelTests(unittest.TestCase):
    def test_account_mapping_defaults_are_immutable_tuples(self):
        first = AccountMappingResult()
        second = AccountMappingResult()

        self.assertEqual(first.collectable_accounts, ())
        self.assertEqual(first.not_collectable_emails, ())
        self.assertEqual(first.orphan_local_emails, ())
        self.assertIsInstance(first.collectable_accounts, tuple)
        self.assertIsInstance(second.collectable_accounts, tuple)

    def test_monitored_account_is_frozen(self):
        monitored = MonitoredAccount(
            account=make_account(),
            applicant="申请人",
            department="研发部",
        )

        with self.assertRaises(FrozenInstanceError):
            monitored.department = "财务部"

    def test_account_mapping_accepts_monitored_accounts(self):
        monitored = MonitoredAccount(
            account=make_account(),
            applicant="申请人",
            department="研发部",
        )
        result = AccountMappingResult(
            collectable_accounts=(monitored,),
            not_collectable_emails=("b@example.com",),
            orphan_local_emails=("c@example.com",),
        )

        self.assertEqual(result.collectable_accounts, (monitored,))

    def test_account_mapping_fields_must_be_tuples(self):
        for field_name in (
            "collectable_accounts",
            "not_collectable_emails",
            "orphan_local_emails",
        ):
            with self.subTest(field_name=field_name):
                with self.assertRaisesRegex(ValueError, field_name):
                    AccountMappingResult(**{field_name: []})

    def test_account_mapping_collectable_elements_must_be_monitored_accounts(self):
        with self.assertRaisesRegex(ValueError, "collectable_accounts"):
            AccountMappingResult(collectable_accounts=(make_account(),))

    def test_account_mapping_email_elements_must_be_normalized(self):
        for field_name in (
            "not_collectable_emails",
            "orphan_local_emails",
        ):
            for email in ("", " a@example.com", "A@example.com", 123):
                with self.subTest(field_name=field_name, email=email):
                    with self.assertRaisesRegex(ValueError, field_name):
                        AccountMappingResult(**{field_name: (email,)})

    def test_breaker_snapshot_holds_state(self):
        snapshot = BreakerSnapshot(
            state="open",
            sample_count=20,
            auth_failure_count=12,
            opened_at=UTC_START,
            retry_at=UTC_END,
        )

        self.assertEqual(snapshot.auth_failure_count, 12)

    def test_breaker_state_must_be_supported(self):
        with self.assertRaisesRegex(ValueError, "state"):
            BreakerSnapshot("invalid", 0, 0, None, None)

    def test_breaker_counts_must_be_consistent(self):
        for sample_count, auth_failure_count in (
            (-1, 0),
            (1, -1),
            (1, 2),
        ):
            with self.subTest(
                sample_count=sample_count,
                auth_failure_count=auth_failure_count,
            ):
                with self.assertRaises(ValueError):
                    BreakerSnapshot(
                        "closed",
                        sample_count,
                        auth_failure_count,
                        None,
                        None,
                    )

    def test_breaker_optional_datetimes_must_be_utc(self):
        with self.assertRaisesRegex(ValueError, "opened_at"):
            BreakerSnapshot(
                "open",
                1,
                1,
                datetime(2026, 7, 1),
                None,
            )
        with self.assertRaisesRegex(ValueError, "retry_at"):
            BreakerSnapshot(
                "half_open",
                1,
                1,
                None,
                datetime(
                    2026,
                    7,
                    1,
                    tzinfo=timezone(timedelta(hours=8)),
                ),
            )

    def test_known_cycle_rejects_invalid_identity_and_plan(self):
        for overrides in (
            {"email": " A@example.com"},
            {"email": 123},
            {"plan_tier": " "},
            {"plan_tier": 123},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    make_known_cycle(**overrides)

    def test_known_cycle_requires_valid_utc_cycle(self):
        for overrides in (
            {"billing_cycle_start": datetime(2026, 7, 1)},
            {
                "billing_cycle_end": datetime(
                    2026,
                    8,
                    1,
                    tzinfo=timezone(timedelta(hours=8)),
                ),
            },
            {
                "billing_cycle_start": UTC_END,
                "billing_cycle_end": UTC_START,
            },
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    make_known_cycle(**overrides)

    def test_final_cycle_applies_known_cycle_validation(self):
        for overrides in (
            {"email": "A@example.com"},
            {"plan_tier": ""},
            {"billing_cycle_start": datetime(2026, 7, 1)},
            {
                "billing_cycle_start": UTC_END,
                "billing_cycle_end": UTC_START,
            },
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    make_final_cycle(**overrides)

    def test_final_cycle_percent_accepts_decimal_string_and_int(self):
        for value, expected in (
            (Decimal("8.5"), Decimal("8.5")),
            ("8.5", Decimal("8.5")),
            (8, Decimal("8")),
        ):
            with self.subTest(value=value):
                cycle = make_final_cycle(total_used_pct=value)
                self.assertEqual(cycle.total_used_pct, expected)
                self.assertIsInstance(cycle.total_used_pct, Decimal)

    def test_final_cycle_constructor_and_stored_percentage_types_are_explicit(self):
        constructor_type = get_type_hints(
            FinalCycle.__init__,
        )["total_used_pct"]
        stored_type = get_type_hints(FinalCycle)["total_used_pct"]

        self.assertEqual(constructor_type, Decimal | str | int)
        self.assertIs(stored_type, Decimal)
        self.assertIsInstance(
            make_final_cycle(total_used_pct="8.5").total_used_pct,
            Decimal,
        )

    def test_final_cycle_percent_rejects_invalid_values(self):
        for value in (
            1.0,
            True,
            "不是数字",
            Decimal("NaN"),
            Decimal("sNaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
            "NaN",
            "sNaN",
            "Infinity",
            "-Infinity",
            Decimal("-0.01"),
            Decimal("100.01"),
        ):
            with self.subTest(value=str(value)):
                with self.assertRaisesRegex(ValueError, "total_used_pct"):
                    make_final_cycle(total_used_pct=value)

    def test_final_cycle_source_must_be_final_source(self):
        with self.assertRaisesRegex(ValueError, "final_source"):
            make_final_cycle(final_source="pre_reset")

    def test_waste_assessment_rejects_invalid_values(self):
        invalid_values = (
            {"email": " A@example.com"},
            {"email": 123},
            {"level": "l1"},
            {"low_usage_streak": -1},
            {"data_quality_status": " "},
            {"data_quality_status": 123},
        )
        for overrides in invalid_values:
            values = {
                "email": "a@example.com",
                "level": WasteLevel.L1,
                "low_usage_streak": 1,
                "data_quality_status": "complete",
            }
            values.update(overrides)
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    WasteAssessment(**values)

    def test_waste_analysis_models_hold_contract_fields(self):
        known = make_known_cycle()
        final = make_final_cycle()
        assessment = WasteAssessment(
            email="a@example.com",
            level=WasteLevel.L2,
            low_usage_streak=2,
            data_quality_status="complete",
        )

        self.assertEqual(known.plan_tier, "pro")
        self.assertEqual(final.final_source, FinalSource.PRE_RESET)
        self.assertEqual(assessment.reason, "")


if __name__ == "__main__":
    unittest.main()
