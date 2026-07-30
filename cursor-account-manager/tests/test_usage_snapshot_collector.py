"""用量快照采集器测试。"""

from datetime import datetime, timezone
import unittest

from cam.models import Account, TokenAcquisitionError, TokenExpiredError
from cam.usage_snapshot_collector import UsageSnapshotCollector
from cam.usage_snapshot_models import (
    AuthOutcome,
    CollectionStatus,
    SnapshotType,
)


def _account():
    return Account(
        email="user@example.com",
        imap_password="password",
        imap_host="imap.example.com",
        imap_port=993,
    )


def _collect(collector):
    return collector.collect(
        _account(),
        snapshot_type=SnapshotType.PERIODIC,
        snapshot_slot=datetime(2025, 7, 1, tzinfo=timezone.utc),
        collected_at=datetime(2025, 7, 2, tzinfo=timezone.utc),
        auth_policy=_AllowPolicy(),
    )


class _AllowPolicy:
    """允许测试采集动作。"""

    def allow_refresh_or_login(self):
        return True


class _Manager:
    """返回固定令牌或抛出预设异常。"""

    def __init__(self, token="token", error=None):
        self.token = token
        self.error = error

    def get_valid_token(self, _account, *, auth_policy):
        if self.error is not None:
            raise self.error
        return self.token


class _Client:
    """模拟三个 Cursor 数据接口和资源关闭。"""

    def __init__(self, *, usage=None, plan=None, stripe=None):
        self.usage = usage if usage is not None else {
            "billingCycleStart": 1751328000000,
            "billingCycleEnd": 1754006400000,
            "planUsage": {"totalPercentUsed": 12},
        }
        self.plan = plan if plan is not None else {"planName": "Pro"}
        self.stripe = stripe if stripe is not None else {"subscriptionStatus": "active"}
        self.closed = False

    def get_current_period_usage(self):
        if isinstance(self.usage, Exception):
            raise self.usage
        return self.usage

    def get_plan_info(self):
        if isinstance(self.plan, Exception):
            raise self.plan
        return self.plan

    def get_stripe_info(self):
        if isinstance(self.stripe, Exception):
            raise self.stripe
        return self.stripe

    def close(self):
        self.closed = True


class _Fallback:
    """记录页面套餐兜底是否被调用。"""

    def __init__(self, value):
        self.value = value
        self.calls = 0

    def resolve(self, _account):
        self.calls += 1
        return self.value


class UsageSnapshotCollectorTests(unittest.TestCase):
    """验证采集结果分类、页面兜底和资源释放。"""

    def test_collects_successful_api_responses(self):
        client = _Client()
        collector = UsageSnapshotCollector(
            manager=_Manager(),
            client_factory=lambda _token: client,
        )

        result = _collect(collector)

        self.assertEqual(result.status, CollectionStatus.SUCCESS)
        self.assertEqual(result.auth_outcome, AuthOutcome.SUCCESS)
        self.assertEqual(result.snapshot.plan_tier, "pro")
        self.assertTrue(client.closed)

    def test_token_acquisition_failure_is_auth_failure(self):
        collector = UsageSnapshotCollector(
            manager=_Manager(error=TokenAcquisitionError("认证失败")),
            client_factory=lambda _token: self.fail("不应创建客户端"),
        )

        result = _collect(collector)

        self.assertEqual(result.status, CollectionStatus.FAILED)
        self.assertEqual(result.auth_outcome, AuthOutcome.AUTH_FAILURE)
        self.assertEqual(result.error_type, "TokenAcquisitionError")

    def test_api_auth_failure_is_reported_and_client_is_closed(self):
        client = _Client(usage=TokenExpiredError("401"))
        collector = UsageSnapshotCollector(
            manager=_Manager(),
            client_factory=lambda _token: client,
        )

        result = _collect(collector)

        self.assertEqual(result.auth_outcome, AuthOutcome.AUTH_FAILURE)
        self.assertEqual(result.error_type, "TokenExpiredError")
        self.assertTrue(client.closed)

    def test_api_error_is_non_auth_failure_and_client_is_closed(self):
        client = _Client(plan=ConnectionError("接口不可用"))
        collector = UsageSnapshotCollector(
            manager=_Manager(),
            client_factory=lambda _token: client,
        )

        result = _collect(collector)

        self.assertEqual(result.status, CollectionStatus.FAILED)
        self.assertEqual(result.auth_outcome, AuthOutcome.NON_AUTH_FAILURE)
        self.assertEqual(result.error_type, "ConnectionError")
        self.assertTrue(client.closed)

    def test_page_fallback_supplies_plan_name_when_apis_lack_one(self):
        client = _Client(plan={}, stripe={})
        fallback = _Fallback("Pro+")
        collector = UsageSnapshotCollector(
            manager=_Manager(),
            client_factory=lambda _token: client,
            plan_tier_fallback=fallback,
        )

        result = _collect(collector)

        self.assertEqual(result.status, CollectionStatus.SUCCESS)
        self.assertEqual(result.snapshot.plan_tier, "pro_plus")
        self.assertEqual(fallback.calls, 1)


if __name__ == "__main__":
    unittest.main()
