"""低用量浪费等级分析测试。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from cam.usage_snapshot_models import (
    FinalCycle,
    FinalSource,
    KnownCycle,
    WasteLevel,
)
from cam.usage_waste import classify_waste


UTC = timezone.utc
EMAIL = "a@example.com"
START = datetime(2026, 1, 1, tzinfo=UTC)


def cycle(index, *, tier="pro", usage="10", known=True):
    """创建连续的完整账期及其可选最终记录。"""
    start = START + timedelta(days=31 * index)
    end = start + timedelta(days=30)
    known_cycle = KnownCycle(EMAIL, tier, start, end)
    final_cycle = FinalCycle(
        EMAIL,
        tier,
        start,
        end,
        Decimal(usage),
        FinalSource.PRE_RESET,
    )
    return known_cycle if known else final_cycle


def assess(*, tier="pro", known=(), finals=()):
    """以统一阈值和连续性容差执行评估。"""
    return classify_waste(
        current_plan_tier=tier,
        known_cycles=known,
        final_cycles=finals,
        low_threshold_pct=Decimal("30"),
        continuity_tolerance=timedelta(days=2),
    )


class WasteClassificationTests(unittest.TestCase):
    """验证连续账期的低用量等级规则。"""

    def test_unknown_current_plan_is_unknown(self):
        known = (cycle(0),)
        result = assess(tier="unknown", known=known, finals=(cycle(0, known=False),))
        self.assertEqual(result.level, WasteLevel.UNKNOWN)

    def test_ended_known_cycle_without_final_is_unknown(self):
        result = assess(known=(cycle(0),), finals=())
        self.assertEqual(result.level, WasteLevel.UNKNOWN)

    def test_latest_healthy_cycle_is_l0(self):
        known = (cycle(0),)
        result = assess(known=known, finals=(cycle(0, usage="30", known=False),))
        self.assertEqual((result.level, result.low_usage_streak), (WasteLevel.L0, 0))

    def test_one_low_cycle_is_l1(self):
        known = (cycle(0),)
        result = assess(known=known, finals=(cycle(0, known=False),))
        self.assertEqual((result.level, result.low_usage_streak), (WasteLevel.L1, 1))

    def test_two_low_cycles_is_l2(self):
        known = (cycle(0), cycle(1))
        finals = (cycle(0, known=False), cycle(1, known=False))
        result = assess(known=known, finals=finals)
        self.assertEqual((result.level, result.low_usage_streak), (WasteLevel.L2, 2))

    def test_three_low_cycles_is_l3(self):
        known = tuple(cycle(index) for index in range(3))
        finals = tuple(cycle(index, known=False) for index in range(3))
        result = assess(known=known, finals=finals)
        self.assertEqual((result.level, result.low_usage_streak), (WasteLevel.L3, 3))

    def test_healthy_cycle_breaks_low_streak(self):
        known = tuple(cycle(index) for index in range(3))
        finals = (
            cycle(0, known=False),
            cycle(1, usage="60", known=False),
            cycle(2, known=False),
        )
        result = assess(known=known, finals=finals)
        self.assertEqual((result.level, result.low_usage_streak), (WasteLevel.L1, 1))

    def test_plan_change_resets_current_segment(self):
        known = (cycle(0), cycle(1, tier="ultra"))
        finals = (cycle(0, known=False), cycle(1, tier="ultra", known=False))
        result = assess(tier="ultra", known=known, finals=finals)
        self.assertEqual((result.level, result.low_usage_streak), (WasteLevel.L1, 1))

    def test_gap_beyond_tolerance_is_unknown(self):
        first = cycle(0)
        second = KnownCycle(
            EMAIL,
            "pro",
            START + timedelta(days=90),
            START + timedelta(days=120),
        )
        final_second = FinalCycle(
            EMAIL,
            "pro",
            second.billing_cycle_start,
            second.billing_cycle_end,
            Decimal("10"),
            FinalSource.PRE_RESET,
        )
        result = assess(
            known=(first, second),
            finals=(cycle(0, known=False), final_second),
        )
        self.assertEqual(result.level, WasteLevel.UNKNOWN)

    def test_boundary_corrected_phantom_final_does_not_inflate_streak(self):
        """账期起点漂移产生的幻影完整账期不得把 L1 抬成 L2。"""
        real = FinalCycle(
            EMAIL,
            "pro",
            datetime(2026, 6, 30, tzinfo=UTC),
            datetime(2026, 7, 30, tzinfo=UTC),
            Decimal("0"),
            FinalSource.PRE_RESET,
        )
        phantom = FinalCycle(
            EMAIL,
            "pro",
            datetime(2026, 7, 30, tzinfo=UTC),
            datetime(2026, 8, 30, tzinfo=UTC),
            Decimal("0"),
            FinalSource.PERIODIC_FALLBACK,
        )
        current = KnownCycle(
            EMAIL,
            "pro",
            datetime(2026, 7, 31, tzinfo=UTC),
            datetime(2026, 8, 31, tzinfo=UTC),
        )
        known = (
            KnownCycle(
                EMAIL,
                "pro",
                real.billing_cycle_start,
                real.billing_cycle_end,
            ),
            KnownCycle(
                EMAIL,
                "pro",
                phantom.billing_cycle_start,
                phantom.billing_cycle_end,
            ),
            current,
        )
        result = assess(known=known, finals=(real, phantom))
        self.assertEqual((result.level, result.low_usage_streak), (WasteLevel.L1, 1))
        self.assertEqual(result.data_quality_status, "complete")
