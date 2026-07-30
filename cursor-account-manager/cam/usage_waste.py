"""基于已结算账期的低用量浪费等级计算。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Sequence

from .usage_snapshot_models import (
    FinalCycle,
    KnownCycle,
    WasteAssessment,
    WasteLevel,
)


def _assessment(
    email: str,
    level: WasteLevel,
    streak: int,
    status: str,
    reason: str = "",
) -> WasteAssessment:
    """构造统一格式的浪费评估结果。"""
    return WasteAssessment(
        email=email,
        level=level,
        low_usage_streak=streak,
        data_quality_status=status,
        reason=reason,
    )


def classify_waste(
    *,
    current_plan_tier: str,
    known_cycles: Sequence[KnownCycle],
    final_cycles: Sequence[FinalCycle],
    low_threshold_pct: Decimal,
    continuity_tolerance: timedelta,
) -> WasteAssessment:
    """按当前套餐的连续账期段计算低用量等级。"""
    cycles = tuple(known_cycles)
    finals = tuple(
        sorted(
            final_cycles,
            key=lambda item: item.billing_cycle_end,
            reverse=True,
        )
    )
    if not cycles and not finals:
        raise ValueError("至少需要一个已知账期或最终账期以确定账号")
    email = (cycles or finals)[0].email

    if current_plan_tier == "unknown":
        return _assessment(
            email,
            WasteLevel.UNKNOWN,
            0,
            "unknown",
            "当前套餐档位未知",
        )

    final_keys = {
        (
            item.billing_cycle_start,
            item.billing_cycle_end,
        )
        for item in finals
    }
    now = datetime.now(timezone.utc)
    if any(
        item.billing_cycle_end <= now
        and (item.billing_cycle_start, item.billing_cycle_end) not in final_keys
        for item in cycles
    ):
        return _assessment(
            email,
            WasteLevel.UNKNOWN,
            0,
            "incomplete",
            "已结束账期缺少最终用量",
        )

    if not finals or finals[0].plan_tier != current_plan_tier:
        return _assessment(
            email,
            WasteLevel.UNKNOWN,
            0,
            "incomplete",
            "当前套餐没有完整账期",
        )

    segment: list[FinalCycle] = []
    previous: FinalCycle | None = None
    for item in finals:
        if item.plan_tier != current_plan_tier:
            break

        if previous is not None:
            gap = previous.billing_cycle_start - item.billing_cycle_end
            if abs(gap) > continuity_tolerance:
                return _assessment(
                    email,
                    WasteLevel.UNKNOWN,
                    0,
                    "incomplete",
                    "账期时间不连续",
                )
        segment.append(item)
        previous = item

    streak = 0
    for item in segment:
        if item.total_used_pct >= low_threshold_pct:
            break
        streak += 1

    level = (
        WasteLevel.L0
        if streak == 0
        else WasteLevel.L1
        if streak == 1
        else WasteLevel.L2
        if streak == 2
        else WasteLevel.L3
    )
    return _assessment(email, level, streak, "complete")
