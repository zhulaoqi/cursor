"""Cursor 用量快照采集器。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .api_client import CursorClient
from .logger import get
from .models import Account, AuthCircuitOpenError, TokenAcquisitionError, TokenExpiredError
from .token_manager import AuthPolicy, TokenManager, get_default_manager
from .usage_snapshot_models import (
    AuthOutcome,
    CollectionResult,
    CollectionStatus,
    SnapshotType,
)
from .usage_snapshot_parser import parse_usage_snapshot


log = get("usage_snapshot")


class PlanTierFallback(Protocol):
    """当接口没有明确套餐名称时提供低频页面兜底。"""

    def resolve(self, account: Account) -> str | None:
        """返回页面解析到的套餐名称，失败时返回空值。"""


class UsageSnapshotCollector:
    """使用已有登录态采集账号的账期和用量。"""

    def __init__(
        self,
        manager: TokenManager | None = None,
        client_factory=CursorClient,
        plan_tier_fallback: PlanTierFallback | None = None,
    ) -> None:
        self._manager = manager or get_default_manager()
        self._client_factory = client_factory
        self._plan_tier_fallback = plan_tier_fallback

    def collect(
        self,
        account: Account,
        *,
        snapshot_type: SnapshotType,
        snapshot_slot: datetime,
        auth_policy: AuthPolicy,
        collected_at: datetime,
    ) -> CollectionResult:
        """采集一个账号；采集失败只返回结果，不伪造业务快照。"""
        try:
            token = self._manager.get_valid_token(
                account,
                auth_policy=auth_policy,
            )
        except AuthCircuitOpenError as exc:
            return CollectionResult(
                email=account.email,
                status=CollectionStatus.AUTH_CIRCUIT_OPEN,
                error_type=type(exc).__name__,
                error_message=str(exc),
                auth_outcome=AuthOutcome.SKIPPED,
            )
        except TokenAcquisitionError as exc:
            return CollectionResult(
                email=account.email,
                status=CollectionStatus.FAILED,
                error_type=type(exc).__name__,
                error_message=str(exc),
                auth_outcome=AuthOutcome.AUTH_FAILURE,
            )

        client = self._client_factory(token)
        try:
            try:
                snapshot = self._read_snapshot(
                    client,
                    account,
                    snapshot_type=snapshot_type,
                    snapshot_slot=snapshot_slot,
                    collected_at=collected_at,
                )
            except TokenExpiredError:
                log.warning(
                    "[%s] 用量接口返回登录失效，作废当前 token 并强制重登",
                    account.email,
                )
                self._manager.mark_access_token_expired(account.email)
                self._close_client(client)
                client = None
                token = self._manager.force_relogin(
                    account, auth_policy=auth_policy,
                )
                client = self._client_factory(token)
                snapshot = self._read_snapshot(
                    client,
                    account,
                    snapshot_type=snapshot_type,
                    snapshot_slot=snapshot_slot,
                    collected_at=collected_at,
                )
            return CollectionResult(
                email=account.email,
                status=CollectionStatus.SUCCESS,
                snapshot=snapshot,
                auth_outcome=AuthOutcome.SUCCESS,
            )
        except TokenExpiredError as exc:
            return CollectionResult(
                email=account.email,
                status=CollectionStatus.FAILED,
                error_type=type(exc).__name__,
                error_message=str(exc),
                auth_outcome=AuthOutcome.AUTH_FAILURE,
            )
        except AuthCircuitOpenError as exc:
            return CollectionResult(
                email=account.email,
                status=CollectionStatus.AUTH_CIRCUIT_OPEN,
                error_type=type(exc).__name__,
                error_message=str(exc),
                auth_outcome=AuthOutcome.SKIPPED,
            )
        except TokenAcquisitionError as exc:
            return CollectionResult(
                email=account.email,
                status=CollectionStatus.FAILED,
                error_type=type(exc).__name__,
                error_message=str(exc),
                auth_outcome=AuthOutcome.AUTH_FAILURE,
            )
        except Exception as exc:
            return CollectionResult(
                email=account.email,
                status=CollectionStatus.FAILED,
                error_type=type(exc).__name__,
                error_message=str(exc),
                auth_outcome=AuthOutcome.NON_AUTH_FAILURE,
            )
        finally:
            self._close_client(client)

    def _read_snapshot(
        self,
        client,
        account: Account,
        *,
        snapshot_type: SnapshotType,
        snapshot_slot: datetime,
        collected_at: datetime,
    ):
        """拉取并解析用量。401/403 原样抛出，由调用方决定是否重登。"""
        usage_payload = client.get_current_period_usage()
        plan_payload = client.get_plan_info()
        stripe_payload = client.get_stripe_info()
        if (
            not any(plan_payload.get(key) for key in ("planName", "plan"))
            and self._plan_tier_fallback is not None
        ):
            fallback_name = self._plan_tier_fallback.resolve(account)
            if fallback_name:
                plan_payload = dict(plan_payload)
                plan_payload["planName"] = fallback_name
        return parse_usage_snapshot(
            email=account.email,
            usage_payload=usage_payload,
            plan_payload=plan_payload,
            stripe_payload=stripe_payload,
            snapshot_type=snapshot_type,
            snapshot_slot=snapshot_slot,
            collected_at=collected_at,
        )

    @staticmethod
    def _close_client(client) -> None:
        if client is None:
            return
        try:
            client.close()
        except Exception:
            log.debug("关闭用量采集客户端失败", exc_info=True)
