"""浏览器登录：邮箱/密码页状态识别与跳过只读 fill。"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from cam import browser_login


class _FakeLocator:
    def __init__(self, *, count: int = 0, value: str = "", readonly: bool = False):
        self._count = count
        self._value = value
        self._readonly = readonly

    def count(self) -> int:
        return self._count

    @property
    def first(self):
        return self

    def input_value(self, timeout: int = 0) -> str:
        return self._value

    def get_attribute(self, name: str):
        if name == "readonly":
            return "" if self._readonly else None
        return None

    def wait_for(self, **kwargs):
        return None

    def click(self, timeout: int = 0):
        return None


class BrowserLoginEmailStepTests(unittest.TestCase):
    def test_password_step_with_matching_email_skips_fill(self) -> None:
        page = MagicMock()
        page.url = (
            "https://authenticator.cursor.sh/password?"
            "email=cursor107%40eclicktech.com.cn"
        )
        page.locator.side_effect = lambda sel: (
            _FakeLocator(count=1)
            if sel == browser_login._SEL_MAGIC_BTN
            else _FakeLocator(count=0)
        )
        page.evaluate.return_value = {
            "exists": True,
            "value": "cursor107@eclicktech.com.cn",
            "editable": False,
        }

        with (
            patch.object(browser_login, "_type_like_human") as typed,
            patch.object(browser_login, "_human_pause"),
        ):
            browser_login._submit_email_with_retry(
                page, "cursor107@eclicktech.com.cn", max_retries=1
            )

        typed.assert_not_called()
        page.fill.assert_not_called()

    def test_password_step_with_other_email_clicks_change_email(self) -> None:
        page = MagicMock()
        page.url = "https://authenticator.cursor.sh/password?email=other%40example.com"
        change_btn = MagicMock()
        change_btn.count.return_value = 1
        change_btn.first = change_btn

        states = iter(
            [
                {"exists": True, "value": "other@example.com", "editable": False},
                {"exists": True, "value": "", "editable": True},
            ]
        )

        def locator(sel: str):
            if sel == browser_login._SEL_MAGIC_BTN:
                return _FakeLocator(count=1)
            if sel == browser_login._SEL_CODE_INPUT:
                return _FakeLocator(count=0)
            if "更改电子邮件" in sel or "Change email" in sel:
                return change_btn
            return _FakeLocator(count=1)

        page.locator.side_effect = locator
        page.evaluate.side_effect = lambda *_a, **_k: next(states)

        with (
            patch.object(browser_login, "_type_like_human") as typed,
            patch.object(browser_login, "_human_pause"),
            patch.object(browser_login, "_has_turnstile_challenge", return_value=False),
        ):
            # fill path after change-email: submit then wait next step
            page.wait_for_selector.side_effect = [None, None]

            browser_login._submit_email_with_retry(
                page, "cursor107@eclicktech.com.cn", max_retries=1
            )

        change_btn.click.assert_called()
        page.fill.assert_called()
        typed.assert_called()

    def test_is_password_step_true_for_readonly_email(self) -> None:
        page = MagicMock()
        page.url = "https://authenticator.cursor.sh/"
        page.locator.return_value = _FakeLocator(count=0)
        page.evaluate.return_value = {
            "exists": True,
            "value": "a@b.com",
            "editable": False,
        }
        self.assertTrue(browser_login._is_password_step(page))


if __name__ == "__main__":
    unittest.main()
