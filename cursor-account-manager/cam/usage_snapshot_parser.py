"""Cursor 用量接口数据解析。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Mapping

from .usage_snapshot_models import SnapshotType, UsageSnapshot


PARSER_VERSION = "usage-v1"

_SENSITIVE_KEYS = {
    "access_token",
    "authorization",
    "cookie",
    "imap_password",
    "password",
    "refresh_token",
    "token",
}


def normalize_plan_tier(raw: object) -> tuple[str, str]:
    """按明确套餐名称规范化档位，绝不按金额推断。"""
    text = str(raw or "").strip()
    normalized = re.sub(r"[\s_-]+", "", text).lower()
    if normalized in {"pro"}:
        return "pro", text
    if normalized in {"pro+", "proplus"}:
        return "pro_plus", text
    if normalized in {"ultra"}:
        return "ultra", text
    if normalized in {"free", "hobby", "none", "notenabled"}:
        return "free", text
    if normalized in {"team", "business", "enterprise"}:
        return normalized, text
    return "unknown", text


def _parse_millisecond_timestamp(value: object, field_name: str) -> datetime:
    """解析毫秒级 Unix 时间戳为 UTC 时间。"""
    if isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是毫秒时间戳")
    try:
        milliseconds = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是毫秒时间戳") from exc
    if milliseconds <= 0:
        raise ValueError(f"{field_name} 必须是正毫秒时间戳")
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)


def _parse_percent(value: object) -> Decimal:
    """解析 0 到 100 的百分比，不猜测 0 到 1 的量纲。"""
    if isinstance(value, bool):
        raise ValueError("totalPercentUsed 必须是 0 到 100 的百分比")
    try:
        percent = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("totalPercentUsed 必须是 0 到 100 的百分比") from exc
    if not percent.is_finite() or not Decimal("0") <= percent <= Decimal("100"):
        raise ValueError("totalPercentUsed 必须是 0 到 100 的百分比")
    return percent


def sanitize_payload(value: Any) -> Any:
    """递归删除原始响应中的凭据字段。"""
    if isinstance(value, Mapping):
        return {
            str(key): "<已脱敏>"
            if str(key).lower() in _SENSITIVE_KEYS
            else sanitize_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_payload(item) for item in value]
    return value


def _first_text(payload: Mapping[str, Any], *keys: str) -> str:
    """按优先级读取第一个非空套餐或状态文本字段。"""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def parse_usage_snapshot(
    *,
    email: str,
    usage_payload: Mapping[str, Any],
    plan_payload: Mapping[str, Any],
    stripe_payload: Mapping[str, Any],
    snapshot_type: SnapshotType,
    snapshot_slot: datetime,
    collected_at: datetime,
) -> UsageSnapshot:
    """将三类接口响应转换为可入库的用量快照。"""
    if not isinstance(usage_payload, Mapping):
        raise ValueError("usage_payload 必须是字典")
    if not isinstance(plan_payload, Mapping):
        raise ValueError("plan_payload 必须是字典")
    if not isinstance(stripe_payload, Mapping):
        raise ValueError("stripe_payload 必须是字典")

    plan_usage = usage_payload.get("planUsage")
    if not isinstance(plan_usage, Mapping):
        raise ValueError("usage_payload 缺少 planUsage")
    cycle_start = _parse_millisecond_timestamp(
        usage_payload.get("billingCycleStart"),
        "billingCycleStart",
    )
    cycle_end = _parse_millisecond_timestamp(
        usage_payload.get("billingCycleEnd"),
        "billingCycleEnd",
    )
    percent = _parse_percent(plan_usage.get("totalPercentUsed"))

    raw_plan_name = _first_text(
        plan_payload,
        "planName",
        "plan",
        "membershipType",
        "individualMembershipType",
    )
    plan_source = "api"
    if not raw_plan_name:
        raw_plan_name = _first_text(
            stripe_payload,
            "individualMembershipType",
            "membershipType",
            "planName",
        )
        plan_source = "stripe" if raw_plan_name else "unknown"
    plan_tier, plan_tier_raw = normalize_plan_tier(raw_plan_name)
    plan_status = _first_text(
        plan_payload,
        "subscriptionStatus",
        "status",
    ) or _first_text(stripe_payload, "subscriptionStatus", "status") or "unknown"

    return UsageSnapshot(
        email=email.strip().lower(),
        plan_tier=plan_tier,
        plan_tier_raw=plan_tier_raw,
        plan_status=plan_status.lower(),
        plan_source=plan_source,
        billing_cycle_start=cycle_start,
        billing_cycle_end=cycle_end,
        total_used_pct=percent,
        snapshot_type=snapshot_type,
        snapshot_slot=snapshot_slot,
        collected_at=collected_at,
        source_endpoint="GetCurrentPeriodUsage,GetPlanInfo,GetStripeInfo",
        parser_version=PARSER_VERSION,
        raw_payload=sanitize_payload(
            {
                "usage": dict(usage_payload),
                "plan": dict(plan_payload),
                "stripe": dict(stripe_payload),
            }
        ),
    )
