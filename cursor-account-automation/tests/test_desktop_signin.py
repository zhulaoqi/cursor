"""桌面登录确认：Continue to sign in → Return to Cursor。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop_signin import (
    complete_desktop_signin_steps,
    is_desktop_continue_text,
    is_login_deep_page,
    is_return_to_cursor_text,
    is_unfinished_handoff_url,
    is_web_app_logged_in,
)


class DesktopSigninPredicateTests(unittest.TestCase):
    def test_login_deep_page_url(self) -> None:
        self.assertTrue(is_login_deep_page("https://www.cursor.com/loginDeepPage"))
        self.assertTrue(is_unfinished_handoff_url("https://cursor.com/cn/loginDeepControl"))
        self.assertFalse(is_unfinished_handoff_url("https://cursor.com/cn/dashboard"))
        self.assertTrue(is_web_app_logged_in("https://cursor.com/agents"))
        self.assertTrue(is_web_app_logged_in("https://www.cursor.com/agents"))
        self.assertFalse(is_web_app_logged_in("https://www.cursor.com/loginDeepPage"))

    def test_continue_and_return_copy(self) -> None:
        self.assertTrue(
            is_desktop_continue_text(
                "Sign in to Cursor\n"
                "Click continue to sign in and complete your sign-in to Cursor desktop."
            )
        )
        self.assertTrue(
            is_return_to_cursor_text(
                "https://www.cursor.com/loginDeepPage",
                "All set! Feel free to return to Cursor.",
            )
        )


class DesktopSigninFlowTests(unittest.TestCase):
    def test_clicks_continue_then_return(self) -> None:
        state = {"step": "continue"}

        def get_url() -> str:
            if state["step"] == "continue":
                return "https://authenticator.cursor.sh/radar"
            return "https://www.cursor.com/loginDeepPage"

        def get_body() -> str:
            if state["step"] == "continue":
                return (
                    "Sign in to Cursor\n"
                    "Click continue to sign in and complete your sign-in to Cursor desktop.\n"
                    "Continue to sign in"
                )
            return "All set! Feel free to return to Cursor.\nReturn to Cursor"

        def click_continue() -> bool:
            state["step"] = "deep"
            return True

        def click_return() -> bool:
            state["step"] = "done"
            return True

        ok = complete_desktop_signin_steps(
            get_url=get_url,
            get_body=get_body,
            click_continue=click_continue,
            click_return=click_return,
            pause=lambda: None,
            timeout=3,
            log=lambda _msg: None,
        )
        self.assertTrue(ok)
        self.assertEqual(state["step"], "done")

    def test_agents_page_is_already_logged_in(self) -> None:
        ok = complete_desktop_signin_steps(
            get_url=lambda: "https://cursor.com/agents",
            get_body=lambda: "Start from scratch\nAsk Cursor to build",
            click_continue=lambda: False,
            click_return=lambda: False,
            pause=lambda: None,
            timeout=2,
            log=lambda _msg: None,
        )
        self.assertTrue(ok)


class PlaywrightLoginGuardTests(unittest.TestCase):
    def test_is_logged_in_false_on_login_deep_page(self) -> None:
        try:
            from playwright_registration import is_logged_in
        except ModuleNotFoundError:
            self.skipTest("playwright 未安装")

        page = MagicMock()
        page.url = "https://www.cursor.com/loginDeepPage"
        page.locator.return_value.inner_text.return_value = "All set! Feel free to return to Cursor."
        self.assertFalse(is_logged_in(page))


class DrissionLoginGuardTests(unittest.TestCase):
    def test_is_logged_in_false_on_login_deep_page(self) -> None:
        from registration import is_logged_in

        tab = MagicMock()
        tab.url = "https://www.cursor.com/loginDeepPage"
        tab.ele.return_value.text = "All set! Feel free to return to Cursor."
        self.assertFalse(is_logged_in(tab))


if __name__ == "__main__":
    unittest.main()
