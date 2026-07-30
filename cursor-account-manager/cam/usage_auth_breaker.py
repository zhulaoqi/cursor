"""Cursor 用量快照认证熔断器。"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from math import isfinite
import threading
from typing import Callable, Deque

from .usage_snapshot_models import AuthOutcome, BreakerSnapshot


_STATE_CLOSED = "closed"
_STATE_OPEN = "open"
_STATE_HALF_OPEN = "half_open"


def _validate_utc(value: datetime, field_name: str) -> datetime:
    """校验并返回有时区的 UTC 时间。"""
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} 必须是 datetime")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} 必须是 UTC 时间")
    return value


def _normalize_email(email: str) -> str:
    """规范化样本中仅用于诊断归属的邮箱。"""
    if not isinstance(email, str):
        raise ValueError("email 必须是非空字符串")
    normalized = email.strip().lower()
    if not normalized:
        raise ValueError("email 去除空白后不能为空")
    return normalized


class UsageAuthBreaker:
    """按滑动窗口统计认证结果的线程安全熔断器。"""

    def __init__(
        self,
        min_samples: int,
        failure_ratio: float,
        cooldown: timedelta,
        window_size: int,
        window_duration: timedelta,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            not isinstance(min_samples, int)
            or isinstance(min_samples, bool)
            or min_samples < 1
        ):
            raise ValueError("min_samples 必须是正整数")
        if isinstance(failure_ratio, bool) or not isinstance(
            failure_ratio,
            (int, float),
        ):
            raise ValueError("failure_ratio 必须是 0 到 1 之间的有限数字")
        ratio = float(failure_ratio)
        if not isfinite(ratio) or not 0 < ratio <= 1:
            raise ValueError("failure_ratio 必须大于 0 且不大于 1")
        if (
            not isinstance(window_size, int)
            or isinstance(window_size, bool)
            or window_size < min_samples
        ):
            raise ValueError("window_size 必须是不小于 min_samples 的整数")
        for field_name, value in (
            ("cooldown", cooldown),
            ("window_duration", window_duration),
        ):
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise ValueError(f"{field_name} 必须是正 timedelta")
        if clock is not None and not callable(clock):
            raise ValueError("clock 必须可调用")

        self._min_samples = min_samples
        self._failure_ratio = ratio
        self._cooldown = cooldown
        self._window_size = window_size
        self._window_duration = window_duration
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._samples: Deque[tuple[datetime, str, AuthOutcome]] = deque()
        self._state = _STATE_CLOSED
        self._opened_at: datetime | None = None
        self._retry_at: datetime | None = None
        self._probe_in_flight = False
        self._open_alert_emitted = False
        self._lock = threading.Lock()

    def _now(self) -> datetime:
        """取得并校验当前 UTC 时间。"""
        return _validate_utc(self._clock(), "clock 返回值")

    def _prune(self, now: datetime) -> None:
        """按时间与容量清理窗口中的旧样本。"""
        cutoff = now - self._window_duration
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        while len(self._samples) > self._window_size:
            self._samples.popleft()

    def _open(self, now: datetime) -> None:
        """进入新的 open 周期并重置告警去重标记。"""
        self._state = _STATE_OPEN
        self._opened_at = now
        self._retry_at = now + self._cooldown
        self._probe_in_flight = False
        self._open_alert_emitted = False

    def _close_after_probe_success(self) -> None:
        """半开探针成功后关闭熔断器并清空历史样本。"""
        self._state = _STATE_CLOSED
        self._samples.clear()
        self._opened_at = None
        self._retry_at = None
        self._probe_in_flight = False
        self._open_alert_emitted = False

    def _counts(self) -> tuple[int, int]:
        """返回当前窗口的样本数和认证失败数。"""
        failures = sum(
            outcome is AuthOutcome.AUTH_FAILURE
            for _, _, outcome in self._samples
        )
        return len(self._samples), failures

    def allow_cached_token(self) -> bool:
        """缓存 token 不受认证熔断限制。"""
        return True

    def allow_refresh_or_login(self) -> bool:
        """决定是否允许刷新 token 或进行浏览器登录。"""
        with self._lock:
            now = self._now()
            self._prune(now)
            if self._state == _STATE_CLOSED:
                return True
            if self._state == _STATE_OPEN:
                if now < self._retry_at:
                    return False
                self._state = _STATE_HALF_OPEN
            if self._state == _STATE_HALF_OPEN and not self._probe_in_flight:
                self._probe_in_flight = True
                return True
            return False

    def record(
        self,
        outcome: AuthOutcome,
        email: str,
        now: datetime | None = None,
    ) -> None:
        """记录一次最终认证结果，并按状态机迁移熔断状态。"""
        if not isinstance(outcome, AuthOutcome):
            raise ValueError("outcome 必须是 AuthOutcome")
        normalized_email = _normalize_email(email)
        observed_at = self._now() if now is None else _validate_utc(now, "now")
        with self._lock:
            self._prune(observed_at)
            if self._state == _STATE_HALF_OPEN:
                if outcome is AuthOutcome.SUCCESS:
                    self._close_after_probe_success()
                    return
                if outcome is AuthOutcome.AUTH_FAILURE:
                    self._samples.append((observed_at, normalized_email, outcome))
                    self._prune(observed_at)
                    self._open(observed_at)
                    return
                self._probe_in_flight = False
                return

            if outcome not in (
                AuthOutcome.SUCCESS,
                AuthOutcome.AUTH_FAILURE,
            ):
                return
            self._samples.append((observed_at, normalized_email, outcome))
            self._prune(observed_at)
            sample_count, failure_count = self._counts()
            if (
                self._state == _STATE_CLOSED
                and sample_count >= self._min_samples
                and failure_count / sample_count >= self._failure_ratio
            ):
                self._open(observed_at)

    def snapshot(self) -> BreakerSnapshot:
        """返回清理后当前状态的只读快照。"""
        with self._lock:
            self._prune(self._now())
            sample_count, failure_count = self._counts()
            return BreakerSnapshot(
                state=self._state,
                sample_count=sample_count,
                auth_failure_count=failure_count,
                opened_at=self._opened_at,
                retry_at=self._retry_at,
            )

    def should_emit_open_alert(self) -> bool:
        """在每个 open 周期中仅首次允许发出聚合告警。"""
        with self._lock:
            self._prune(self._now())
            if self._state != _STATE_OPEN or self._open_alert_emitted:
                return False
            self._open_alert_emitted = True
            return True
