"""main.py - CLI 入口与流程编排（DrissionPage 版）

完整流程：注册 → 登录 → 绑卡 → 订阅

用法：
  # 完整流程（注册 + 登录 + 订阅）
  python -u main.py --email xxx@example.com --imap-password xxx

  # 仅注册（不登录、不订阅）
  python -u main.py --email xxx@example.com --imap-password xxx --register-only

  # 仅登录 + 订阅（已有账号）
  python -u main.py --email xxx@example.com --imap-password xxx --skip-register

  # 使用密码流程注册（不推荐）
  python -u main.py --email xxx@example.com --imap-password xxx --use-password

  # 跳过订阅
  python -u main.py --email xxx@example.com --imap-password xxx --skip-subscribe
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback

from config import ConfigError, load_config
from browser import create_browser, warmup_browser, clear_browser_state, close_browser
from registration import (
    generate_password,
    navigate_to_signup,
    fill_email_and_continue,
    fill_password_and_submit,
    handle_turnstile,
    navigate_to_login,
    fill_login_email,
    fill_login_password,
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
from subscription import (
    navigate_to_billing,
    select_plan,
    fill_stripe_card,
    submit_payment,
    verify_subscription,
    _wait_for_stripe_checkout,
)
from output import print_account, save_account, mask_card_number

MAX_BLOCK_RETRIES = 5
RETRY_DELAY_SEC = 10


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cursor 账号自动注册 + 订阅（DrissionPage）")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--use-password", action="store_true",
                      help="注册走密码流程（不推荐）")

    parser.add_argument("--email", default="", help="注册/登录邮箱")
    parser.add_argument("--imap-password", default="", help="IMAP 授权密码")
    parser.add_argument("--first-name", default="", help="名（不传则随机）")
    parser.add_argument("--last-name", default="", help="姓（不传则随机）")

    parser.add_argument("--proxy", default="", help="代理地址")
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

def _dump_page_state(tab, label: str) -> None:
    """打印当前页面诊断信息。"""
    try:
        url = tab.url or "(unknown)"
        title = tab.title or "(unknown)"
        body_text = ""
        try:
            body_text = tab.ele("tag:body").text[:300]
        except Exception:
            body_text = "(无法读取)"
        print(f"  [{label}] URL: {url[:100]}")
        print(f"  [{label}] Title: {title[:80]}")
        print(f"  [{label}] Body: {body_text[:200]}")
    except Exception as e:
        print(f"  [{label}] 无法读取页面状态: {e}")


def _recover_tab(browser, tab):
    """尝试恢复断开的 tab 连接。"""
    try:
        tab.url
        return tab
    except Exception:
        pass
    try:
        new_tab = browser.latest_tab
        new_tab.url
        print("[恢复] 使用现有 tab")
        return new_tab
    except Exception:
        pass
    try:
        new_tab = browser.new_tab()
        print("[恢复] 创建新 tab")
        return new_tab
    except Exception as e:
        print(f"[恢复] 无法恢复 tab: {e}")
        return tab


def phase_register(browser, tab, config, email, password, use_password: bool = False):
    print("\n══════ 阶段一：注册 ══════")

    if not use_password:
        print("[策略] 邮箱验证码路径（跳过密码页 Turnstile）")
    else:
        print("[策略] 密码路径")

    for attempt in range(1, MAX_BLOCK_RETRIES + 1):
        if attempt > 1:
            delay = RETRY_DELAY_SEC * attempt
            print(f"\n[重试] 第 {attempt}/{MAX_BLOCK_RETRIES} 次")
            tab = _recover_tab(browser, tab)
            try:
                clear_browser_state(tab)
            except Exception:
                tab = _recover_tab(browser, tab)
            print(f"[重试] 等待 {delay} 秒后重试...")
            time.sleep(delay)
            try:
                warmup_browser(tab)
            except Exception:
                tab = _recover_tab(browser, tab)

        try:
            print("[1.1] 打开注册页 ...")
            navigate_to_signup(tab)
        except Exception as e:
            print(f"[1.1] 注册页打开失败: {e}")
            _dump_page_state(tab, "1.1")
            continue

        try:
            print("[1.2] 填写姓名 / 邮箱 ...")
            fill_email_and_continue(
                tab, email, config.first_name, config.last_name,
                use_email_code=not use_password,
            )
        except Exception as e:
            print(f"[1.2] 邮箱填写失败: {e}")
            _dump_page_state(tab, "1.2")
            continue

        if is_blocked(tab):
            print("[1.2] 邮箱提交后被阻止")
            _dump_page_state(tab, "blocked")
            continue

        if use_password:
            try:
                print("[1.3] 设置密码 ...")
                fill_password_and_submit(tab, password)
            except Exception as e:
                print(f"[1.3] 密码提交失败: {e}")
                continue

            if is_blocked(tab):
                print("[1.3] 密码提交后被阻止")
                _dump_page_state(tab, "blocked")
                continue

            print("[1.4] 等待邮箱验证码页面...")
        else:
            print("[1.3] 等待邮箱验证码页面（已跳过密码页）...")

        for _ in range(20):
            if is_on_verification_page(tab):
                break
            if is_blocked(tab):
                break
            time.sleep(1)

        if is_blocked(tab):
            print("[注册] 等待过程中被阻止")
            _dump_page_state(tab, "blocked")
            continue

        if is_on_verification_page(tab):
            step = "[1.4]" if use_password else "[1.3]"
            print(f"{step} 收到邮箱验证码页面，获取验证码 ...")
            code = _poll_email_code(config)
            print(f"{step} 验证码: {code}")
            fill_verification_code(tab, code)
            handle_turnstile(tab)
            complete_desktop_signin_steps(tab, timeout=60)
        else:
            print(f"[警告] 未出现验证码页面")
            _dump_page_state(tab, "状态")

        wait_time = 5
        for i in range(wait_time):
            print(f"[注册] 等待系统处理... {wait_time - i}s")
            time.sleep(1)

        print("══════ 注册完成 ══════\n")
        return tab

    raise RuntimeError(
        f"注册失败：连续 {MAX_BLOCK_RETRIES} 次失败。建议：\n"
        "  1. 在 .env 配置 CAPSOLVER_API_KEY 启用外部 Turnstile 求解\n"
        "  2. 使用 --proxy 切换住宅代理 IP\n"
        "  3. 等待 30+ 分钟后重试\n"
        "  4. 检查邮箱是否已被注册"
    )


# ═══════════════════════════════════════════════════════════════════════
# 阶段二：登录
# ═══════════════════════════════════════════════════════════════════════

def phase_login(tab, config, email):
    """登录阶段：走邮箱验证码路径。"""
    print("\n══════ 阶段二：登录 ══════")

    if is_logged_in(tab):
        print("[登录] 已登录，跳过")
        return

    for attempt in range(1, MAX_BLOCK_RETRIES + 1):
        if attempt > 1:
            delay = RETRY_DELAY_SEC * attempt
            print(f"\n[重试] 第 {attempt}/{MAX_BLOCK_RETRIES} 次，等待 {delay} 秒...")
            time.sleep(delay)

        try:
            print("[2.1] 打开登录页 ...")
            navigate_to_login(tab)
        except Exception as e:
            print(f"[2.1] 登录页打开失败: {e}")
            continue

        try:
            print("[2.2] 填写邮箱，走验证码登录 ...")
            login_with_email_code(tab, email)
        except Exception as e:
            print(f"[2.2] 邮箱填写失败: {e}")
            continue

        if is_blocked(tab):
            print("[2.2] 登录被阻止")
            continue

        # 等验证码页面
        for _ in range(15):
            if is_on_verification_page(tab):
                break
            if is_logged_in(tab):
                break
            time.sleep(1)

        if is_logged_in(tab):
            print("══════ 登录完成 ══════\n")
            return

        if is_on_verification_page(tab):
            print("[2.3] 获取登录验证码 ...")
            code = _poll_email_code(config)
            print(f"[2.3] 验证码: {code}")
            fill_verification_code(tab, code)

            if wait_for_login_complete(tab, timeout=60):
                print("══════ 登录完成 ══════\n")
                return
            else:
                print("[2.3] 登录未完成，继续重试...")
        else:
            print(f"[警告] 未出现验证码页面，URL: {tab.url[:80]}")

    raise RuntimeError(f"登录失败：连续 {MAX_BLOCK_RETRIES} 次失败")


# ═══════════════════════════════════════════════════════════════════════
# 阶段三：获取 Token & 写入 Cursor
# ═══════════════════════════════════════════════════════════════════════

def phase_get_token(tab, email):
    print("\n══════ 阶段三：获取 Token ══════")

    print("[3.1] 获取 session token ...")
    access_token, refresh_token = get_cursor_session_token(tab)

    if access_token and refresh_token:
        print("[3.2] 写入 Cursor 本地数据库 ...")
        update_cursor_auth(email, access_token, refresh_token)
        print("══════ Token 获取完成 ══════\n")
        return access_token, refresh_token
    else:
        print("[3.1] Token 获取失败，账号密码仍然可用")
        print("══════ Token 获取失败 ══════\n")
        return None, None


# ═══════════════════════════════════════════════════════════════════════
# 阶段四：绑卡 & 订阅
# ═══════════════════════════════════════════════════════════════════════

def phase_subscribe(tab, config):
    """订阅阶段：选择套餐 → 填卡 → 支付。"""
    print("\n══════ 阶段四：绑卡 & 订阅 ══════")

    print(f"[4.1] 打开 billing 页面 ...")
    navigate_to_billing(tab)

    print(f"[4.2] 选择套餐: {config.plan_name} ...")
    select_plan(tab, config.plan_name)

    print("[4.3] 等待 Stripe Checkout ...")
    if not _wait_for_stripe_checkout(tab, timeout=30):
        print("[4.3] Stripe 页面未加载，尝试继续...")

    print(f"[4.4] 填写信用卡: ****{config.card_number[-4:]} ...")
    fill_stripe_card(
        tab,
        card_number=config.card_number,
        card_exp_month=config.card_exp_month,
        card_exp_year=config.card_exp_year,
        card_cvv=config.card_cvv,
        card_holder=config.card_holder,
        card_zip=config.card_zip,
    )

    print("[4.5] 提交支付 ...")
    submit_payment(tab)

    print("[4.6] 验证订阅状态 ...")
    if verify_subscription(tab):
        print("══════ 订阅完成 ══════\n")
    else:
        print("══════ 订阅状态不确定，请手动确认 ══════\n")


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

    # 打印配置摘要
    print("=" * 50)
    print("  Cursor 自动化流程")
    print("=" * 50)
    print(f"  邮箱: {email}")
    print(f"  姓名: {config.first_name} {config.last_name}")
    if use_password:
        print(f"  密码: {password}")
    print(f"  套餐: {config.plan_name}")
    print(f"  银行卡: {mask_card_number(config.card_number)}")
    if config.proxy:
        print(f"  代理: {config.proxy[:50]}...")
    capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "").strip()
    twocaptcha_key = os.environ.get("TWOCAPTCHA_API_KEY", "").strip()
    solver_parts = []
    if capsolver_key:
        solver_parts.append("CapSolver ✓")
    if twocaptcha_key:
        solver_parts.append("2Captcha ✓")
    print(f"  Turnstile: {' + '.join(solver_parts) if solver_parts else '仅浏览器内验证（建议配置 CAPSOLVER_API_KEY 或 TWOCAPTCHA_API_KEY）'}")
    print(f"  注册路径: {'密码流程' if use_password else '邮箱验证码'}")
    steps = []
    if not args.skip_register:
        steps.append("注册")
    steps.append("登录")
    if not args.skip_subscribe and not args.register_only:
        steps.append("绑卡+订阅")
    print(f"  执行步骤: {' → '.join(steps)}")
    print(f"  浏览器: {'headless' if args.headless else 'headed (DrissionPage)'}")
    print("=" * 50)

    browser, tab = create_browser(
        headless=args.headless,
        proxy=config.proxy if hasattr(config, "proxy") else None,
    )

    try:
        tab.run_js("try { turnstile.reset() } catch(e) { }")
        warmup_browser(tab)

        # 阶段一：注册
        if not args.skip_register:
            tab = phase_register(browser, tab, config, email, password, use_password=use_password) or tab

        if args.register_only:
            print_account(email, password)
            save_account(email, password)
            return

        # 阶段二：登录
        phase_login(tab, config, email)

        # 阶段三：获取 Token
        phase_get_token(tab, email)

        # 阶段四：绑卡 & 订阅
        if not args.skip_subscribe:
            phase_subscribe(tab, config)

        # 输出结果
        print_account(email, password)
        save_account(email, password)

        print("\n[完成] 全部流程执行完毕")

    except Exception:
        traceback.print_exc()
        sys.exit(1)
    finally:
        close_browser(browser)


if __name__ == "__main__":
    run()
