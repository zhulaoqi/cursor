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

    def test_auth_success_page_is_complete_without_homepage(self) -> None:
        url = (
            "https://authenticator.cursor.sh/success?"
            "client_redirect_key=01M0Y267ESJSDHKV5EARM4GNQ4"
        )
        self.assertTrue(browser_login._is_auth_success_page(url))
        page = MagicMock()
        page.url = url
        page.locator.return_value = MagicMock(count=lambda: 0)
        self.assertTrue(browser_login._wait_logged_in(page, timeout=1))

    def test_homepage_still_counts_as_logged_in(self) -> None:
        page = MagicMock()
        page.url = "https://cursor.com/cn/dashboard"
        self.assertTrue(browser_login._wait_logged_in(page, timeout=1))

    def test_agents_page_counts_as_login_success_without_desktop_buttons(self) -> None:
        """验证码后若已到 /agents 聊天页，就是已登录，不能再等 Continue/Return。"""
        page = MagicMock()
        page.url = "https://cursor.com/agents"
        page.locator.return_value = _FakeLocator(count=0)
        page.context.cookies.return_value = []
        with patch.object(browser_login, "_human_pause"):
            ok = browser_login._complete_desktop_signin_steps(page, timeout=2)
        self.assertTrue(ok)
        self.assertTrue(browser_login._is_web_app_logged_in(page.url))
        self.assertTrue(browser_login._wait_logged_in(page, timeout=1))

    def test_success_page_without_cookie_uses_deep_control_poll(self) -> None:
        page = MagicMock()
        page.context.cookies.return_value = []
        with (
            patch.object(browser_login, "_generate_pkce", return_value={"s": "ver", "n": "ch", "r": "uuid-1"}),
            patch.object(browser_login, "_click_deep_control_confirm"),
            patch.object(browser_login, "_human_pause"),
            patch.object(
                browser_login,
                "_poll_token",
                return_value=("access-token", "refresh-token"),
            ) as poll,
        ):
            access, refresh = browser_login._resolve_session_tokens(page, proxy=None)

        self.assertEqual((access, refresh), ("access-token", "refresh-token"))
        self.assertTrue(page.goto.called)
        self.assertIn("loginDeepControl", page.goto.call_args[0][0])
        poll.assert_called_once()

    def test_cookie_token_still_preferred(self) -> None:
        page = MagicMock()
        page.context.cookies.return_value = [
            {"name": "WorkosCursorSessionToken", "value": "user%3A%3Ajwt"},
        ]
        with patch.object(browser_login, "_poll_token") as poll:
            access, refresh = browser_login._resolve_session_tokens(page, proxy=None)
        self.assertEqual(access, "user%3A%3Ajwt")
        self.assertEqual(refresh, "")
        poll.assert_not_called()

    def test_deep_control_waits_ready_and_polls_even_if_click_misses(self) -> None:
        """页面慢加载时不能立刻放弃；点不到确认按钮仍应继续 auth/poll。"""
        page = MagicMock()
        page.context.cookies.return_value = []
        with (
            patch.object(browser_login, "_generate_pkce", return_value={"s": "ver", "n": "ch", "r": "uuid-1"}),
            patch.object(browser_login, "_wait_deep_control_ready", return_value=True) as wait_ready,
            patch.object(
                browser_login,
                "_click_deep_control_confirm",
                side_effect=browser_login.BrowserLoginError("未找到按钮"),
            ),
            patch.object(browser_login, "_human_pause"),
            patch.object(
                browser_login,
                "_poll_token",
                return_value=("access-token", "refresh-token"),
            ) as poll,
        ):
            access, refresh = browser_login._resolve_session_tokens(page, proxy=None)

        wait_ready.assert_called_once()
        poll.assert_called_once()
        self.assertEqual((access, refresh), ("access-token", "refresh-token"))

    def test_wait_deep_control_ready_looks_for_logged_in_text(self) -> None:
        page = MagicMock()
        seen: list[str] = []

        def wait_for_selector(sel: str, timeout: int = 0):
            seen.append(sel)
            if "logged in as" in sel.lower():
                return True
            raise TimeoutError(sel)

        page.wait_for_selector.side_effect = wait_for_selector
        self.assertTrue(browser_login._wait_deep_control_ready(page, timeout_ms=200))
        self.assertTrue(any("logged in as" in sel.lower() for sel in seen))

    def test_click_deep_control_prefers_layout_script(self) -> None:
        page = MagicMock()
        page.evaluate.return_value = "layout"
        browser_login._click_deep_control_confirm(page)
        page.evaluate.assert_called_once()
        page.locator.assert_not_called()

    def test_login_deep_page_is_not_already_logged_in(self) -> None:
        page = MagicMock()
        page.url = "https://www.cursor.com/loginDeepPage"
        page.locator.return_value = _FakeLocator(count=0)
        page.context.cookies.return_value = []
        self.assertTrue(browser_login._is_login_deep_page(page.url))
        self.assertFalse(browser_login._wait_logged_in(page, timeout=1))

    def test_complete_desktop_steps_clicks_continue_then_return(self) -> None:
        """验证码后必须点 Continue to sign in，再到 loginDeepPage 点 Return。"""
        state = {"step": "continue"}

        class _Btn:
            def __init__(self, name: str):
                self.name = name

            def count(self) -> int:
                if self.name == "Continue to sign in":
                    return 1 if state["step"] == "continue" else 0
                if self.name == "Return to Cursor":
                    return 1 if state["step"] == "deep" else 0
                return 0

            @property
            def first(self):
                return self

            def click(self, timeout: int = 0) -> None:
                if self.name == "Continue to sign in":
                    state["step"] = "deep"
                elif self.name == "Return to Cursor":
                    state["step"] = "done"

        page = MagicMock()

        def locator(sel: str):
            if "Continue to sign in" in sel:
                return _Btn("Continue to sign in")
            if "Return to Cursor" in sel:
                return _Btn("Return to Cursor")
            body = MagicMock()
            if state["step"] == "continue":
                body.inner_text.return_value = (
                    "Sign in to Cursor\n"
                    "Click continue to sign in and complete your sign-in to Cursor desktop.\n"
                    "Cancel\nContinue to sign in"
                )
            elif state["step"] == "deep":
                body.inner_text.return_value = (
                    "All set! Feel free to return to Cursor.\nReturn to Cursor"
                )
            else:
                body.inner_text.return_value = ""
            return body

        page.locator.side_effect = locator
        page.context.cookies.return_value = []

        def current_url(_page=None):
            if state["step"] == "continue":
                return "https://authenticator.cursor.sh/radar"
            if state["step"] == "deep":
                return "https://www.cursor.com/loginDeepPage"
            return "https://cursor.com/"

        with (
            patch.object(browser_login, "_current_url", side_effect=current_url),
            patch.object(browser_login, "_human_pause"),
        ):
            ok = browser_login._complete_desktop_signin_steps(page, timeout=3)

        self.assertTrue(ok)
        self.assertEqual(state["step"], "done")


if __name__ == "__main__":
    unittest.main()
