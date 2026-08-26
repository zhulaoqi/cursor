# playwright_registration.py - 注册 / 登录 / Turnstile（Playwright 版）
#
# 对标 registration.py，使用 Playwright API 实现。
# 核心区别：
#   - 使用 page.locator() / page.fill() 替代 DrissionPage 选择器
#   - 使用 page.evaluate() 替代 tab.run_js()
#   - Shadow DOM 穿透使用 Playwright 内置 >> 选择器
#   - 行为模拟更真实（Playwright 的 mouse.move 支持 steps 参数）

from __future__ import annotations

import os
import random
import string
import time

from playwright.sync_api import Page

from browser_playwright import simulate_human_behavior, simulate_typing


SIGN_UP_URL = "https://authenticator.cursor.sh/sign-up"
LOGIN_URL = "https://authenticator.cursor.sh/sign-in"


def generate_password(length: int = 12) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(random.choices(chars, k=length))


def _has_capsolver() -> bool:
    return bool(os.environ.get("CAPSOLVER_API_KEY", "").strip())


def _has_twocaptcha() -> bool:
    return bool(os.environ.get("TWOCAPTCHA_API_KEY", "").strip())


def _random_wait(low: float = 0.3, high: float = 1.0) -> None:
    time.sleep(random.uniform(low, high))


# ═══════════════════════════════════════════════════════════════════════
# Turnstile 处理
# ═══════════════════════════════════════════════════════════════════════

def _get_turnstile_response(page: Page) -> str:
    """检查 Turnstile 是否已返回有效 token。"""
    try:
        return page.evaluate("""() => {
            try {
                const resp = window.turnstile && turnstile.getResponse && turnstile.getResponse();
                if (resp && resp.length > 20) return resp;
            } catch(e) {}
            const sels = [
                'input[name="cf-turnstile-response"]',
                '[name="cf-chl-turnstile-response"]',
            ];
            for (const sel of sels) {
                const el = document.querySelector(sel);
                if (el && el.value && el.value.length > 20) return el.value;
            }
            return '';
        }""") or ""
    except Exception:
        return ""


def _wait_for_turnstile_init(page: Page, timeout: int = 30) -> bool:
    """等待 Turnstile 脚本加载并初始化。"""
    for _ in range(timeout):
        try:
            state = page.evaluate("""() => {
                if (window.turnstile && typeof window.turnstile.render === 'function')
                    return 'ready';
                return 'waiting';
            }""")
            if state == "ready":
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _wait_for_turnstile_token(page: Page, timeout: int = 15) -> bool:
    """等待 Turnstile invisible challenge 完成。"""
    for _ in range(timeout):
        token = _get_turnstile_response(page)
        if token:
            return True
        time.sleep(1)
    return False


def _try_click_turnstile_checkbox(page: Page) -> bool:
    """尝试通过 iframe 找到并点击 Turnstile 复选框。"""
    try:
        frames = page.frames
        for frame in frames:
            if "challenges.cloudflare.com" not in (frame.url or ""):
                continue
            try:
                checkbox = frame.locator("input[type='checkbox']")
                if checkbox.count() > 0:
                    simulate_human_behavior(page)
                    time.sleep(random.uniform(0.5, 1.5))
                    checkbox.first.click()
                    print("[Turnstile] 点击了验证复选框 (iframe)")
                    time.sleep(3)
                    return True
            except Exception:
                continue
    except Exception:
        pass

    try:
        cf_widgets = page.locator("[id^='cf-chl-widget'], .cf-turnstile, #cf-turnstile")
        for i in range(cf_widgets.count()):
            try:
                widget = cf_widgets.nth(i)
                widget.click(force=True)
                print("[Turnstile] 点击了验证 widget")
                time.sleep(3)
                return True
            except Exception:
                continue
    except Exception:
        pass

    return False


def _solve_turnstile_external(page: Page, timeout: int = 60, reason: str = "") -> bool:
    """使用外部求解器（2Captcha 优先，CapSolver 备选）。"""
    prefix = f"[Turnstile] {reason} " if reason else "[Turnstile] "

    if _has_twocaptcha():
        try:
            from twocaptcha_solver import solve_and_inject_turnstile
            print(f"{prefix}使用 2Captcha 求解...")
            if solve_and_inject_turnstile(page, timeout=timeout):
                return True
        except Exception as e:
            print(f"{prefix}2Captcha 失败: {e}")

    if _has_capsolver():
        try:
            from turnstile_solver import get_turnstile_params, solve_turnstile, inject_turnstile_token
            print(f"{prefix}使用 CapSolver 求解...")
            params = _get_turnstile_params_pw(page) or {}
            sitekey = (params.get("sitekey") or "").strip()
            if sitekey:
                token = solve_turnstile(page.url, sitekey, timeout=timeout)
                return _inject_token_pw(page, token)
        except Exception as e:
            print(f"{prefix}CapSolver 失败: {e}")

    return False


def _get_turnstile_params_pw(page: Page) -> dict:
    """从页面提取 Turnstile 参数（Playwright 版）。"""
    try:
        return page.evaluate("""() => {
            const result = { sitekey: '', action: '', cdata: '', source: '' };
            // observer 捕获
            try {
                const ct = window.__capturedTurnstile;
                if (ct && ct.sitekey && ct.sitekey.length > 10) {
                    result.sitekey = ct.sitekey;
                    result.source = ct.source || 'observer';
                    return result;
                }
            } catch(e) {}
            // data-sitekey 属性
            for (const el of document.querySelectorAll('[data-sitekey]')) {
                const key = el.getAttribute('data-sitekey');
                if (key && key.length > 10) {
                    result.sitekey = key;
                    result.action = el.getAttribute('data-action') || '';
                    result.cdata = el.getAttribute('data-cdata') || '';
                    result.source = 'data-attr';
                    return result;
                }
            }
            // iframe src
            for (const f of document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]')) {
                try {
                    const u = new URL(f.src);
                    const k = u.searchParams.get('k') || u.searchParams.get('sitekey');
                    if (k && k.length > 10) {
                        result.sitekey = k;
                        result.source = 'iframe-src';
                        return result;
                    }
                } catch(e) {}
            }
            return result;
        }""") or {}
    except Exception:
        return {}


def _inject_token_pw(page: Page, token: str) -> bool:
    """将外部获取的 Turnstile token 注入页面。"""
    try:
        result = page.evaluate(f"""(token) => {{
            let injected = false;
            for (const sel of ['input[name="cf-turnstile-response"]', '[name="cf-chl-turnstile-response"]']) {{
                const el = document.querySelector(sel);
                if (el) {{ el.value = token; injected = true; }}
            }}
            for (const w of document.querySelectorAll('[id^="cf-chl-widget"], .cf-turnstile, #cf-turnstile')) {{
                for (const inp of w.querySelectorAll('input[type="hidden"]')) {{
                    inp.value = token; injected = true;
                }}
            }}
            try {{ if (window.turnstile) {{ window.turnstile.getResponse = () => token; injected = true; }} }} catch(e) {{}}
            try {{
                for (const el of document.querySelectorAll('[data-callback]')) {{
                    const cb = el.getAttribute('data-callback');
                    if (cb && window[cb]) {{ window[cb](token); injected = true; }}
                }}
            }} catch(e) {{}}
            return injected;
        }}""", token)
        print(f"[Turnstile] token 已注入 (injected={result})")
        return True
    except Exception as e:
        print(f"[Turnstile] token 注入失败: {e}")
        return False


def handle_turnstile(page: Page, max_retries: int = 3) -> bool:
    """处理 Cloudflare Turnstile（通用版 - Playwright）。"""
    print("[Turnstile] 检测验证状态...")

    if _solve_turnstile_external(page, reason="通用验证页"):
        return True

    for retry in range(1, max_retries + 1):
        try:
            page.evaluate("try { turnstile.reset() } catch(e) { }")
        except Exception:
            pass

        simulate_human_behavior(page)
        time.sleep(random.uniform(1, 3))

        if _try_click_turnstile_checkbox(page):
            if _check_page_advanced(page):
                print("[Turnstile] 验证通过")
                return True

        if _check_page_advanced(page):
            print("[Turnstile] 已自动通过")
            return True

        time.sleep(random.uniform(1, 2))

    print(f"[Turnstile] {max_retries} 次重试后未通过")
    return False


def _check_page_advanced(page: Page) -> bool:
    """检查页面是否已推进到下一步。"""
    for selector in [
        '[name="first_name"]', '[name="password"]',
        '[data-index="0"]', 'text=Account Settings',
    ]:
        try:
            if page.locator(selector).first.is_visible(timeout=1000):
                return True
        except Exception:
            continue
    return False


# ═══════════════════════════════════════════════════════════════════════
# 注册
# ═══════════════════════════════════════════════════════════════════════

def navigate_to_signup(page: Page) -> None:
    print("[注册] 打开注册页...")
    page.goto(SIGN_UP_URL, timeout=30000, wait_until="domcontentloaded")
    time.sleep(random.uniform(3, 5))

    try:
        page.wait_for_selector('[name="first_name"]', timeout=5000)
    except Exception:
        handle_turnstile(page)

    try:
        page.wait_for_selector('[name="first_name"]', timeout=15000)
    except Exception:
        raise RuntimeError(
            f"注册页表单未加载。URL: {page.url[:100]}\n"
            "建议：换代理 IP 或等待 30 分钟后重试"
        )

    print(f"[注册] 当前页面: {page.url[:80]}")


def fill_email_and_continue(
    page: Page, email: str,
    first_name: str = "", last_name: str = "",
    use_email_code: bool = True,
) -> None:
    """注册第一步：填姓名、邮箱，点继续。"""
    time.sleep(random.uniform(1.5, 3.0))
    old_url = page.url

    if page.locator('[name="first_name"]').count() > 0:
        simulate_human_behavior(page)
        time.sleep(random.uniform(0.5, 1.0))

        simulate_typing(page, '[name="first_name"]', first_name)
        print(f"[注册] 名: {first_name}")
        time.sleep(random.uniform(1.5, 3.0))

        simulate_human_behavior(page)
        simulate_typing(page, '[name="last_name"]', last_name)
        print(f"[注册] 姓: {last_name}")
        time.sleep(random.uniform(1.5, 3.0))

        simulate_human_behavior(page)
        simulate_typing(page, '[name="email"]', email)
        print(f"[注册] 邮箱: {email}")
        time.sleep(random.uniform(2.0, 4.0))

        print("[注册] 等待 Turnstile 初始化...")
        if _wait_for_turnstile_init(page, timeout=25):
            print("[注册] Turnstile 已加载，等待 invisible challenge...")
            simulate_human_behavior(page)
            if _wait_for_turnstile_token(page, timeout=15):
                print("[注册] Turnstile token 已就绪")
            else:
                print("[注册] Turnstile token 未就绪，仍尝试提交")
            time.sleep(random.uniform(1.0, 2.0))
        else:
            print("[注册] Turnstile 未加载，直接提交")
            time.sleep(random.uniform(0.8, 1.5))

        simulate_human_behavior(page)
        page.click('[type="submit"]')
        print("[注册] 提交个人信息...")

    navigated = _wait_for_page_change(page, old_url, timeout=30)

    if not navigated:
        try:
            body = page.locator("body").inner_text(timeout=2000)
            if "确认您是真人" in body or "verify" in body.lower():
                print("[注册] 人机验证未通过，尝试处理...")
                simulate_human_behavior(page)
                time.sleep(random.uniform(3, 6))
                _try_click_turnstile_checkbox(page)
                time.sleep(random.uniform(1, 2))
                try:
                    page.click('[type="submit"]')
                except Exception:
                    pass
                navigated = _wait_for_page_change(page, old_url, timeout=25)
        except Exception:
            pass

    if navigated:
        print(f"[注册] 已跳转到: {page.url[:80]}")
    else:
        print(f"[注册] 仍停留在第一页: {page.url[:80]}")

    if use_email_code and not _is_on_password_page(page):
        if is_blocked(page):
            raise RuntimeError("注册第一页已被阻止")
        raise RuntimeError("注册第一页提交后未进入密码页")

    if use_email_code:
        _handle_password_page_turnstile(page)

    print(f"[注册] URL: {page.url[:80]}")


def _wait_for_page_change(page: Page, old_url: str, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    checks = 0
    while time.time() < deadline:
        cur = page.url or ""
        if cur != old_url and "sign-up" not in cur.split("?")[0].rsplit("/", 1)[-1]:
            return True
        if "password" in cur or "magic-code" in cur:
            return True
        if is_blocked(page):
            return True
        if checks > 0 and checks % 6 == 0:
            simulate_human_behavior(page)
        checks += 1
        time.sleep(0.8)
    return False


def _is_on_password_page(page: Page) -> bool:
    url = page.url or ""
    if "sign-up/password" in url:
        return True
    try:
        if page.locator('[name="password"]').count() > 0:
            return True
    except Exception:
        pass
    return False


def _handle_password_page_turnstile(page: Page) -> None:
    """在密码页处理 Turnstile，然后点击「使用邮箱验证码继续」。"""
    if "magic-code" in (page.url or ""):
        print("[注册] 已在验证码页，跳过 Turnstile 处理")
        return

    if not _is_on_password_page(page):
        raise RuntimeError(f"当前不在密码页，URL={page.url}")

    print("[注册] 密码页 — 模拟浏览行为中...")
    simulate_human_behavior(page)
    time.sleep(random.uniform(2, 4))

    print("[注册] 密码页等待 Turnstile...")
    _wait_for_turnstile_init(page, timeout=20)
    simulate_human_behavior(page)
    time.sleep(random.uniform(1, 2))
    _wait_for_turnstile_token(page, timeout=15)
    simulate_human_behavior(page)
    time.sleep(random.uniform(1, 2))

    _click_email_code_button(page)


def _click_email_code_button(page: Page, max_attempts: int = 3) -> bool:
    candidates = [
        "使用邮箱验证码继续",
        "Continue with email code",
        "Use email code",
        "使用验证码继续",
        "Use verification code",
    ]

    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            print(f"[注册] 第 {attempt} 次重试点击邮箱验证码按钮...")
            simulate_human_behavior(page)
            time.sleep(random.uniform(3, 5))
            try:
                page.evaluate("try { turnstile.reset() } catch(e) { }")
            except Exception:
                pass
            time.sleep(random.uniform(2, 4))
            _wait_for_turnstile_token(page, timeout=10)

        simulate_human_behavior(page)
        time.sleep(random.uniform(0.3, 0.8))

        for text in candidates:
            try:
                btn = page.locator(f"text={text}").first
                if btn.is_visible(timeout=4000):
                    tag = btn.evaluate("el => el.tagName.toLowerCase()")
                    if tag in ("script", "style", "noscript"):
                        continue
                    print(f"[注册] 找到「{text}」(tag={tag})，点击...")
                    simulate_human_behavior(page)
                    time.sleep(random.uniform(0.5, 1.0))
                    btn.click()

                    for _ in range(8):
                        if is_blocked(page) or "magic-code" in (page.url or ""):
                            break
                        time.sleep(1)

                    if is_blocked(page):
                        print(f"[注册] 点击「{text}」后被阻止 (attempt {attempt}/{max_attempts})")
                        break
                    if "magic-code" in (page.url or ""):
                        print(f"[注册] 点击「{text}」成功，已跳转验证码页")
                        return True
                    print(f"[注册] 点击「{text}」成功")
                    return True
            except Exception:
                continue
        else:
            print("[注册] 未找到邮箱验证码按钮")
            return False

    return False


def fill_password_and_submit(page: Page, password: str) -> None:
    """注册第二步：填密码，提交。"""
    page.wait_for_selector('[name="password"]', timeout=15000)
    _random_wait(0.5, 1.5)
    simulate_typing(page, '[name="password"]', password)
    print("[注册] 密码已填写")
    time.sleep(random.uniform(1, 3))
    page.click('[type="submit"]')
    print("[注册] 密码已提交")
    time.sleep(3)
    handle_turnstile(page)


# ═══════════════════════════════════════════════════════════════════════
# 登录
# ═══════════════════════════════════════════════════════════════════════

def navigate_to_login(page: Page) -> None:
    print("[登录] 打开登录页...")
    page.goto(LOGIN_URL, timeout=30000, wait_until="domcontentloaded")
    time.sleep(random.uniform(3, 5))

    try:
        page.wait_for_selector('[name="email"]', timeout=5000)
    except Exception:
        handle_turnstile(page)

    try:
        page.wait_for_selector('[name="email"]', timeout=15000)
    except Exception:
        raise RuntimeError(f"登录页表单未加载。URL={page.url[:100]}")

    print(f"[登录] 当前页面: {page.url[:80]}")


def fill_login_email(page: Page, email: str) -> None:
    simulate_typing(page, '[name="email"]', email)
    time.sleep(random.uniform(1.5, 3.0))
    page.click('[type="submit"]')
    print("[登录] 邮箱已提交，等待 Turnstile...")
    time.sleep(random.uniform(3, 5))
    simulate_human_behavior(page)
    _wait_for_turnstile_init(page, timeout=20)
    _wait_for_turnstile_token(page, timeout=15)
    time.sleep(random.uniform(2, 4))


def login_with_email_code(page: Page, email: str) -> None:
    """登录：填邮箱 → 点「邮箱登录验证码」→ 等待验证码页面。"""
    fill_login_email(page, email)

    candidates = [
        "邮箱登录验证码", "使用邮箱验证码登录",
        "Continue with email code", "Use email code",
    ]
    clicked = False
    simulate_human_behavior(page)
    time.sleep(random.uniform(0.5, 1.0))
    for text in candidates:
        try:
            btn = page.locator(f"text={text}").first
            if btn.is_visible(timeout=4000):
                tag = btn.evaluate("el => el.tagName.toLowerCase()")
                if tag in ("script", "style", "noscript"):
                    continue
                simulate_human_behavior(page)
                time.sleep(random.uniform(0.5, 1.0))
                btn.click()
                print(f"[登录] 点击「{text}」")
                clicked = True
                for _ in range(8):
                    if is_blocked(page) or "magic-code" in (page.url or ""):
                        break
                    time.sleep(1)
                break
        except Exception:
            continue

    if not clicked:
        print("[登录] 未找到邮箱验证码按钮")


# ═══════════════════════════════════════════════════════════════════════
# 验证码 & 状态检测
# ═══════════════════════════════════════════════════════════════════════

def fill_verification_code(page: Page, code: str) -> None:
    """填入邮箱验证码 — Clerk 6 位独立输入框。"""
    time.sleep(random.uniform(1.5, 3.0))
    try:
        for i, digit in enumerate(code):
            inp = page.locator(f'[data-index="{i}"]')
            inp.click()
            time.sleep(random.uniform(0.05, 0.15))
            inp.fill(digit)
            time.sleep(random.uniform(0.15, 0.4))
        print(f"[验证码] 已填入 {code}")
        time.sleep(random.uniform(2, 4))
    except Exception as e:
        print(f"[验证码] 填入失败: {e}")


def is_logged_in(page: Page) -> bool:
    from desktop_signin import is_unfinished_handoff_url

    url = page.url or ""
    if is_unfinished_handoff_url(url):
        return False
    if any(kw in url for kw in ["dashboard", "settings", "/~", "billing", "/agents"]):
        return True
    try:
        body = page.locator("body").inner_text(timeout=2000)
        for kw in ["Account Settings", "Billing", "Overview", "Usage", "Settings"]:
            if kw in body:
                return True
    except Exception:
        pass
    return False


def is_blocked(page: Page) -> bool:
    try:
        body = page.locator("body").inner_text(timeout=2000)
        for kw in ["访问被阻止", "Access denied", "Access blocked", "请联系支持"]:
            if kw in body:
                return True
    except Exception:
        pass
    return False


def is_on_verification_page(page: Page) -> bool:
    try:
        return page.locator('[data-index="0"]').is_visible(timeout=3000)
    except Exception:
        return False


def _page_body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=1500) or ""
    except Exception:
        return ""


def _click_first_text(page: Page, texts: tuple[str, ...]) -> bool:
    for text in texts:
        for sel in (f'button:has-text("{text}")', f"text={text}"):
            try:
                loc = page.locator(sel)
                if loc.count() <= 0:
                    continue
                loc.first.click(timeout=5000)
                return True
            except Exception:
                continue
    return False


def complete_desktop_signin_steps(page: Page, timeout: int = 60) -> bool:
    """验证码后点完 Continue to sign in → Return to Cursor。"""
    from desktop_signin import (
        CONTINUE_BUTTON_TEXTS,
        RETURN_BUTTON_TEXTS,
        complete_desktop_signin_steps as _run,
    )

    return _run(
        get_url=lambda: page.url or "",
        get_body=lambda: _page_body_text(page),
        click_continue=lambda: _click_first_text(page, CONTINUE_BUTTON_TEXTS),
        click_return=lambda: _click_first_text(page, RETURN_BUTTON_TEXTS),
        pause=lambda: time.sleep(1),
        timeout=timeout,
    )


def wait_for_login_complete(page: Page, timeout: int = 60) -> bool:
    """验证码后先完成桌面确认，再认主站已登录。"""
    if complete_desktop_signin_steps(page, timeout=timeout):
        print("[登录] 登录成功（桌面确认完成）")
        return True
    if is_logged_in(page):
        print(f"[登录] 登录成功，URL: {(page.url or '')[:80]}")
        return True
    return False
