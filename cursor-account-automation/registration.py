# registration.py - 注册 / 登录 / Turnstile 处理（DrissionPage 版）

import os
import random
import string
import time
import logging


class TurnstileError(Exception):
    pass


def _has_capsolver() -> bool:
    return bool(os.environ.get("CAPSOLVER_API_KEY", "").strip())


def _has_twocaptcha() -> bool:
    return bool(os.environ.get("TWOCAPTCHA_API_KEY", "").strip())


def generate_password(length: int = 12) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(random.choices(chars, k=length))


def _random_wait(low: float = 0.3, high: float = 1.0) -> None:
    time.sleep(random.uniform(low, high))


def _is_fast_mode() -> bool:
    """启用 CapSolver 时走更激进的快速路径。"""
    return _has_capsolver()


def _sleep_by_mode(normal: float, fast: float) -> None:
    time.sleep(fast if _is_fast_mode() else normal)


# ═══════════════════════════════════════════════════════════════════════
# Turnstile / Cloudflare 处理
# ═══════════════════════════════════════════════════════════════════════

def _get_turnstile_response(tab) -> str:
    """通过多种方式检查 Turnstile 是否已返回有效 token。

    返回 token 字符串（可能是通过 token 也可能是失败 token）。
    空字符串表示 Turnstile 尚未完成。
    """
    try:
        return tab.run_js("""
            // 方法 1: Turnstile JS API
            try {
                const widgets = window.turnstile && turnstile.getResponse
                    ? [turnstile.getResponse()] : [];
                for (const t of widgets) {
                    if (t && t.length > 20) return t;
                }
            } catch(e) {}
            // 方法 2: hidden input
            const sels = [
                'input[name="cf-turnstile-response"]',
                '[name="cf-chl-turnstile-response"]',
                '[data-testid="cf-turnstile-response"]',
            ];
            for (const sel of sels) {
                const el = document.querySelector(sel);
                if (el && el.value && el.value.length > 20) return el.value;
            }
            // 方法 3: widget 内部 hidden input
            const ws = document.querySelectorAll('[id^="cf-chl-widget"],.cf-turnstile,[id=cf-turnstile]');
            for (const w of ws) {
                const inp = w.querySelector('input[type="hidden"]');
                if (inp && inp.value && inp.value.length > 20) return inp.value;
            }
            return '';
        """) or ""
    except Exception:
        return ""


def _dump_turnstile_diag(tab) -> None:
    """诊断页面上 Turnstile/captcha 相关元素，帮助定位 invisible captcha 问题。"""
    try:
        diag = tab.run_js("""
            const result = {};
            result.hasTurnstile = !!window.turnstile;
            result.turnstileMethods = window.turnstile
                ? Object.keys(window.turnstile).filter(k => typeof window.turnstile[k] === 'function')
                : [];
            try {
                const resp = window.turnstile && turnstile.getResponse && turnstile.getResponse();
                result.hasToken = !!(resp && resp.length > 20);
                result.tokenLen = resp ? resp.length : 0;
            } catch(e) { result.hasToken = false; result.tokenLen = 0; }
            const iframes = Array.from(document.querySelectorAll('iframe')).map(f => ({
                src: (f.src || '').substring(0, 120),
                id: f.id || '',
                w: f.offsetWidth, h: f.offsetHeight,
            }));
            result.iframes = iframes;
            const captchaEls = Array.from(document.querySelectorAll(
                '[data-sitekey], .cf-turnstile, #cf-turnstile, [id^="cf-chl-widget"], ' +
                '[id*="captcha"], [class*="captcha"], [id*="turnstile"], [class*="turnstile"]'
            )).map(e => ({
                tag: e.tagName, id: e.id || '', cls: e.className || '',
                sitekey: e.getAttribute('data-sitekey') || '',
            }));
            result.captchaElements = captchaEls;
            return result;
        """) or {}
        print(f"[诊断] turnstile={diag.get('hasTurnstile')}, methods={diag.get('turnstileMethods')}, hasToken={diag.get('hasToken')}, tokenLen={diag.get('tokenLen')}")
        for f in (diag.get('iframes') or [])[:3]:
            print(f"[诊断] iframe: src={f.get('src','')[:80]}, {f.get('w')}x{f.get('h')}")
        for el in (diag.get('captchaElements') or [])[:5]:
            print(f"[诊断] {el.get('tag')} id={el.get('id')} sitekey={el.get('sitekey')[:30]}")
    except Exception as e:
        print(f"[诊断] 失败: {e}")


def _wait_for_turnstile_init(tab, timeout: int = 30) -> bool:
    """等待 Turnstile 脚本加载并初始化（window.turnstile 可用）。

    Clerk 需要 Turnstile 先完成 invisible challenge 才能提交表单。
    如果提交时 Turnstile 还没加载完，Clerk 会显示"确认您是真人"。
    """
    script_logged = False
    for _ in range(timeout):
        try:
            state = tab.run_js("""
                if (window.turnstile && typeof window.turnstile.render === 'function')
                    return 'ready';
                const script = document.getElementById('cf-turnstile-script');
                if (script) return 'script:' + (script.src || '(inline)').substring(0, 100);
                return 'waiting';
            """) or "waiting"
            if state == "ready":
                return True
            if state.startswith("script:") and not script_logged:
                print(f"[Turnstile] 脚本加载中: {state[7:]}")
                script_logged = True
        except Exception:
            pass
        time.sleep(1)
    return False


def _wait_for_turnstile_token(tab, timeout: int = 15) -> bool:
    """等待 Turnstile invisible challenge 完成并产生 token。"""
    for _ in range(timeout):
        try:
            has_token = tab.run_js("""
                try {
                    const resp = window.turnstile && turnstile.getResponse && turnstile.getResponse();
                    if (resp && resp.length > 20) return true;
                } catch(e) {}
                const inputs = document.querySelectorAll(
                    'input[name="cf-turnstile-response"], [name="cf-chl-turnstile-response"]');
                for (const inp of inputs) {
                    if (inp.value && inp.value.length > 20) return true;
                }
                return false;
            """)
            if has_token:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _simulate_human_move(tab) -> None:
    """在页面上模拟自然鼠标移动 + 滚动，给 Turnstile 提供行为信号。

    Turnstile ML 模型会分析鼠标加速度、曲线、停顿模式、滚动行为。
    """
    try:
        tab.actions.move(random.randint(100, 400), random.randint(100, 300))
        time.sleep(random.uniform(0.1, 0.3))
        tab.actions.move(random.randint(-80, 80), random.randint(-60, 60), duration=0.3)
        time.sleep(random.uniform(0.2, 0.5))
        tab.actions.move(random.randint(-50, 50), random.randint(-40, 40), duration=0.2)
    except Exception:
        pass


def _simulate_rich_behavior(tab) -> None:
    """进入重要页面时的丰富行为模拟。

    比 _simulate_human_move 更全面：多段鼠标移动 + 滚动 + 停顿，
    尽可能拉高 Turnstile 的行为评分以抵消 CDP 检测带来的扣分。
    """
    try:
        x, y = random.randint(200, 600), random.randint(150, 350)
        tab.actions.move(x, y)
        time.sleep(random.uniform(0.3, 0.6))

        for _ in range(random.randint(2, 4)):
            dx = random.randint(-120, 120)
            dy = random.randint(-80, 80)
            tab.actions.move(dx, dy, duration=random.uniform(0.2, 0.5))
            time.sleep(random.uniform(0.15, 0.4))

        tab.scroll.down(random.randint(30, 100))
        time.sleep(random.uniform(0.3, 0.8))
        tab.scroll.up(random.randint(10, 50))
        time.sleep(random.uniform(0.2, 0.5))

        tab.actions.move(random.randint(-60, 60), random.randint(-40, 40), duration=0.3)
    except Exception:
        pass


def _try_click_turnstile_checkbox(tab) -> bool:
    """尝试通过 Shadow DOM 找到并点击 Turnstile 可见复选框。"""
    methods = [
        lambda: (
            tab.ele(".main-content")
            .ele("tag:div").ele("tag:div").ele("tag:div")
            .shadow_root.ele("tag:iframe")
            .ele("tag:body").sr("tag:input")
        ),
        lambda: (
            tab.ele("@id=cf-turnstile", timeout=2)
            .child()
            .shadow_root.ele("tag:iframe")
            .ele("tag:body").sr("tag:input")
        ),
        lambda: (
            tab.ele(".cf-turnstile", timeout=2)
            .child()
            .shadow_root.ele("tag:iframe")
            .ele("tag:body").sr("tag:input")
        ),
    ]

    for method in methods:
        try:
            checkbox = method()
            if checkbox:
                _simulate_human_move(tab)
                time.sleep(random.uniform(0.5, 1.5))
                checkbox.click()
                print("[Turnstile] 点击了验证复选框")
                time.sleep(3)
                return True
        except Exception:
            continue
    return False


def _has_turnstile_widget(tab) -> bool:
    """检查当前页面是否已渲染 Turnstile widget。"""
    try:
        return bool(tab.run_js("""
            const sels = [
                '[data-sitekey]',
                '.cf-turnstile',
                '#cf-turnstile',
                '[id^="cf-chl-widget"]',
                'input[name="cf-turnstile-response"]',
                '[name="cf-chl-turnstile-response"]',
            ];
            return sels.some(sel => document.querySelector(sel));
        """))
    except Exception:
        return False


def _solve_turnstile_direct(tab, timeout: int = 60, reason: str = "") -> bool:
    """优先使用外部求解器获取有效 token（CapSolver → 2Captcha 自动降级）。"""
    if not _has_capsolver() and not _has_twocaptcha():
        return False

    prefix = f"[Turnstile] {reason} " if reason else "[Turnstile] "
    print(f"{prefix}使用 CapSolver 主动求解...")

    ready = False
    params_ready = False
    max_checks = 20 if not _is_fast_mode() else 24
    for _ in range(max_checks):
        if "magic-code" in (tab.url or ""):
            return True

        if _has_turnstile_widget(tab):
            ready = True

        if "magic-code" in (tab.url or ""):
            return True

        try:
            from turnstile_solver import get_turnstile_params
            params = get_turnstile_params(tab) or {}
            if (params.get("sitekey") or "").strip():
                params_ready = True
                break
        except Exception:
            pass

        time.sleep(random.uniform(0.8, 1.5))

    if not ready and not params_ready:
        print(f"{prefix}未检测到 Turnstile widget 或真实参数，跳过主动求解")
        return False

    # 尝试 CapSolver
    if _has_capsolver():
        try:
            from turnstile_solver import solve_and_inject

            if solve_and_inject(tab, timeout=timeout):
                time.sleep(random.uniform(2, 3))
                token = _get_turnstile_response(tab)
                if token:
                    print(f"{prefix}CapSolver token 已就绪, len={len(token)}")
                else:
                    print(f"{prefix}CapSolver 已注入 token")
                return True
        except Exception as e:
            print(f"{prefix}CapSolver 失败: {e}")

    # CapSolver 失败或未配置，降级到 2Captcha
    if _has_twocaptcha():
        try:
            from twocaptcha_solver import solve_and_inject_turnstile
            print(f"{prefix}降级到 2Captcha 求解...")
            if solve_and_inject_turnstile(tab, timeout=timeout):
                time.sleep(random.uniform(2, 3))
                token = _get_turnstile_response(tab)
                if token:
                    print(f"{prefix}2Captcha token 已就绪, len={len(token)}")
                else:
                    print(f"{prefix}2Captcha 已注入 token")
                return True
        except Exception as e:
            print(f"{prefix}2Captcha 失败: {e}")

    return False


def handle_turnstile(tab, max_retries: int = 3) -> bool:
    """处理 Cloudflare Turnstile 验证（通用版）。"""
    print("[Turnstile] 检测验证状态...")

    if _solve_turnstile_direct(tab, reason="通用验证页"):
        return True

    for retry in range(1, max_retries + 1):
        try:
            tab.run_js("try { turnstile.reset() } catch(e) { }")
        except Exception:
            pass

        _simulate_human_move(tab)
        time.sleep(random.uniform(1, 3))

        if _try_click_turnstile_checkbox(tab):
            if _check_page_advanced(tab):
                print("[Turnstile] 验证通过")
                return True

        if _check_page_advanced(tab):
            print("[Turnstile] 已自动通过")
            return True

        time.sleep(random.uniform(1, 2))

    print(f"[Turnstile] {max_retries} 次重试后未通过")
    return False


def _check_page_advanced(tab) -> bool:
    """检查页面是否已推进到下一步。"""
    for selector in ["@name=first_name", "@name=password", "@data-index=0", "Account Settings"]:
        try:
            if tab.ele(selector, timeout=1):
                return True
        except Exception:
            continue
    return False


# ═══════════════════════════════════════════════════════════════════════
# 注册
# ═══════════════════════════════════════════════════════════════════════

SIGN_UP_URL = "https://authenticator.cursor.sh/sign-up"
LOGIN_URL = "https://authenticator.cursor.sh/sign-in"
SETTINGS_URL = "https://www.cursor.com/settings"


def navigate_to_signup(tab) -> None:
    print("[注册] 打开注册页...")
    tab.get(SIGN_UP_URL)
    time.sleep(random.uniform(3, 5))

    if not tab.ele("@name=first_name", timeout=5):
        handle_turnstile(tab)

    if not tab.ele("@name=first_name", timeout=15):
        title = tab.title or ""
        url = tab.url or ""
        body = ""
        try:
            body = tab.ele("tag:body").text[:200]
        except Exception:
            pass
        raise RuntimeError(
            f"注册页表单未加载。\n  title: {title}\n  URL: {url[:100]}\n  内容: {body}\n"
            "建议：换代理 IP 或等待 30 分钟后重试"
        )

    print(f"[注册] 当前页面: {tab.url[:80]}")


def fill_email_and_continue(
    tab, email: str,
    first_name: str = "", last_name: str = "",
    use_email_code: bool = True,
) -> None:
    """注册第一步：填姓名、邮箱，点继续。

    第一页有 Clerk invisible captcha，依赖行为信号判断是否为真人。
    必须全程模拟人类操作速度（不使用 fast mode），CapSolver 在此页
    无法工作（没有可提取的 sitekey）。
    """
    # 停留在页面上"阅读"一会儿
    time.sleep(random.uniform(1.5, 3.0))

    old_url = tab.url or ""

    if tab.ele("@name=first_name"):
        _simulate_human_move(tab)
        time.sleep(random.uniform(0.5, 1.0))

        tab.actions.click("@name=first_name").input(first_name)
        print(f"[注册] 名: {first_name}")
        time.sleep(random.uniform(1.5, 3.0))

        _simulate_human_move(tab)
        tab.actions.click("@name=last_name").input(last_name)
        print(f"[注册] 姓: {last_name}")
        time.sleep(random.uniform(1.5, 3.0))

        _simulate_human_move(tab)
        tab.actions.click("@name=email").input(email)
        print(f"[注册] 邮箱: {email}")
        time.sleep(random.uniform(2.0, 4.0))

        # 等待 Turnstile invisible challenge 完成后再提交
        print("[注册] 等待 Turnstile 初始化...")
        if _wait_for_turnstile_init(tab, timeout=25):
            print("[注册] Turnstile 已加载，等待 invisible challenge...")
            _simulate_human_move(tab)
            if _wait_for_turnstile_token(tab, timeout=15):
                print("[注册] Turnstile token 已就绪")
            else:
                print("[注册] Turnstile token 未就绪，仍尝试提交")
            time.sleep(random.uniform(1.0, 2.0))
        else:
            print("[注册] Turnstile 未加载（可能脚本 CDN 受阻），直接提交")
            time.sleep(random.uniform(0.8, 1.5))

        _simulate_human_move(tab)
        tab.actions.click("@type=submit")
        print("[注册] 提交个人信息...")

    # ── 等待 invisible captcha 处理并跳转（真实秒数） ──
    navigated = _wait_for_page_change(tab, old_url, timeout=30)

    if not navigated:
        try:
            body = tab.ele("tag:body").text or ""
            if "确认您是真人" in body or "verify" in body.lower():
                print("[注册] 人机验证未通过，诊断页面状态...")
                _dump_turnstile_diag(tab)
                _simulate_human_move(tab)
                time.sleep(random.uniform(3, 6))
                _try_click_turnstile_checkbox(tab)
                time.sleep(random.uniform(1, 2))
                try:
                    tab.actions.click("@type=submit")
                    print("[注册] 已重新点击继续按钮")
                except Exception:
                    pass
                navigated = _wait_for_page_change(tab, old_url, timeout=25)
        except Exception:
            pass

    if navigated:
        print(f"[注册] 已跳转到: {tab.url[:80]}")
    else:
        cur = tab.url or ""
        print(f"[注册] 仍停留在第一页: {cur[:80]}")
        try:
            marker_info = {
                "first_name": bool(tab.ele("@name=first_name", timeout=0.5)),
                "email": bool(tab.ele("@name=email", timeout=0.5)),
                "password": bool(tab.ele("@name=password", timeout=0.5)),
                "submit": bool(tab.ele("@type=submit", timeout=0.5)),
            }
            print(f"[注册] 页面标记: {marker_info}")
        except Exception:
            pass

    if use_email_code and not is_on_password_page(tab):
        if is_blocked(tab):
            raise TurnstileError("注册第一页已被阻止，未进入密码页")
        raise TurnstileError("注册第一页提交后未进入密码页，无法继续邮箱验证码流程")

    # ── 在密码页等待 Turnstile 并点击「邮箱验证码继续」 ──
    if use_email_code:
        _handle_password_page_turnstile(tab)

    print(f"[注册] URL: {tab.url[:80]}")


def _wait_for_page_change(tab, old_url: str, timeout: int = 30) -> bool:
    """等待页面 URL 变化或出现新元素。timeout 为实际秒数。"""
    deadline = time.time() + timeout
    checks = 0
    while time.time() < deadline:
        cur = tab.url or ""
        if cur != old_url and "sign-up" not in cur.split("?")[0].rsplit("/", 1)[-1]:
            return True
        if "password" in cur or "magic-code" in cur:
            return True
        if is_blocked(tab):
            return True
        if checks > 0 and checks % 6 == 0:
            _simulate_human_move(tab)
        checks += 1
        time.sleep(0.8)
    return False


def is_on_password_page(tab) -> bool:
    """判断当前是否已经进入注册密码页。"""
    url = tab.url or ""
    if "sign-up/password" in url:
        return True
    try:
        if tab.ele("@name=password", timeout=1):
            return True
    except Exception:
        pass

    # 不再仅凭文案判断密码页。
    # Clerk 可能在第一页 DOM 中预渲染“使用邮箱验证码继续”文案，导致误判。
    try:
        has_first_name = bool(tab.ele("@name=first_name", timeout=0.5))
        has_email = bool(tab.ele("@name=email", timeout=0.5))
        has_submit = bool(tab.ele("@type=submit", timeout=0.5))
        if not has_first_name and not has_email and has_submit:
            body = (tab.ele("tag:body", timeout=0.5).text or "")[:500]
            if "密码" in body or "Password" in body:
                return True
    except Exception:
        pass
    return False


def _handle_password_page_turnstile(tab) -> None:
    """在密码页处理 Turnstile，然后点击「使用邮箱验证码继续」。

    原理：Turnstile invisible 模式在页面加载时自动评估，
    结果编码进 token（pass/fail）。CDP 被检测到会拉低评分。
    如果初始 token 是 fail token，点击被阻止后，
    retry 时 reset Turnstile → 积累更多行为数据 → 新 token 评分更高 → 通过。
    """
    if "magic-code" in (tab.url or ""):
        print("[注册] 已在验证码页，跳过 Turnstile 处理")
        return

    if not is_on_password_page(tab):
        raise TurnstileError(f"当前不在密码页，URL={tab.url}")

    # 行为信号积累：让 Turnstile ML 模型收集更多正面数据
    print("[注册] 密码页 — 模拟浏览行为中...")
    _simulate_rich_behavior(tab)
    time.sleep(random.uniform(2, 4))

    if is_blocked(tab):
        print("[注册] 密码页已被阻止")

    # 等待 Turnstile 完成评估
    print("[注册] 密码页等待 Turnstile...")
    _wait_for_turnstile_init(tab, timeout=20)
    _simulate_human_move(tab)
    time.sleep(random.uniform(1, 2))
    _wait_for_turnstile_token(tab, timeout=15)
    _simulate_human_move(tab)
    time.sleep(random.uniform(1, 2))

    _click_email_code_button(tab)


def _wait_for_turnstile_pass(tab, timeout: int = 30, max_resets: int = 2) -> bool:
    """等待 Turnstile 验证真正通过。

    策略分层：
    1. 等待浏览器自身无感验证通过（免费）
    2. 如果失败/被阻止，尝试重置 + 点击可见复选框
    3. 如果配置了 CAPSOLVER_API_KEY，调用外部求解器获取有效 token

    Returns: True = 验证通过, False = 被阻止或超时
    """
    print("[Turnstile] 等待验证...")
    _simulate_human_move(tab)

    resets_done = 0

    for i in range(timeout):
        url = tab.url or ""

        if "magic-code" in url:
            print("[Turnstile] 已跳到验证码页")
            return True

        # 检查 token
        token = _get_turnstile_response(tab)
        if token:
            print(f"[Turnstile] 获取到 token ({i+1}s), len={len(token)}")
            time.sleep(2)
            return True

        # 检查被阻止 → 尝试 CapSolver 外部求解
        if is_blocked(tab):
            if _solve_turnstile_direct(tab, reason="页面被阻止后"):
                return True
            if resets_done < max_resets:
                resets_done += 1
                print(f"[Turnstile] 被阻止，重置第 {resets_done}/{max_resets} 次...")
                try:
                    tab.run_js("try { turnstile.reset() } catch(e) { }")
                except Exception:
                    pass
                _simulate_human_move(tab)
                time.sleep(random.uniform(3, 6))
                _try_click_turnstile_checkbox(tab)
                time.sleep(3)
                continue
            else:
                print("[Turnstile] 多次重置后仍被阻止")
                return False

        # 检查失败提示
        try:
            body_text = tab.ele("tag:body").text or ""
            if "无法验证用户为真人" in body_text or "Can't verify" in body_text:
                if _solve_turnstile_direct(tab, reason="检测到失败提示后"):
                    return True
                if resets_done < max_resets:
                    resets_done += 1
                    print(f"[Turnstile] 验证失败，重置第 {resets_done}/{max_resets} 次...")
                    try:
                        tab.run_js("try { turnstile.reset() } catch(e) { }")
                    except Exception:
                        pass
                    _simulate_human_move(tab)
                    time.sleep(random.uniform(3, 5))
                    _try_click_turnstile_checkbox(tab)
                    time.sleep(3)
                    continue
        except Exception:
            pass

        # 检查按钮状态（仅当页面上有密码框时才检查，避免误判第一页按钮）
        try:
            has_password = tab.ele("@name=password", timeout=0.5)
            submit = tab.ele("@type=submit", timeout=0.5)
            if submit and has_password:
                is_disabled = submit.attr("disabled")
                if is_disabled:
                    if i == 0 or i % 10 == 0:
                        print(f"[Turnstile] 按钮 disabled, 验证中... ({i+1}s)")
                elif not is_disabled:
                    print(f"[Turnstile] 按钮变为 enabled ({i+1}s)，等待确认...")
                    time.sleep(random.uniform(4, 6))
                    if is_blocked(tab):
                        if _solve_turnstile_direct(tab, reason="按钮启用后被阻止"):
                            return True
                        if resets_done < max_resets:
                            resets_done += 1
                            print(f"[Turnstile] enabled 但被阻止，重置第 {resets_done} 次...")
                            try:
                                tab.run_js("try { turnstile.reset() } catch(e) { }")
                            except Exception:
                                pass
                            _simulate_human_move(tab)
                            time.sleep(random.uniform(3, 6))
                            _try_click_turnstile_checkbox(tab)
                            continue
                        else:
                            return False
                    # 再检查一次 body 有无失败关键字
                    try:
                        body = tab.ele("tag:body").text or ""
                        if "访问被阻止" in body or "无法验证" in body or "Can't verify" in body:
                            print("[Turnstile] 按钮 enabled 但页面有阻止提示")
                            if _solve_turnstile_direct(tab, reason="按钮启用但页面异常"):
                                return True
                            continue
                    except Exception:
                        pass
                    print(f"[Turnstile] 验证通过 ({i+1}s)")
                    return True
        except Exception:
            pass

        if i > 3 and i % 8 == 0:
            _try_click_turnstile_checkbox(tab)

        time.sleep(random.uniform(0.8, 1.5))

    print(f"[Turnstile] {timeout}s 超时")
    # 最后一次尝试 CapSolver
    if _solve_turnstile_direct(tab, reason="等待超时后"):
        return True
    return False


def _try_capsolver_fallback(tab) -> bool:
    """兼容旧调用，内部转到主动求解逻辑。"""
    if not _has_capsolver() and not _has_twocaptcha():
        print("[Turnstile] 未配置外部求解器，跳过")
        print("[Turnstile] 提示: 在 .env 添加 CAPSOLVER_API_KEY 或 TWOCAPTCHA_API_KEY")
        return False
    return _solve_turnstile_direct(tab, reason="兼容 fallback")


def _click_email_code_button(tab, max_attempts: int = 3) -> bool:
    """找到并点击「使用邮箱验证码继续」按钮，跳过密码页。

    点击后如果被阻止，用 CapSolver 重新获取 token 并重试。
    """
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
            _simulate_rich_behavior(tab)
            time.sleep(random.uniform(3, 5))
            # reset + 等新 token
            try:
                tab.run_js("try { turnstile.reset() } catch(e) { }")
            except Exception:
                pass
            time.sleep(random.uniform(2, 4))
            _wait_for_turnstile_token(tab, timeout=10)

        # token 就绪后尽快点击，减少被内部消费的时间窗口
        _simulate_human_move(tab)
        time.sleep(random.uniform(0.3, 0.8))

        for text in candidates:
            try:
                btn = tab.ele(f"text:{text}", timeout=4)
                if not btn:
                    continue
                tag = btn.tag.lower() if hasattr(btn, "tag") else ""
                if tag in ("script", "style", "noscript"):
                    continue
                print(f"[注册] 找到「{text}」(tag={tag})，点击...")
                _simulate_human_move(tab)
                time.sleep(random.uniform(0.5, 1.0))
                btn.click()
                for _ in range(8):
                    if is_blocked(tab) or "magic-code" in (tab.url or ""):
                        break
                    time.sleep(1)

                if is_blocked(tab):
                    print(f"[注册] 点击「{text}」后被阻止 (attempt {attempt}/{max_attempts})")
                    break
                if "magic-code" in (tab.url or ""):
                    print(f"[注册] 点击「{text}」成功，已跳转验证码页")
                    return True
                print(f"[注册] 点击「{text}」成功")
                return True
            except Exception:
                continue
        else:
            print("[注册] 未找到邮箱验证码按钮")
            return False

    print(f"[注册] {max_attempts} 次点击邮箱验证码按钮均被阻止")
    return False


def fill_password_and_submit(tab, password: str) -> None:
    """注册第二步：填密码，提交。"""
    if not tab.ele("@name=password", timeout=15):
        raise RuntimeError("密码输入框未找到（15s 超时）")

    _random_wait(0.5, 1.5)
    tab.ele("@name=password").input(password)
    print("[注册] 密码已填写")
    time.sleep(random.uniform(1, 3))

    tab.ele("@type=submit").click()
    print("[注册] 密码已提交")

    # 检查邮箱是否已被注册
    time.sleep(3)
    if tab.ele("This email is not available.", timeout=2):
        raise RuntimeError("该邮箱已被注册")

    handle_turnstile(tab)
    print(f"[注册] 密码提交后, URL: {tab.url[:80]}")


# ═══════════════════════════════════════════════════════════════════════
# 登录
# ═══════════════════════════════════════════════════════════════════════

DASHBOARD_URL = "https://cursor.com/settings"


def navigate_to_login(tab) -> None:
    print("[登录] 打开登录页...")
    tab.get(LOGIN_URL)
    time.sleep(random.uniform(3, 5))

    if not tab.ele("@name=email", timeout=5):
        handle_turnstile(tab)

    if not tab.ele("@name=email", timeout=15):
        raise RuntimeError(f"登录页表单未加载。title={tab.title}, URL={tab.url[:100]}")

    print(f"[登录] 当前页面: {tab.url[:80]}")


def fill_login_email(tab, email: str) -> None:
    tab.actions.click("@name=email").input(email)
    time.sleep(random.uniform(1.5, 3.0))
    tab.actions.click("@type=submit")
    print(f"[登录] 邮箱已提交，等待 Turnstile...")
    time.sleep(random.uniform(3, 5))
    _simulate_human_move(tab)
    _wait_for_turnstile_init(tab, timeout=20)
    _wait_for_turnstile_token(tab, timeout=15)
    time.sleep(random.uniform(2, 4))
    print(f"[登录] URL: {tab.url[:80]}")


def fill_login_password(tab, password: str) -> None:
    if not tab.ele("@name=password", timeout=15):
        raise RuntimeError("密码输入框未找到")
    tab.ele("@name=password").input(password)
    time.sleep(random.uniform(1, 3))
    tab.ele("@type=submit").click()
    time.sleep(5)
    handle_turnstile(tab)
    print(f"[登录] 密码已提交, URL: {tab.url[:80]}")


def login_with_email_code(tab, email: str) -> None:
    """登录：填邮箱 → 点「邮箱登录验证码」→ 等待验证码页面。"""
    fill_login_email(tab, email)

    # 在密码页点击「邮箱登录验证码」
    candidates = [
        "邮箱登录验证码",
        "使用邮箱验证码登录",
        "Continue with email code",
        "Use email code",
    ]
    clicked = False
    _simulate_human_move(tab)
    time.sleep(random.uniform(0.5, 1.0))
    for text in candidates:
        try:
            btn = tab.ele(f"text:{text}", timeout=4)
            if not btn:
                continue
            tag = btn.tag.lower() if hasattr(btn, "tag") else ""
            if tag in ("script", "style", "noscript"):
                continue
            _simulate_human_move(tab)
            time.sleep(random.uniform(0.5, 1.0))
            btn.click()
            print(f"[登录] 点击「{text}」")
            clicked = True
            for _ in range(8):
                if is_blocked(tab) or "magic-code" in (tab.url or ""):
                    break
                time.sleep(1)
            break
        except Exception:
            continue

    if not clicked:
        print("[登录] 未找到邮箱验证码按钮")


def _page_body_text(tab) -> str:
    try:
        el = tab.ele("tag:body", timeout=2)
        return (el.text if el else "") or ""
    except Exception:
        return ""


def _click_first_text(tab, texts: tuple[str, ...]) -> bool:
    for text in texts:
        try:
            el = tab.ele(f"text:{text}", timeout=2)
            if not el:
                continue
            el.click()
            return True
        except Exception:
            continue
    return False


def complete_desktop_signin_steps(tab, timeout: int = 60) -> bool:
    """验证码后点完 Continue to sign in → Return to Cursor。"""
    from desktop_signin import (
        CONTINUE_BUTTON_TEXTS,
        RETURN_BUTTON_TEXTS,
        complete_desktop_signin_steps as _run,
    )

    return _run(
        get_url=lambda: tab.url or "",
        get_body=lambda: _page_body_text(tab),
        click_continue=lambda: _click_first_text(tab, CONTINUE_BUTTON_TEXTS),
        click_return=lambda: _click_first_text(tab, RETURN_BUTTON_TEXTS),
        pause=lambda: time.sleep(1),
        timeout=timeout,
    )


def wait_for_login_complete(tab, timeout: int = 60) -> bool:
    """验证码后先完成桌面确认，再认主站已登录。"""
    if complete_desktop_signin_steps(tab, timeout=timeout):
        print("[登录] 登录成功（桌面确认完成）")
        return True
    if is_logged_in(tab):
        print(f"[登录] 登录成功，URL: {(tab.url or '')[:80]}")
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════
# 验证码 & 状态检测
# ═══════════════════════════════════════════════════════════════════════

def fill_verification_code(tab, code: str) -> None:
    """填入邮箱验证码 — Clerk 6 位独立输入框，用 @data-index 定位。"""
    time.sleep(random.uniform(1.5, 3.0))
    try:
        for i, digit in enumerate(code):
            tab.ele(f"@data-index={i}").input(digit)
            time.sleep(random.uniform(0.15, 0.4))
        print(f"[验证码] 已填入 {code}")
        time.sleep(random.uniform(2, 4))
    except Exception as e:
        print(f"[验证码] 填入失败: {e}")


def is_logged_in(tab) -> bool:
    from desktop_signin import is_unfinished_handoff_url

    url = tab.url or ""
    if is_unfinished_handoff_url(url):
        return False
    if any(kw in url for kw in ["dashboard", "settings", "/~", "billing", "/agents"]):
        return True
    try:
        body = tab.ele("tag:body").text or ""
        for kw in ["Account Settings", "Billing", "Overview", "Usage", "Settings"]:
            if kw in body:
                return True
    except Exception:
        pass
    return False


def is_blocked(tab) -> bool:
    try:
        body_text = tab.ele("tag:body").text or ""
        for kw in ["访问被阻止", "Access denied", "Access blocked", "请联系支持"]:
            if kw in body_text:
                return True
    except Exception:
        pass
    return False


def is_on_verification_page(tab) -> bool:
    """检测是否在验证码页面 — 用 @data-index=0 检测（Clerk 特征）。"""
    try:
        if tab.ele("@data-index=0", timeout=3):
            return True
    except Exception:
        pass
    return False
