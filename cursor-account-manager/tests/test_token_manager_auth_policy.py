"""TokenManager 认证策略测试。"""

import time
import unittest
from unittest.mock import patch

from cam.models import Account, AuthCircuitOpenError, TokenRecord
from cam.token_manager import TokenManager


class _Store:
    """提供 TokenManager 所需的最小内存存储。"""

    def __init__(self, record):
        self.record = record

    def get(self, _email):
        return self.record


class _DeniedPolicy:
    """模拟已开启的认证熔断器。"""

    def allow_refresh_or_login(self):
        return False


class _FailIfConsultedPolicy:
    """确保有效缓存令牌路径不查询策略。"""

    def allow_refresh_or_login(self):
        raise AssertionError("有效缓存令牌不应查询认证策略")


def _account():
    return Account(
        email="user@example.com",
        imap_password="password",
        imap_host="imap.example.com",
        imap_port=993,
    )


class TokenManagerAuthPolicyTests(unittest.TestCase):
    """验证认证熔断器只阻止新的认证动作。"""

    def test_valid_cached_token_does_not_consult_policy(self):
        manager = TokenManager(
            _Store(
                TokenRecord(
                    email="user@example.com",
                    access_token="cached-token",
                    expires_at=int(time.time()) + 3600,
                )
            )
        )

        token = manager.get_valid_token(_account(), auth_policy=_FailIfConsultedPolicy())

        self.assertEqual(token, "cached-token")

    def test_expired_token_is_denied_before_refresh(self):
        manager = TokenManager(
            _Store(
                TokenRecord(
                    email="user@example.com",
                    access_token="expired-token",
                    refresh_token="refresh-token",
                    expires_at=1,
                )
            )
        )

        with patch("cam.token_manager._refresh_via_api") as refresh:
            with self.assertRaises(AuthCircuitOpenError):
                manager.get_valid_token(_account(), auth_policy=_DeniedPolicy())

        refresh.assert_not_called()

    def test_expired_token_without_refresh_is_denied_before_browser_login(self):
        manager = TokenManager(
            _Store(TokenRecord(email="user@example.com", access_token="expired-token"))
        )

        with patch.object(manager, "_browser_login_and_save") as browser_login:
            with self.assertRaises(AuthCircuitOpenError):
                manager.get_valid_token(_account(), auth_policy=_DeniedPolicy())

        browser_login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
