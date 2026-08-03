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


def is_billing_cycle_boundary_correction(
    *,
    old_start: datetime,
    old_end: datetime,
    new_start: datetime,
    continuity_tolerance: timedelta,
) -> bool:
    """判断新账期起点是否只是 Cursor 对旧窗口的边界微调。

    典型场景：旧窗 07-30/08-30，次日 API 改报 07-31/08-31。
    起点漂移落在连续性容差内，且新起点仍落在旧窗结束前，应视为修正而非整月切换。
    """
    if new_start <= old_start:
        return False
    if new_start - old_start > continuity_tolerance:
        return False
    return new_start < old_end


def is_cycle_superseded_by_boundary_correction(
    *,
    cycle_start: datetime,
    cycle_end: datetime,
    other_cycle_starts: Sequence[datetime],
    continuity_tolerance: timedelta,
) -> bool:
    """若存在更新且重叠的账期起点，则该账期被边界修正覆盖。"""
    for other_start in other_cycle_starts:
        if is_billing_cycle_boundary_correction(
            old_start=cycle_start,
            old_end=cycle_end,
            new_start=other_start,
            continuity_tolerance=continuity_tolerance,
        ):
            return True
    return False


def filter_authoritative_cycles(
    cycles: Sequence[KnownCycle | FinalCycle],
    *,
    all_cycle_starts: Sequence[datetime],
    continuity_tolerance: timedelta,
) -> list:
    """剔除被账期边界修正覆盖的幻影账期。"""
    return [
        item
        for item in cycles
        if not is_cycle_superseded_by_boundary_correction(
            cycle_start=item.billing_cycle_start,
            cycle_end=item.billing_cycle_end,
            other_cycle_starts=all_cycle_starts,
            continuity_tolerance=continuity_tolerance,
        )
    ]


def classify_waste(
    *,
    current_plan_tier: str,
    known_cycles: Sequence[KnownCycle],
    final_cycles: Sequence[FinalCycle],
    low_threshold_pct: Decimal,
    continuity_tolerance: timedelta,
) -> WasteAssessment:
    """按当前套餐的连续账期段计算低用量等级。"""
    raw_cycles = tuple(known_cycles)
    raw_finals = tuple(final_cycles)
    if not raw_cycles and not raw_finals:
        raise ValueError("至少需要一个已知账期或最终账期以确定账号")
    email = (raw_cycles or raw_finals)[0].email
    all_starts = tuple(
        {
            item.billing_cycle_start
            for item in (*raw_cycles, *raw_finals)
        }
    )
    cycles = tuple(
        filter_authoritative_cycles(
            raw_cycles,
            all_cycle_starts=all_starts,
            continuity_tolerance=continuity_tolerance,
        )
    )
    finals = tuple(
        sorted(
            filter_authoritative_cycles(
                raw_finals,
                all_cycle_starts=all_starts,
                continuity_tolerance=continuity_tolerance,
            ),
            key=lambda item: item.billing_cycle_end,
            reverse=True,
        )
    )

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
