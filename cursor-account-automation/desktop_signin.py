"""验证码后的桌面登录确认：Continue to sign in → Return to Cursor。

Cursor 改版后，填完邮箱验证码还会出现两步，点完才算登录成功。
DrissionPage / Playwright 两套登录都走这里。
"""

from __future__ import annotations

import time
from typing import Callable


CONTINUE_BUTTON_TEXTS = ("Continue to sign in", "继续登录")
RETURN_BUTTON_TEXTS = ("Return to Cursor", "返回 Cursor")


def is_login_deep_page(url: str) -> bool:
    return "loginDeepPage" in (url or "")


def is_unfinished_handoff_url(url: str) -> bool:
    text = url or ""
    return is_login_deep_page(text) or "loginDeepControl" in text


def is_web_app_logged_in(url: str) -> bool:
    """主站已登录页。现在默认落到 /agents，不再是 dashboard。"""
    text = url or ""
    if is_unfinished_handoff_url(text) or "authenticator.cursor.sh" in text:
        return False
    if "cursor.com" not in text:
        return False
    return any(
        part in text
        for part in (
            "/agents",
            "/dashboard",
            "/settings",
            "/billing",
            "/home",
            "/cn/",
            "/en/",
        )
    )


def is_desktop_continue_text(body: str) -> bool:
    text = body or ""
    return (
        "Continue to sign in" in text
        or "complete your sign-in to Cursor desktop" in text
        or "继续登录" in text
    )


def is_return_to_cursor_text(url: str, body: str) -> bool:
    if is_login_deep_page(url):
        return True
    text = body or ""
    return "All set" in text and (
        "Return to Cursor" in text or "返回 Cursor" in text
    )


def complete_desktop_signin_steps(
    *,
    get_url: Callable[[], str],
    get_body: Callable[[], str],
    click_continue: Callable[[], bool],
    click_return: Callable[[], bool],
    pause: Callable[[], None] | None = None,
    timeout: int = 60,
    log: Callable[[str], None] = print,
) -> bool:
    """点完 Continue to sign in 和 Return to Cursor 后返回 True。"""
    deadline = time.time() + timeout
    clicked_continue = False
    clicked_return = False
    url = ""
    log("[登录] 验证码后等待桌面确认（Continue to sign in → Return to Cursor）")

    while time.time() < deadline:
        url = get_url() or ""
        body = get_body() or ""
        if is_web_app_logged_in(url):
            log(f"[登录] 已进入主站（{url[:120]}），无需再点桌面确认")
            return True
        if not clicked_continue and is_desktop_continue_text(body):
            if click_continue():
                log("[登录] 已点击 Continue to sign in")
                clicked_continue = True
                if pause:
                    pause()
                continue
        if not clicked_return and is_return_to_cursor_text(url, body):
            if click_return():
                log("[登录] 已点击 Return to Cursor（loginDeepPage）")
                clicked_return = True
                if pause:
                    pause()
                return True
        if clicked_continue and clicked_return:
            return True
        time.sleep(0.4)

    log(
        f"[登录] 桌面确认未完成 continue={clicked_continue} "
        f"return={clicked_return} url={url[:160]}"
    )
    return False
