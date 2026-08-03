"""Cursor 用量快照领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, DecimalException
from enum import Enum
import json
from typing import Any

from cam.models import Account


class SnapshotType(str, Enum):
    """快照采集类型。"""

    PERIODIC = "periodic"
    PRE_RESET = "pre_reset"


class CollectionStatus(str, Enum):
    """单账号采集状态。"""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    NOT_COLLECTABLE = "not_collectable"
    ORPHAN_LOCAL_ACCOUNT = "orphan_local_account"
    LOCK_BUSY = "lock_busy"
    AUTH_CIRCUIT_OPEN = "auth_circuit_open"


class AuthOutcome(str, Enum):
    """认证尝试的终态。"""

    SUCCESS = "success"
    AUTH_FAILURE = "auth_failure"
    NON_AUTH_FAILURE = "non_auth_failure"
    SKIPPED = "skipped"


class FinalSource(str, Enum):
    """账期最终记录来源。"""

    PRE_RESET = "pre_reset"
    PERIODIC_FALLBACK = "periodic_fallback"


class WasteLevel(str, Enum):
    """账号浪费等级。"""

    UNKNOWN = "unknown"
    L0 = "l0"
    L1 = "l1"
    L2 = "l2"
    L3 = "l3"


class _FrozenPayload(dict):
    """保持 dict 接口的不可变原始载荷。"""

    def _reject_mutation(self, *args: object, **kwargs: object) -> None:
        """拒绝所有原地变异操作。"""
        raise TypeError("raw_payload 不可修改")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    clear = _reject_mutation
    pop = _reject_mutation
    popitem = _reject_mutation
    setdefault = _reject_mutation
    update = _reject_mutation
    __ior__ = _reject_mutation


def _freeze_payload_value(value: object) -> object:
    """递归复制并冻结 JSON 载荷中的容器。"""
    if isinstance(value, dict):
        return _FrozenPayload(
            (key, _freeze_payload_value(item))
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_payload_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        raise ValueError("raw_payload 不允许 set 或 frozenset")
    return value


def _freeze_raw_payload(value: object) -> dict[str, Any]:
    """冻结原始载荷并确认其仍可序列化为 JSON。"""
    if not isinstance(value, dict):
        raise ValueError("raw_payload 必须是 dict")
    try:
        frozen = _freeze_payload_value(value)
        json.dumps(frozen)
    except (RecursionError, TypeError, ValueError) as exc:
        raise ValueError("raw_payload 必须是可序列化的 JSON 结构") from exc
    return frozen


def _validate_utc_datetime(value: object, field_name: str) -> None:
    """校验值是 UTC 有时区时间，且不转换时区。"""
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} 必须是 datetime")
    try:
        offset = value.utcoffset()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是有效的 UTC 时间") from exc
    if value.tzinfo is None or offset != timedelta(0):
        raise ValueError(f"{field_name} 必须是有时区的 UTC 时间")


def _validate_normalized_email(value: object, field_name: str = "email") -> None:
    """校验邮箱是非空且已经去空白、转小写的字符串。"""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是字符串")
    if not value.strip():
        raise ValueError(f"{field_name} 去除首尾空白后不能为空")
    if value != value.strip():
        raise ValueError(f"{field_name} 必须已经去除首尾空白")
    if value != value.lower():
        raise ValueError(f"{field_name} 必须已经规范化为小写")


def _validate_plan_tier(value: object) -> None:
    """校验套餐档位是非空字符串。"""
    if not isinstance(value, str):
        raise ValueError("plan_tier 必须是字符串")
    if not value.strip():
        raise ValueError("plan_tier 不能为空")


def _normalize_percentage(value: object, field_name: str = "total_used_pct") -> Decimal:
    """将允许的百分比输入转为 Decimal 并校验。"""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field_name} 不接受 bool 或 float")
    if not isinstance(value, (Decimal, str, int)):
        raise ValueError(f"{field_name} 必须是 Decimal、十进制字符串或 int")
    try:
        normalized = Decimal(value)
    except (DecimalException, ValueError) as exc:
        raise ValueError(f"{field_name} 不是有效十进制数") from exc
    if not normalized.is_finite():
        raise ValueError(f"{field_name} 必须是有限 Decimal")
    if not Decimal("0") <= normalized <= Decimal("100"):
        raise ValueError(f"{field_name} 必须在 0 到 100 之间")
    return normalized


def _normalize_optional_percentage(
    value: object,
    field_name: str,
) -> Decimal | None:
    """可选百分比：None/空串保持为空，否则按 0~100 校验。"""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return _normalize_percentage(value, field_name)


def _validate_cycle(
    *,
    email: object,
    plan_tier: object,
    billing_cycle_start: object,
    billing_cycle_end: object,
) -> None:
    """校验账期模型共用的身份、套餐和 UTC 时间字段。"""
    _validate_normalized_email(email)
    _validate_plan_tier(plan_tier)
    _validate_utc_datetime(billing_cycle_start, "billing_cycle_start")
    _validate_utc_datetime(billing_cycle_end, "billing_cycle_end")
    if billing_cycle_end <= billing_cycle_start:
        raise ValueError(
            "billing_cycle_end 必须晚于 billing_cycle_start",
        )


@dataclass(frozen=True, init=False)
class UsageSnapshot:
    """单账号在指定时间槽采集到的用量快照。"""

    email: str
    plan_tier: str
    plan_tier_raw: str
    plan_status: str
    plan_source: str
    billing_cycle_start: datetime
    billing_cycle_end: datetime
    total_used_pct: Decimal
    auto_used_pct: Decimal | None
    api_used_pct: Decimal | None
    snapshot_type: SnapshotType
    snapshot_slot: datetime
    collected_at: datetime
    source_endpoint: str
    parser_version: str
    raw_payload: dict[str, Any]

    def __init__(
        self,
        email: str,
        plan_tier: str,
        plan_tier_raw: str,
        plan_status: str,
        plan_source: str,
        billing_cycle_start: datetime,
        billing_cycle_end: datetime,
        total_used_pct: Decimal | str | int,
        snapshot_type: SnapshotType,
        snapshot_slot: datetime,
        collected_at: datetime,
        source_endpoint: str,
        parser_version: str,
        raw_payload: dict[str, Any],
        auto_used_pct: Decimal | str | int | None = None,
        api_used_pct: Decimal | str | int | None = None,
    ) -> None:
        object.__setattr__(self, "email", email)
        object.__setattr__(self, "plan_tier", plan_tier)
        object.__setattr__(self, "plan_tier_raw", plan_tier_raw)
        object.__setattr__(self, "plan_status", plan_status)
        object.__setattr__(self, "plan_source", plan_source)
        object.__setattr__(
            self,
            "billing_cycle_start",
            billing_cycle_start,
        )
        object.__setattr__(self, "billing_cycle_end", billing_cycle_end)
        object.__setattr__(self, "total_used_pct", total_used_pct)
        object.__setattr__(self, "auto_used_pct", auto_used_pct)
        object.__setattr__(self, "api_used_pct", api_used_pct)
        object.__setattr__(self, "snapshot_type", snapshot_type)
        object.__setattr__(self, "snapshot_slot", snapshot_slot)
        object.__setattr__(self, "collected_at", collected_at)
        object.__setattr__(self, "source_endpoint", source_endpoint)
        object.__setattr__(self, "parser_version", parser_version)
        object.__setattr__(self, "raw_payload", raw_payload)
        self.__post_init__()

    def __post_init__(self) -> None:
        _validate_cycle(
            email=self.email,
            plan_tier=self.plan_tier,
            billing_cycle_start=self.billing_cycle_start,
            billing_cycle_end=self.billing_cycle_end,
        )
        _validate_utc_datetime(self.snapshot_slot, "snapshot_slot")
        _validate_utc_datetime(self.collected_at, "collected_at")
        object.__setattr__(
            self,
            "total_used_pct",
            _normalize_percentage(self.total_used_pct),
        )
        object.__setattr__(
            self,
            "auto_used_pct",
            _normalize_optional_percentage(self.auto_used_pct, "auto_used_pct"),
        )
        object.__setattr__(
            self,
            "api_used_pct",
            _normalize_optional_percentage(self.api_used_pct, "api_used_pct"),
        )
        if not isinstance(self.snapshot_type, SnapshotType):
            raise ValueError("snapshot_type 必须是 SnapshotType")
        object.__setattr__(
            self,
            "raw_payload",
            _freeze_raw_payload(self.raw_payload),
        )


@dataclass(frozen=True)
class CollectionResult:
    """单账号一次采集操作的结果。"""

    email: str
    status: CollectionStatus
    snapshot: UsageSnapshot | None = None
    error_type: str = ""
    error_message: str = ""
    auth_outcome: AuthOutcome = AuthOutcome.SKIPPED

    def __post_init__(self) -> None:
        if not isinstance(self.status, CollectionStatus):
            raise ValueError("status 必须是 CollectionStatus")
        if not isinstance(self.auth_outcome, AuthOutcome):
            raise ValueError("auth_outcome 必须是 AuthOutcome")
        _validate_normalized_email(self.email)
        if self.snapshot is not None and not isinstance(
            self.snapshot,
            UsageSnapshot,
        ):
            raise ValueError("snapshot 必须是 UsageSnapshot")
        if self.status is CollectionStatus.SUCCESS and self.snapshot is None:
            raise ValueError("SUCCESS 状态必须携带 snapshot")
        if self.status is not CollectionStatus.SUCCESS and self.snapshot is not None:
            raise ValueError("非 SUCCESS 状态不得携带 snapshot")
        if (
            self.status is CollectionStatus.SUCCESS
            and self.snapshot is not None
            and self.snapshot.email != self.email
        ):
            raise ValueError("SUCCESS 状态的 snapshot.email 必须与 email 相同")


@dataclass(frozen=True)
class MonitoredAccount:
    """参与用量监控的本地账号及申请信息。"""

    account: Account
    applicant: str
    department: str


@dataclass(frozen=True)
class AccountMappingResult:
    """监控账号与本地账号的映射结果。"""

    collectable_accounts: tuple[MonitoredAccount, ...] = field(default_factory=tuple)
    not_collectable_emails: tuple[str, ...] = field(default_factory=tuple)
    orphan_local_emails: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        fields = (
            ("collectable_accounts", self.collectable_accounts),
            ("not_collectable_emails", self.not_collectable_emails),
            ("orphan_local_emails", self.orphan_local_emails),
        )
        for field_name, value in fields:
            if not isinstance(value, tuple):
                raise ValueError(f"{field_name} 必须是 tuple")
        if not all(
            isinstance(item, MonitoredAccount)
            for item in self.collectable_accounts
        ):
            raise ValueError(
                "collectable_accounts 的元素必须是 MonitoredAccount",
            )
        for field_name, emails in (
            ("not_collectable_emails", self.not_collectable_emails),
            ("orphan_local_emails", self.orphan_local_emails),
        ):
            for email in emails:
                try:
                    _validate_normalized_email(email)
                except ValueError as exc:
                    raise ValueError(
                        f"{field_name} 的元素必须是规范化邮箱",
                    ) from exc


@dataclass(frozen=True)
class BreakerSnapshot:
    """认证熔断器的只读状态快照。"""

    state: str
    sample_count: int
    auth_failure_count: int
    opened_at: datetime | None
    retry_at: datetime | None

    def __post_init__(self) -> None:
        if self.state not in {"closed", "open", "half_open"}:
            raise ValueError("state 仅允许 closed、open 或 half_open")
        if (
            not isinstance(self.sample_count, int)
            or isinstance(self.sample_count, bool)
            or self.sample_count < 0
        ):
            raise ValueError("sample_count 必须是非负整数")
        if (
            not isinstance(self.auth_failure_count, int)
            or isinstance(self.auth_failure_count, bool)
            or self.auth_failure_count < 0
        ):
            raise ValueError("auth_failure_count 必须是非负整数")
        if self.auth_failure_count > self.sample_count:
            raise ValueError("auth_failure_count 不得大于 sample_count")
        if self.opened_at is not None:
            _validate_utc_datetime(self.opened_at, "opened_at")
        if self.retry_at is not None:
            _validate_utc_datetime(self.retry_at, "retry_at")


@dataclass(frozen=True)
class KnownCycle:
    """已知存在的账号账期。"""

    email: str
    plan_tier: str
    billing_cycle_start: datetime
    billing_cycle_end: datetime

    def __post_init__(self) -> None:
        _validate_cycle(
            email=self.email,
            plan_tier=self.plan_tier,
            billing_cycle_start=self.billing_cycle_start,
            billing_cycle_end=self.billing_cycle_end,
        )


@dataclass(frozen=True, init=False)
class FinalCycle:
    """已结算账期及其最终用量。"""

    email: str
    plan_tier: str
    billing_cycle_start: datetime
    billing_cycle_end: datetime
    total_used_pct: Decimal
    final_source: FinalSource

    def __init__(
        self,
        email: str,
        plan_tier: str,
        billing_cycle_start: datetime,
        billing_cycle_end: datetime,
        total_used_pct: Decimal | str | int,
        final_source: FinalSource,
    ) -> None:
        object.__setattr__(self, "email", email)
        object.__setattr__(self, "plan_tier", plan_tier)
        object.__setattr__(
            self,
            "billing_cycle_start",
            billing_cycle_start,
        )
        object.__setattr__(self, "billing_cycle_end", billing_cycle_end)
        object.__setattr__(self, "total_used_pct", total_used_pct)
        object.__setattr__(self, "final_source", final_source)
        self.__post_init__()

    def __post_init__(self) -> None:
        _validate_cycle(
            email=self.email,
            plan_tier=self.plan_tier,
            billing_cycle_start=self.billing_cycle_start,
            billing_cycle_end=self.billing_cycle_end,
        )
        object.__setattr__(
            self,
            "total_used_pct",
            _normalize_percentage(self.total_used_pct),
        )
        if not isinstance(self.final_source, FinalSource):
            raise ValueError("final_source 必须是 FinalSource")


@dataclass(frozen=True)
class WasteAssessment:
    """账号连续低用量的浪费评估。"""

    email: str
    level: WasteLevel
    low_usage_streak: int
    data_quality_status: str
    reason: str = ""

    def __post_init__(self) -> None:
        _validate_normalized_email(self.email)
        if not isinstance(self.level, WasteLevel):
            raise ValueError("level 必须是 WasteLevel")
        if (
            not isinstance(self.low_usage_streak, int)
            or isinstance(self.low_usage_streak, bool)
            or self.low_usage_streak < 0
        ):
            raise ValueError("low_usage_streak 必须是非负整数")
        if (
            not isinstance(self.data_quality_status, str)
            or not self.data_quality_status.strip()
        ):
            raise ValueError("data_quality_status 不能为空")
