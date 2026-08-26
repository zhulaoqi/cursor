"""main_playwright.py - Playwright 引擎入口（方案A）

Playwright + Stealth + 住宅代理方案。独立于 DrissionPage 版本，可单独测试。
复用 config.py / email_client.py / cursor_auth.py / output.py。

用法：
  # 完整流程（注册 + 登录 + 订阅）
  python -u main_playwright.py --email xxx@example.com --imap-password xxx --proxy http://user:pass@host:port

  # 仅注册
  python -u main_playwright.py --email xxx@example.com --imap-password xxx --register-only

  # 仅登录 + 订阅
  python -u main_playwright.py --email xxx@example.com --imap-password xxx --skip-register

  # 无头模式
  python -u main_playwright.py --email xxx@example.com --imap-password xxx --headless

  # 使用密码流程注册
  python -u main_playwright.py --email xxx@example.com --imap-password xxx --use-password
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback

from config import ConfigError, load_config
from browser_playwright import (
    create_playwright_browser,
    warmup_browser,
    simulate_human_behavior,
    close_playwright,
)
from playwright_registration import (
    generate_password,
    navigate_to_signup,
    fill_email_and_continue,
    fill_password_and_submit,
    handle_turnstile,
    navigate_to_login,
    login_with_email_code,
    fill_verification_code,
    complete_desktop_signin_steps,
    is_logged_in,
    is_blocked,
    is_on_verification_page,
    wait_for_login_complete,
)
from email_client import connect_imap, poll_for_verification_code
from cursor_auth import get_cursor_session_token, update_cursor_auth
from output import print_account, save_account, mask_card_number


MAX_BLOCK_RETRIES = 5
RETRY_DELAY_SEC = 10


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cursor 账号自动注册 + 订阅（Playwright 引擎）"
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--use-password", action="store_true",
                      help="注册走密码流程（不推荐）")

    parser.add_argument("--email", default="", help="注册/登录邮箱")
    parser.add_argument("--imap-password", default="", help="IMAP 授权密码")
    parser.add_argument("--first-name", default="", help="名（不传则随机）")
    parser.add_argument("--last-name", default="", help="姓（不传则随机）")

    parser.add_argument("--proxy", default="", help="代理地址（推荐住宅/移动代理）")
    parser.add_argument("--headless", action="store_true", help="无头模式")

    parser.add_argument("--register-only", action="store_true",
                        help="仅注册，不登录不订阅")
    parser.add_argument("--skip-register", action="store_true",
                        help="跳过注册，直接登录 + 订阅")
    parser.add_argument("--skip-subscribe", action="store_true",
                        help="跳过订阅步骤")

    return parser.parse_args()


# ═══════════════════════════════════════════════════════════════════════
# 阶段一：注册
# ═══════════════════════════════════════════════════════════════════════

def phase_register(page, config, email, password, use_password: bool = False):
    print("\n══════ 阶段一：注册（Playwright）══════")

    if not use_password:
        print("[策略] 邮箱验证码路径（跳过密码页 Turnstile）")
    else:
        print("[策略] 密码路径")

    for attempt in range(1, MAX_BLOCK_RETRIES + 1):
        if attempt > 1:
            delay = RETRY_DELAY_SEC * attempt
            print(f"\n[重试] 第 {attempt}/{MAX_BLOCK_RETRIES} 次")
            print(f"[重试] 等待 {delay} 秒后重试...")
            time.sleep(delay)
            try:
                warmup_browser(page)
            except Exception:
                pass

        try:
            print("[1.1] 打开注册页 ...")
            navigate_to_signup(page)
        except Exception as e:
            print(f"[1.1] 注册页打开失败: {e}")
            continue

        try:
            print("[1.2] 填写姓名 / 邮箱 ...")
            fill_email_and_continue(
                page, email, config.first_name, config.last_name,
                use_email_code=not use_password,
            )
        except Exception as e:
            print(f"[1.2] 邮箱填写失败: {e}")
            continue

        if is_blocked(page):
            print("[1.2] 邮箱提交后被阻止")
            continue

        if use_password:
            try:
                print("[1.3] 设置密码 ...")
                fill_password_and_submit(page, password)
            except Exception as e:
                print(f"[1.3] 密码提交失败: {e}")
                continue

            if is_blocked(page):
                print("[1.3] 密码提交后被阻止")
                continue

            print("[1.4] 等待邮箱验证码页面...")
        else:
            print("[1.3] 等待邮箱验证码页面（已跳过密码页）...")

        for _ in range(20):
            if is_on_verification_page(page):
                break
            if is_blocked(page):
                break
            time.sleep(1)

        if is_blocked(page):
            print("[注册] 等待过程中被阻止")
            continue

        if is_on_verification_page(page):
            step = "[1.4]" if use_password else "[1.3]"
            print(f"{step} 收到邮箱验证码页面，获取验证码 ...")
            code = _poll_email_code(config)
            print(f"{step} 验证码: {code}")
            fill_verification_code(page, code)
            handle_turnstile(page)
            complete_desktop_signin_steps(page, timeout=60)
        else:
            print("[警告] 未出现验证码页面")

        wait_time = 5
        for i in range(wait_time):
            print(f"[注册] 等待系统处理... {wait_time - i}s")
            time.sleep(1)

        print("══════ 注册完成 ══════\n")
        return

    raise RuntimeError(
        f"注册失败：连续 {MAX_BLOCK_RETRIES} 次失败。建议：\n"
        "  1. 配置 TWOCAPTCHA_API_KEY 或 CAPSOLVER_API_KEY\n"
        "  2. 使用 --proxy 住宅/移动代理\n"
        "  3. 等待 30+ 分钟后重试"
    )


# ═══════════════════════════════════════════════════════════════════════
# 阶段二：登录
# ═══════════════════════════════════════════════════════════════════════

def phase_login(page, config, email):
    print("\n══════ 阶段二：登录（Playwright）══════")

    if is_logged_in(page):
        print("[登录] 已登录，跳过")
        return

    for attempt in range(1, MAX_BLOCK_RETRIES + 1):
        if attempt > 1:
            delay = RETRY_DELAY_SEC * attempt
            print(f"\n[重试] 第 {attempt}/{MAX_BLOCK_RETRIES} 次，等待 {delay} 秒...")
            time.sleep(delay)

        try:
            print("[2.1] 打开登录页 ...")
            navigate_to_login(page)
        except Exception as e:
            print(f"[2.1] 登录页打开失败: {e}")
            continue

        try:
            print("[2.2] 填写邮箱，走验证码登录 ...")
            login_with_email_code(page, email)
        except Exception as e:
            print(f"[2.2] 邮箱填写失败: {e}")
            continue

        if is_blocked(page):
            print("[2.2] 登录被阻止")
            continue

        for _ in range(15):
            if is_on_verification_page(page):
                break
            if is_logged_in(page):
                break
            time.sleep(1)

        if is_logged_in(page):
            print("══════ 登录完成 ══════\n")
            return

        if is_on_verification_page(page):
            print("[2.3] 获取登录验证码 ...")
            code = _poll_email_code(config)
            print(f"[2.3] 验证码: {code}")
            fill_verification_code(page, code)

            if wait_for_login_complete(page, timeout=60):
                print("══════ 登录完成 ══════\n")
                return
            else:
                print("[2.3] 登录未完成，继续重试...")
        else:
            print(f"[警告] 未出现验证码页面，URL: {page.url[:80]}")

    raise RuntimeError(f"登录失败：连续 {MAX_BLOCK_RETRIES} 次失败")


# ═══════════════════════════════════════════════════════════════════════
# 阶段三：获取 Token
# ═══════════════════════════════════════════════════════════════════════

def phase_get_token(page, email):
    """获取 Token — 复用 cursor_auth.py 的 PKCE 逻辑。

    cursor_auth.py 的 get_cursor_session_token 接受 DrissionPage tab，
    但内部只用了 tab.get() / tab.ele() / tab.run_js() 三个方法。
    这里用 Playwright 的 page 对象做一个适配层。
    """
    print("\n══════ 阶段三：获取 Token（Playwright）══════")

    from cursor_auth import _generate_auth_params, _poll_for_login_result, update_cursor_auth

    params = _generate_auth_params()
    url = (
        f"https://www.cursor.com/cn/loginDeepControl"
        f"?challenge={params['n']}&uuid={params['r']}&mode=login"
    )

    print("[Token] 导航到 loginDeepControl ...")
    page.goto(url, timeout=30000, wait_until="domcontentloaded")

    for _ in range(3):
        try:
            page.wait_for_selector("text=You're currently logged in as:", timeout=5000)
            break
        except Exception:
            pass
        try:
            page.wait_for_selector("text=Continue to sign in", timeout=2000)
            break
        except Exception:
            pass
        try:
            page.wait_for_selector("text=Return to Cursor", timeout=2000)
            break
        except Exception:
            time.sleep(2)

    time.sleep(2)
    complete_desktop_signin_steps(page, timeout=15)

    for text in ("Yes, Log In", "Yes"):
        try:
            loc = page.locator(f'button:has-text("{text}")')
            if loc.count() > 0:
                loc.first.click(timeout=3000)
                print(f"[Token] 已点击 {text}")
                break
        except Exception:
            continue

    try:
        page.evaluate("""() => {
            try {
                const button = document.querySelectorAll('.min-h-screen')[1]
                    .querySelectorAll('.gap-4')[1]
                    .querySelectorAll('button')[1];
                if (button) { button.click(); return true; }
            } catch(e) {}
            return false;
        }""")
    except Exception as e:
        print(f"[Token] 点击确认按钮失败: {e}")

    print("[Token] 轮询 auth poll ...")
    _, access_token, refresh_token = _poll_for_login_result(params["r"], params["s"])

    if access_token and refresh_token:
        print("[Token] 获取成功")
        print("[Token] 写入 Cursor 本地数据库 ...")
        update_cursor_auth(email, access_token, refresh_token)
        print("══════ Token 获取完成 ══════\n")
        return access_token, refresh_token
    else:
        print("[Token] 获取失败")
        print("══════ Token 获取失败 ══════\n")
        return None, None


# ═══════════════════════════════════════════════════════════════════════

def _poll_email_code(config) -> str:
    conn = connect_imap(
        config.imap_host, config.imap_port,
        config.email, config.imap_password,
    )
    return poll_for_verification_code(conn)


def run() -> None:
    args = parse_args()
    proxy = args.proxy or None
    use_password = getattr(args, "use_password", False)

    if not args.email:
        print("错误：请指定邮箱：--email xxx --imap-password xxx", file=sys.stderr)
        sys.exit(1)

    try:
        config = load_config(
            email=args.email,
            imap_password=args.imap_password,
            first_name=args.first_name,
            last_name=args.last_name,
            headless=args.headless,
        )
    except ConfigError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    if proxy:
        config.proxy = proxy

    email = config.email
    password = generate_password()

    capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "").strip()
    twocaptcha_key = os.environ.get("TWOCAPTCHA_API_KEY", "").strip()

    print("=" * 55)
    print("  Cursor 自动化（Playwright + Stealth 引擎）")
    print("=" * 55)
    print(f"  邮箱: {email}")
    print(f"  姓名: {config.first_name} {config.last_name}")
    if use_password:
        print(f"  密码: {password}")
    print(f"  套餐: {config.plan_name}")
    print(f"  银行卡: {mask_card_number(config.card_number)}")
    if config.proxy:
        print(f"  代理: {config.proxy[:50]}...")
    solver_info = []
    if twocaptcha_key:
        solver_info.append("2Captcha ✓")
    if capsolver_key:
        solver_info.append("CapSolver ✓")
    print(f"  Turnstile: {' + '.join(solver_info) if solver_info else '仅浏览器自动验证'}")
    print(f"  注册路径: {'密码流程' if use_password else '邮箱验证码'}")
    steps = []
    if not args.skip_register:
        steps.append("注册")
    steps.append("登录")
    if not args.skip_subscribe and not args.register_only:
        steps.append("绑卡+订阅")
    print(f"  执行步骤: {' → '.join(steps)}")
    print(f"  浏览器: Playwright ({'headless' if args.headless else 'headed'})")
    print("=" * 55)

    pw, browser, context, page = create_playwright_browser(
        headless=args.headless,
        proxy=config.proxy if hasattr(config, "proxy") else None,
    )

    try:
        warmup_browser(page)

        if not args.skip_register:
            phase_register(page, config, email, password, use_password=use_password)

        if args.register_only:
            print_account(email, password)
            save_account(email, password)
            return

        phase_login(page, config, email)
        phase_get_token(page, email)

        # 订阅阶段暂时跳过（Playwright 版 Stripe 填卡待后续适配）
        if not args.skip_subscribe:
            print("\n[提示] Playwright 版 Stripe 订阅功能待适配，本次跳过")
            print("[提示] 可使用 DrissionPage 版 main.py --skip-register 完成订阅")

        print_account(email, password)
        save_account(email, password)

        print("\n[完成] 全部流程执行完毕（Playwright 引擎）")

    except Exception:
        traceback.print_exc()
        sys.exit(1)
    finally:
        close_playwright(pw, browser)


if __name__ == "__main__":
    run()
