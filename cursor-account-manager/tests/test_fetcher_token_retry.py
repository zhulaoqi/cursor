import unittest
from unittest.mock import patch

from cam import fetcher
from cam.models import Account, TokenExpiredError


class FakeManager:
    def __init__(self):
        self.tokens = ["old-token", "new-token"]
        self.get_calls = 0
        self.force_calls = 0
        self.expired = []

    def get_valid_token(self, account):
        self.get_calls += 1
        return "old-token"

    def force_relogin(self, account):
        self.force_calls += 1
        return "new-token"

    def mark_access_token_expired(self, email):
        self.expired.append(email)


class FakeClient:
    def __init__(self, token):
        self.token = token

    def close(self):
        pass

    def get_current_period_usage(self):
        if self.token == "old-token":
            raise TokenExpiredError("old token rejected")
        return {"total": 1}

    def get_plan_info(self):
        return {"plan": "pro", "token": self.token}


class FlakyReloginManager(FakeManager):
    def force_relogin(self, account):
        self.force_calls += 1
        if self.force_calls < 3:
            return "old-token"
        return "new-token"


class FetcherTokenRetryTests(unittest.TestCase):
    def test_fetch_one_refreshes_client_after_usage_401(self):
        manager = FakeManager()
        account = Account(
            email="cursor@eclicktech.com.cn",
            imap_password="pw",
            imap_host="imap.feishu.cn",
            imap_port=993,
        )

        with patch.object(fetcher, "CursorClient", FakeClient):
            snap = fetcher.fetch_one(account, manager=manager, what=("usage", "plan"))

        self.assertEqual(manager.expired, ["cursor@eclicktech.com.cn"])
        self.assertEqual(manager.get_calls, 1)
        self.assertEqual(manager.force_calls, 1)
        self.assertEqual(snap.usage, {"total": 1})
        self.assertEqual(snap.plan, {"plan": "pro", "token": "new-token"})
        self.assertEqual(snap.errors, {})

    def test_fetch_one_retries_forced_relogin_up_to_three_times(self):
        manager = FlakyReloginManager()
        account = Account(
            email="cursor@eclicktech.com.cn",
            imap_password="pw",
            imap_host="imap.feishu.cn",
            imap_port=993,
        )

        with patch.object(fetcher, "CursorClient", FakeClient):
            snap = fetcher.fetch_one(account, manager=manager, what=("usage", "plan"))

        self.assertEqual(manager.force_calls, 3)
        self.assertEqual(snap.usage, {"total": 1})
        self.assertEqual(snap.plan, {"plan": "pro", "token": "new-token"})
        self.assertEqual(snap.errors, {})


class FlakyPlanClient(FakeClient):
    """模拟 plan / usage_limit 接口的瞬时网络/代理错误：前 N 次失败，第 N+1 次成功。"""

    def __init__(self, token):
        super().__init__(token)
        self.plan_attempts = 0
        self.fail_count = 2  # 默认前 2 次失败

    def get_current_period_usage(self):
        return {"total": 1}

    def get_plan_info(self):
        self.plan_attempts += 1
        if self.plan_attempts <= self.fail_count:
            raise ConnectionError(
                "ProxyError('Unable to connect to proxy', "
                "RemoteDisconnected('Remote end closed connection without response'))"
            )
        return {"plan": "pro", "attempts": self.plan_attempts}


class AlwaysFailingPlanClient(FakeClient):
    """模拟 plan 接口持续失败：用于验证 3 次重试后落败。"""

    def __init__(self, token):
        super().__init__(token)
        self.plan_attempts = 0

    def get_current_period_usage(self):
        return {"total": 1}

    def get_plan_info(self):
        self.plan_attempts += 1
        raise ConnectionError("Max retries exceeded with url: ProxyError")


class FetcherTransientNetworkRetryTests(unittest.TestCase):
    """对非认证类瞬时错误（代理断连/超时）应自动重试 3 次再放弃。"""

    def setUp(self):
        # 加速测试：避免真实指数退避
        self._sleep_patch = patch.object(fetcher.time, "sleep", lambda _s: None)
        self._sleep_patch.start()
        self.addCleanup(self._sleep_patch.stop)

    def _account(self):
        return Account(
            email="cursor@eclicktech.com.cn",
            imap_password="pw",
            imap_host="imap.feishu.cn",
            imap_port=993,
        )

    def test_call_retries_transient_network_error_three_times_and_succeeds(self):
        manager = FakeManager()
        flaky_holder = {}

        def make_client(token):
            c = FlakyPlanClient(token)
            flaky_holder["client"] = c
            return c

        with patch.object(fetcher, "CursorClient", make_client):
            snap = fetcher.fetch_one(
                self._account(), manager=manager, what=("plan",)
            )

        self.assertEqual(flaky_holder["client"].plan_attempts, 3,
                         "前 2 次失败，第 3 次应成功")
        self.assertEqual(snap.plan.get("plan"), "pro")
        self.assertEqual(snap.errors, {})

    def test_call_records_error_after_three_failed_retries(self):
        manager = FakeManager()
        always_holder = {}

        def make_client(token):
            c = AlwaysFailingPlanClient(token)
            always_holder["client"] = c
            return c

        with patch.object(fetcher, "CursorClient", make_client):
            snap = fetcher.fetch_one(
                self._account(), manager=manager, what=("plan",)
            )

        self.assertEqual(always_holder["client"].plan_attempts, 3,
                         "持续失败时应尝试 3 次再放弃")
        self.assertIn("plan", snap.errors)
        self.assertIn("ProxyError", snap.errors["plan"])


if __name__ == "__main__":
    unittest.main()
