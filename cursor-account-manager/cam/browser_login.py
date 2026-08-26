"""浏览器登录：Playwright + 邮箱验证码 → PKCE 拿 token。

全局并发：BROWSER_LOGIN_CONCURRENCY（默认 5）。
失败只向上抛 BrowserLoginError，由调用方累计 consecutive_failures。
"""

from __future__ import annotations

import base64
import hashlib
import ntpath
import os
import pathlib
import random
import shutil
import sys
import tempfile
import threading
import time
import uuid
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

import requests
# patchright = undetected Playwright（修复 Runtime.enable Leak 等 CDP 层泄漏）
# https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python
from patchright.sync_api import Page, Playwright, sync_playwright

from . import email_client, turnstile_solver
from .config import (
    CURSOR_AUTH_POLL_URL,
    CURSOR_LOGIN_DEEP_CONTROL,
    CURSOR_SIGN_IN_URL,
    SETTINGS,
)
from .logger import get
from .models import BrowserLoginError

log = get("browser")


_LOGIN_SEMAPHORE = threading.Semaphore(max(1, SETTINGS.browser_login_concurrency))

# user-data 基目录：放在用户 home 下，避免 /tmp 清理；每个账号一个独立子目录
_USER_DATA_BASE = pathlib.Path.home() / ".cam" / "chrome-profiles"
_USER_DATA_BASE.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════
# PKCE 参数
# ═══════════════════════════════════════════════════════════════════════

def _generate_pkce() -> dict:
    """生成 PKCE 三元组 + uuid。s=verifier, n=challenge(base64url of sha256), r=uuid。"""
    t = os.urandom(32)
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")
    s = b64(t)
    n = b64(hashlib.sha256(s.encode()).digest())
    r = str(uuid.uuid4())
    return {"s": s, "n": n, "r": r}


def _poll_token(
    auth_uuid: str, verifier: str,
    proxy: Optional[str] = None,
    max_attempts: int = 40, interval: int = 2,
) -> tuple[str, str]:
    """轮询 Cursor auth poll API 拿 access/refresh token。"""
    url = f"{CURSOR_AUTH_POLL_URL}?uuid={auth_uuid}&verifier={verifier}"
    headers = {"Content-Type": "application/json"}
    proxies = {"http": proxy, "https": proxy} if proxy else None

    for _ in range(max_attempts):
        try:
            r = requests.get(url, headers=headers, timeout=10, proxies=proxies)
            if r.status_code == 200:
                data = r.json()
                if data.get("accessToken") and data.get("refreshToken"):
                    return data["accessToken"], data["refreshToken"]
        except Exception as e:
            log.debug(f"poll_token 异常: {e}")
        time.sleep(interval)

    raise BrowserLoginError("auth/poll 轮询超时")


# ═══════════════════════════════════════════════════════════════════════
# 浏览器工具
# ═══════════════════════════════════════════════════════════════════════

def _parse_proxy(proxy: str) -> Optional[dict]:
    """`http://user:pass@host:port` → Playwright proxy dict。"""
    if not proxy:
        return None
    from urllib.parse import urlparse
    u = urlparse(proxy)
    if not u.hostname:
        return None
    d = {"server": f"{u.scheme}://{u.hostname}:{u.port or 80}"}
    if u.username:
        d["username"] = u.username
    if u.password:
        d["password"] = u.password
    return d


def _user_data_dir_for(email_addr: str) -> str:
    """每个账号一个独立 user_data_dir：保留 cookie/缓存/指纹，多次登录更像真人。"""
    safe = "".join(c if c.isalnum() else "_" for c in email_addr)
    path = _USER_DATA_BASE / safe
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _has_system_chrome(platform: str | None = None) -> bool:
    """检测系统 Chrome；Windows/macOS 不能依赖 PATH。"""
    platform = platform or sys.platform

    if platform == "darwin":
        chrome_paths = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
        return any(os.path.exists(p) for p in chrome_paths)

    if platform.startswith("win"):
        chrome_paths = [
            ntpath.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Google", "Chrome", "Application", "chrome.exe"),
            ntpath.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Google", "Chrome", "Application", "chrome.exe"),
            ntpath.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        ]
        return any(p and os.path.exists(p) for p in chrome_paths) or bool(
            shutil.which("chrome") or shutil.which("chrome.exe") or shutil.which("google-chrome")
        )

    return bool(shutil.which("google-chrome") or shutil.which("google-chrome-stable") or shutil.which("chrome"))


def _requires_system_chrome(platform: str | None = None) -> bool:
    """桌面环境必须使用系统 Chrome，避免误走 patchright 内置 Chromium。"""
    platform = platform or sys.platform
    return platform == "darwin" or platform.startswith("win")


def _launch(pw: Playwright, headless: bool, proxy: str, email_addr: str):
    """patchright 官方最佳实践：
    - launch_persistent_context（保留指纹）
    - 桌面 Windows/macOS 必须用 channel="chrome"（真实 Chrome）
    - Linux 服务器没有系统 Chrome 时才 fallback 到 patchright 内置 Chromium
    - no_viewport=True（不人为设置 viewport）
    - 不传 user_agent / 不传 args / 不注入 stealth JS

    patchright 已在 CDP 层修复 Runtime.enable / Console.enable / 命令行 flag 泄漏，
    额外 JS stealth 反而会被 Cloudflare 识别为"过度伪装"。
    """
    user_data_dir = _user_data_dir_for(email_addr)
    log.info(f"user-data: {user_data_dir}")

    ignore_args = [
        "--enable-automation",
        "--no-sandbox",
        "--disable-extensions",
        "--disable-component-extensions-with-background-pages",
        "--disable-default-apps",
        "--disable-component-update",
        "--disable-features=ImprovedCookieControls,LazyFrameLoading,GlobalMediaControls,"
        "DestroyProfileOnBrowserClose,MediaRouter,DialMediaRouteProvider,"
        "AcceptCHFrame,AutoExpandDetailsElement,CertificateTransparencyComponentUpdater,"
        "AvoidUnnecessaryBeforeUnloadCheckSync,Translate,HttpsUpgrades,"
        "PaintHolding,PrivacySandboxSettings4,PushMessaging,"
        "CalculateNativeWinOcclusion,BackForwardCache,OptimizationHints",
    ]

    is_linux = sys.platform.startswith("linux")
    has_chrome = _has_system_chrome()

    kwargs = {
        "user_data_dir": user_data_dir,
        "headless": headless,
        "no_viewport": True,
        "ignore_default_args": ignore_args,
    }
    log.info(f"浏览器启动模式: {'headless' if headless else 'headed'}")

    if has_chrome:
        kwargs["channel"] = "chrome"
        log.info("使用系统 Chrome")
    else:
        if _requires_system_chrome():
            raise BrowserLoginError(
                "当前平台必须使用系统 Google Chrome，但未检测到 Chrome。"
                "请先安装 Google Chrome 后重试，避免误用 patchright 内置 Chromium 被检测。"
            )
        log.info("使用 patchright 内置 Chromium（服务器模式）")
        if is_linux:
            # Linux 服务器没有用户命名空间沙箱，必须加 --no-sandbox；
            # 同时 /dev/shm 可能很小，用 --disable-dev-shm-usage 改为写到 /tmp
            kwargs["args"] = ["--no-sandbox", "--disable-dev-shm-usage"]

    proxy_cfg = _parse_proxy(proxy)
    if proxy_cfg:
        kwargs["proxy"] = proxy_cfg

    context = pw.chromium.launch_persistent_context(**kwargs)
    page = context.pages[0] if context.pages else context.new_page()
    # 代理首次建连慢（10-15s），给足够的超时裕量
    _default_timeout = 60000 if proxy_cfg else 30000
    page.set_default_timeout(_default_timeout)
    context.set_default_timeout(_default_timeout)
    return context, page


def _clear_user_data(email_addr: str) -> None:
    """删除账号对应的 user_data_dir，彻底重置浏览器指纹。"""
    try:
        shutil.rmtree(_user_data_dir_for(email_addr), ignore_errors=True)
    except Exception:
        pass


def _human_pause(low: float = 0.5, high: float = 1.5) -> None:
    time.sleep(random.uniform(low, high))


def _type_like_human(page: Page, selector: str, text: str) -> None:
    page.click(selector)
    _human_pause(0.2, 0.5)
    for ch in text:
        page.keyboard.type(ch, delay=random.randint(40, 140))


# ═══════════════════════════════════════════════════════════════════════
# Turnstile 处理
# ═══════════════════════════════════════════════════════════════════════

def _get_turnstile_token(page: Page) -> str:
    try:
        return page.evaluate(r"""() => {
            try {
                const r = window.turnstile && turnstile.getResponse && turnstile.getResponse();
                if (r && r.length > 20) return r;
            } catch(e) {}
            for (const sel of ['input[name="cf-turnstile-response"]',
                               '[name="cf-chl-turnstile-response"]']) {
                const el = document.querySelector(sel);
                if (el && el.value && el.value.length > 20) return el.value;
            }
            return '';
        }""") or ""
    except Exception:
        return ""


def _wait_turnstile_token(page: Page, timeout: int = 20) -> bool:
    for _ in range(timeout):
        if _get_turnstile_token(page):
            return True
        time.sleep(1)
    return False


def _has_turnstile_challenge(page: Page) -> bool:
    """页面上是否存在需要用户交互的 Turnstile（interactive 模式，有可见 checkbox）。

    注意：只判 interactive。invisible Turnstile 由后端自己执行，无需我们干预，
    在 Cursor password/login 页经常存在但不阻挡流程。
    """
    try:
        # 1. 主 DOM 里的 widget 必须可见且有尺寸
        visible_widget = page.evaluate(r"""() => {
            for (const sel of ['#cf-turnstile', '.cf-turnstile', '[data-sitekey]', '[id^="cf-chl-widget"]']) {
                for (const el of document.querySelectorAll(sel)) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 20 && r.height > 20) return true;
                }
            }
            return false;
        }""")
        if visible_widget:
            return True

        # 2. cloudflare iframe 必须有可见尺寸（invisible 通常是 0x0 或 display:none）
        for f in page.frames:
            if "challenges.cloudflare.com" not in (f.url or ""):
                continue
            try:
                fe = f.frame_element()
                box = fe.bounding_box()
                if box and box.get("width", 0) > 20 and box.get("height", 0) > 20:
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False


def _try_click_turnstile_checkbox(page: Page) -> bool:
    """尝试点击 Turnstile 的 checkbox（interactive 模式）。"""
    try:
        for frame in page.frames:
            url = frame.url or ""
            if "challenges.cloudflare.com" not in url:
                continue
            try:
                cb = frame.locator("input[type='checkbox']")
                if cb.count() > 0:
                    cb.first.click(timeout=3000)
                    log.info("点击了 Turnstile iframe 内的 checkbox")
                    time.sleep(2)
                    return True
            except Exception:
                continue
    except Exception:
        pass

    try:
        widgets = page.locator("[id^='cf-chl-widget'], .cf-turnstile, #cf-turnstile")
        for i in range(min(widgets.count(), 3)):
            try:
                widgets.nth(i).click(force=True, timeout=2000)
                log.info("点击了 Turnstile widget 外壳")
                time.sleep(2)
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _extract_sitekey(page: Page) -> str:
    try:
        return page.evaluate(r"""() => {
            for (const el of document.querySelectorAll('[data-sitekey]')) {
                const k = el.getAttribute('data-sitekey');
                if (k && k.length > 10) return k;
            }
            for (const f of document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]')) {
                try {
                    const u = new URL(f.src);
                    const k = u.searchParams.get('k') || u.searchParams.get('sitekey');
                    if (k && k.length > 10) return k;
                } catch(e) {}
            }
            return '';
        }""") or ""
    except Exception:
        return ""


def _inject_turnstile_token(page: Page, token: str) -> None:
    try:
        page.evaluate(r"""(tk) => {
            for (const sel of ['input[name="cf-turnstile-response"]',
                               '[name="cf-chl-turnstile-response"]']) {
                const el = document.querySelector(sel);
                if (el) el.value = tk;
            }
            try { if (window.turnstile) window.turnstile.getResponse = () => tk; } catch(e) {}
            try {
                for (const el of document.querySelectorAll('[data-callback]')) {
                    const cb = el.getAttribute('data-callback');
                    if (cb && window[cb]) window[cb](tk);
                }
            } catch(e) {}
        }""", token)
    except Exception as e:
        log.debug(f"token 注入异常: {e}")


def _pass_turnstile_once(page: Page, timeout: int = 30) -> bool:
    """尝试过一次 Turnstile（点 checkbox 或等 invisible）。成功返回 True。

    核心路径（参考 JiuZ-Chn/Cursor-Register）：
      #cf-turnstile → iframe(challenges.cloudflare.com) → input[type=checkbox]
    """
    if not _has_turnstile_challenge(page):
        return True

    # 等 iframe 加载
    deadline = time.time() + timeout
    while time.time() < deadline:
        # invisible 已经通过了？
        if _get_turnstile_token(page):
            return True

        cf_frame = None
        for f in page.frames:
            if "challenges.cloudflare.com" in (f.url or ""):
                cf_frame = f
                break
        if cf_frame is None:
            time.sleep(1)
            continue

        try:
            cb = cf_frame.locator("input[type='checkbox']")
            cnt = cb.count()
            log.debug(f"Turnstile iframe 内 checkbox 数量: {cnt}，iframe URL: {cf_frame.url[:80]}")
            if cnt > 0:
                # 等 checkbox 真正可交互
                cb.first.wait_for(state="visible", timeout=5000)
                box = cb.first.bounding_box()
                log.debug(f"checkbox bounding_box: {box}")
                time.sleep(random.uniform(0.8, 1.8))  # 人类反应时间
                if box:
                    # 用页面坐标点击，在 Xvfb 环境下比 locator.click 更可靠
                    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    log.info(f"鼠标点击 Turnstile checkbox ({box['x']:.0f},{box['y']:.0f})")
                else:
                    cb.first.click(timeout=5000)
                    log.info("点击 Turnstile checkbox（iframe 内，fallback）")
                # 点完等 token 生成
                for _ in range(15):
                    if _get_turnstile_token(page):
                        log.info("Turnstile 通过")
                        return True
                    time.sleep(1)
            else:
                log.debug(f"Turnstile iframe 内无 checkbox，frame 子元素: {cf_frame.locator('*').count()}")
        except Exception as e:
            log.debug(f"点击 checkbox 异常: {e}")

        time.sleep(1)
    return False


def _extract_sitekey_deep(page: Page) -> str:
    """深度提取 sitekey：frame URL → data-sitekey 属性 → JS 全局 → 源码正则。"""
    import re as _re

    # 1. 最可靠：直接遍历所有 frame，找 challenges.cloudflare.com 里的 k/sitekey 参数
    try:
        for frame in page.frames():
            url = frame.url or ""
            if "challenges.cloudflare.com" in url:
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(url).query)
                k = (qs.get("k") or qs.get("sitekey") or [[]])[0]
                if k and len(k) > 10:
                    return k[0] if isinstance(k, list) else k
    except Exception:
        pass

    # 2. DOM 属性 / JS 全局 / 页面源码正则
    try:
        key = page.evaluate(r"""() => {
            // data-sitekey 属性（含 shadow DOM 里的）
            const all = [...document.querySelectorAll('[data-sitekey]'),
                         ...(document.body ? document.body.querySelectorAll('[data-sitekey]') : [])];
            for (const el of all) {
                const k = el.getAttribute('data-sitekey');
                if (k && k.length > 10) return k;
            }
            // iframe src 参数
            for (const f of document.querySelectorAll('iframe')) {
                try {
                    const u = new URL(f.src);
                    if (u.hostname.includes('cloudflare.com')) {
                        const k = u.searchParams.get('k') || u.searchParams.get('sitekey');
                        if (k && k.length > 10) return k;
                    }
                } catch(e) {}
            }
            // window.turnstile._cfConfig
            try {
                const cfg = window.turnstile && window.turnstile._cfConfig;
                if (cfg) for (const key in cfg) {
                    const k = cfg[key] && cfg[key].sitekey;
                    if (k && k.length > 10) return k;
                }
            } catch(e) {}
            // 页面源码正则：Turnstile sitekey 是 0x 开头 20+ 位十六进制
            const html = document.documentElement.outerHTML;
            const m = html.match(/0x[0-9a-fA-F]{20,}/);
            if (m) return m[0];
            return '';
        }""") or ""
        if key:
            return key
    except Exception:
        pass

    # 3. 兜底：抓取页面全部 HTML 用正则扫描（包括 JS 里的内联 sitekey）
    try:
        html = page.content()
        m = _re.search(r'0x[0-9a-fA-F]{20,}', html)
        if m:
            return m.group(0)
    except Exception:
        pass

    return ""


def _ensure_turnstile_passed(page: Page, attempts: int = 3) -> bool:
    """组合策略：无 Turnstile 跳过 → 点 checkbox → 外部求解器。"""
    if not _has_turnstile_challenge(page):
        return True

    for i in range(attempts):
        if _pass_turnstile_once(page, timeout=25):
            return True
        log.info(f"Turnstile 第 {i+1}/{attempts} 次点击尝试失败")

    if not turnstile_solver.has_external_solver():
        log.warning("Turnstile 点击方式未通过，且未配置外部求解器")
        return False

    sitekey = _extract_sitekey_deep(page)
    if not sitekey:
        # 打印所有 frame URL 帮助诊断
        try:
            frame_urls = [f.url for f in page.frames() if f.url and f.url != "about:blank"]
            log.warning(f"Turnstile 存在但未找到 sitekey，无法求解。当前 frames: {frame_urls}")
        except Exception:
            log.warning("Turnstile 存在但未找到 sitekey，无法求解")
        return False

    try:
        log.info(f"调用外部求解器 sitekey={sitekey[:16]}...")
        token = turnstile_solver.solve(page.url, sitekey, timeout=120)
    except Exception as e:
        log.error(f"Turnstile 求解失败: {e}")
        return False

    _inject_turnstile_token(page, token)
    log.info("Turnstile token 已通过外部求解器注入")
    time.sleep(2)
    return True


# ═══════════════════════════════════════════════════════════════════════
# 登录步骤
# ═══════════════════════════════════════════════════════════════════════

def _current_url(page: Page) -> str:
    try:
        return page.url or ""
    except Exception:
        return ""


def _wait_url_contains(page: Page, keyword: str, timeout: int = 8) -> bool:
    """URL 包含 keyword 就返回 True。

    优先用 Playwright 原生 wait_for_url（跳转瞬间立即返回，不用轮询），
    并用一个短轮询兜底避免 Playwright 事件丢失。
    """
    # 先检查当前就已经匹配
    if keyword in _current_url(page):
        return True
    try:
        page.wait_for_url(lambda url: keyword in (url or ""), timeout=timeout * 1000)
        return True
    except Exception:
        pass
    # 兜底：wait_for_url 可能错过瞬时跳转，再 poll 一次当前 URL
    return keyword in _current_url(page)


def _goto_login_page(page: Page) -> None:
    """打开 cursor.com/login，会自动 302 到 authenticator.cursor.sh/?...OAuth参数。"""
    log.info("打开登录页...")
    page.goto(CURSOR_SIGN_IN_URL, wait_until="domcontentloaded", timeout=60000)
    _human_pause(2, 4)
    try:
        page.wait_for_selector('[name="email"]', timeout=20000)
        return
    except Exception:
        pass

    log.info("邮箱输入框未出现，尝试过 Turnstile ...")
    _ensure_turnstile_passed(page)
    page.wait_for_selector('[name="email"]', timeout=30000)


# ─── 关键：直接等目标元素，不看 URL ─────────────────────────────────────
#
# Cursor 登录 3 个关键元素（DOM 层面，不依赖 URL 和文本）：
#   • 邮箱页:   input[name="email"]
#   • 密码页:   button[value="magic-code"]（"邮箱登录验证码"按钮）
#   • 验证码页: input[data-index="0"]（6 位码第一格）
#
# 不管代理多慢、URL 跳没跳，只要目标元素在 DOM 里出现 → 继续推进。

_SEL_EMAIL      = 'input[name="email"]'
_SEL_MAGIC_BTN  = 'button[value="magic-code"]'
_SEL_CODE_INPUT = 'input[data-index="0"]'
_CHANGE_EMAIL_SELECTORS = (
    'text=更改电子邮件',
    'text=Change email',
    'a:has-text("更改电子邮件")',
    'a:has-text("Change email")',
    'button:has-text("更改电子邮件")',
    'button:has-text("Change email")',
)


def _norm_email(value: str) -> str:
    return (value or "").strip().lower()


def _read_email_field_state(page: Page) -> dict:
    """读取邮箱框是否存在、当前值、是否可编辑。"""
    try:
        state = page.evaluate(
            """() => {
                const el = document.querySelector('input[name="email"]');
                if (!el) return {exists: false, value: '', editable: false};
                const readonlyAttr = el.getAttribute('readonly');
                const editable = !(
                    el.readOnly
                    || el.disabled
                    || (readonlyAttr !== null && readonlyAttr !== undefined)
                    || el.tabIndex < 0
                );
                return {
                    exists: true,
                    value: el.value || '',
                    editable: Boolean(editable),
                };
            }"""
        )
        if isinstance(state, dict):
            return {
                "exists": bool(state.get("exists")),
                "value": str(state.get("value") or ""),
                "editable": bool(state.get("editable")),
            }
    except Exception:
        pass
    return {"exists": False, "value": "", "editable": False}


def _email_matches_page(page: Page, email_addr: str) -> bool:
    """密码页 URL 或只读邮箱是否已是目标账号。"""
    want = _norm_email(email_addr)
    if not want:
        return False
    state = _read_email_field_state(page)
    if _norm_email(state.get("value") or "") == want:
        return True
    try:
        query = parse_qs(urlparse(_current_url(page)).query)
        for key in ("email", "login_hint"):
            for raw in query.get(key) or ():
                if _norm_email(unquote(str(raw))) == want:
                    return True
    except Exception:
        pass
    return False


def _is_code_step(page: Page) -> bool:
    try:
        return page.locator(_SEL_CODE_INPUT).count() > 0
    except Exception:
        return False


def _is_password_step(page: Page) -> bool:
    """是否已在密码页（可点邮箱验证码，邮箱通常只读）。"""
    try:
        if page.locator(_SEL_MAGIC_BTN).count() > 0:
            return True
    except Exception:
        pass
    if "/password" in _current_url(page):
        return True
    state = _read_email_field_state(page)
    return bool(state["exists"] and state["value"] and not state["editable"])


def _click_change_email(page: Page) -> bool:
    """点「更改电子邮件」，回到可编辑邮箱页。"""
    for sel in _CHANGE_EMAIL_SELECTORS:
        try:
            loc = page.locator(sel)
            if loc.count() <= 0:
                continue
            loc.first.click(timeout=5000)
            log.info("已点击更改电子邮件")
            return True
        except Exception:
            continue
    return False


def _wait_editable_email(page: Page, timeout_ms: int = 15000) -> bool:
    """等待可编辑邮箱框出现。"""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        state = _read_email_field_state(page)
        if state["exists"] and state["editable"]:
            return True
        time.sleep(0.25)
    return False


def _submit_email_with_retry(page: Page, email_addr: str, max_retries: int = 3) -> None:
    """填邮箱 → 提交 → 等 magic-code / 验证码框；已在密码页且邮箱匹配则跳过 fill。"""
    for retry in range(max_retries):
        try:
            if _is_code_step(page):
                log.info("已在验证码页，跳过填邮箱")
                return

            if _is_password_step(page):
                if _email_matches_page(page, email_addr):
                    log.info("已在密码页且邮箱匹配，跳过填邮箱，继续验证码流程")
                    return
                log.info(
                    "密码页邮箱与目标不一致，尝试更改电子邮件 shown_url=%s",
                    _current_url(page)[:120],
                )
                if not _click_change_email(page):
                    raise BrowserLoginError("密码页邮箱不匹配且未找到更改电子邮件入口")
                _human_pause(1.0, 2.0)
                if not _wait_editable_email(page):
                    raise BrowserLoginError("更改电子邮件后未出现可编辑邮箱框")

            page.wait_for_selector(_SEL_EMAIL, timeout=15000)
            state = _read_email_field_state(page)
            if state["exists"] and not state["editable"]:
                # 仍可能刚跳到密码页
                if _email_matches_page(page, email_addr):
                    log.info("邮箱框只读且已匹配目标，跳过填邮箱")
                    return
                raise BrowserLoginError("邮箱框只读且与目标账号不匹配")

            log.info(f"[{retry+1}/{max_retries}] 填邮箱: {email_addr}")
            page.fill(_SEL_EMAIL, "")
            _type_like_human(page, _SEL_EMAIL, email_addr)
            _human_pause(1.0, 2.0)
            page.click('[type="submit"]')
            log.info("邮箱已提交，等待下一页元素...")

            # 等下一页的任一标志性元素（密码页按钮 或 直接是验证码页输入框）
            try:
                page.wait_for_selector(
                    f"{_SEL_MAGIC_BTN}, {_SEL_CODE_INPUT}",
                    timeout=60000,
                )
                log.info("已进入下一页（DOM 就绪）")
                return
            except Exception:
                pass

            # 元素仍没出现：看是不是 interactive Turnstile 挡着
            if _has_turnstile_challenge(page):
                log.info("检测到 interactive Turnstile，尝试点击")
                _ensure_turnstile_passed(page)
                try:
                    page.wait_for_selector(
                        f"{_SEL_MAGIC_BTN}, {_SEL_CODE_INPUT}",
                        timeout=30000,
                    )
                    return
                except Exception:
                    pass

            log.warning(f"60s 未出现下一页元素，URL: {_current_url(page)[:120]}")
        except Exception as e:
            log.warning(f"提交邮箱异常: {e}")

        log.info(f"refresh 重试 {retry+1}/{max_retries}")
        try:
            page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        _human_pause(2, 4)

    raise BrowserLoginError(f"提交邮箱 {max_retries} 次后仍未就绪")


def _click_magic_code_button(page: Page, max_retries: int = 3) -> None:
    """点"邮箱登录验证码"按钮，等验证码输入框（data-index=0）出现。"""
    # 同时监听验证码输入框 OR Turnstile iframe 出现，不再死等 60s
    _TURNSTILE_SEL = "iframe[src*='challenges.cloudflare.com'], [id^='cf-chl-widget'], .cf-turnstile, #cf-turnstile"
    _BOTH_SEL = f"{_SEL_CODE_INPUT}, {_TURNSTILE_SEL}"

    for retry in range(max_retries):
        try:
            # 已经在验证码页了就直接返回
            if page.locator(_SEL_CODE_INPUT).count() > 0:
                log.info("已在验证码页（code 输入框就绪）")
                return

            btn = page.locator(_SEL_MAGIC_BTN).first
            btn.wait_for(state="visible", timeout=8000)
            _human_pause(0.8, 1.8)
            btn.click(timeout=5000)
            log.info(f"[{retry+1}/{max_retries}] 点击 magic-code 按钮")

            # 等验证码框或 Turnstile 其中一个先出现（最多 30s）
            try:
                page.wait_for_selector(_BOTH_SEL, timeout=30000)
            except Exception:
                pass

            # 已经到验证码页了？
            if page.locator(_SEL_CODE_INPUT).count() > 0:
                log.info("验证码输入框已就绪")
                return

            # 出现了 Turnstile，立即处理
            if _has_turnstile_challenge(page):
                log.info("检测到 interactive Turnstile（magic-code 后），处理中...")
                _ensure_turnstile_passed(page)
                try:
                    page.wait_for_selector(_SEL_CODE_INPUT, timeout=30000)
                    log.info("Turnstile 通过，验证码输入框就绪")
                    return
                except Exception:
                    pass

            # 检查后端是否报人机验证失败
            try:
                body = page.locator("body").inner_text(timeout=1500).lower()
                if "verify the user is human" in body or "确认您是真人" in body:
                    log.warning("后端报 Turnstile 未通过（magic-code 后）")
            except Exception:
                pass
            log.warning(f"未出现验证码输入框，URL: {_current_url(page)[:120]}")
        except Exception as e:
            log.warning(f"点击 magic-code 按钮异常: {e}")

        log.info(f"refresh 重试 {retry+1}/{max_retries}")
        try:
            page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        _human_pause(2, 4)

    raise BrowserLoginError(f"点 magic-code 按钮 {max_retries} 次后仍未就绪")


def _fill_verification_code(page: Page, code: str) -> None:
    log.info(f"填入验证码: {code}")
    try:
        page.wait_for_selector('[data-index="0"]', timeout=15000)
    except Exception:
        raise BrowserLoginError("验证码输入框未出现")

    for i, digit in enumerate(code):
        el = page.locator(f'[data-index="{i}"]')
        el.click()
        _human_pause(0.05, 0.15)
        el.fill(digit)
        _human_pause(0.1, 0.3)


def _is_auth_success_page(url: str) -> bool:
    """验证码通过后 Auth 常停在 /success，不再跳首页。"""
    text = url or ""
    return "authenticator.cursor.sh/success" in text or (
        "/success" in text and "client_redirect_key=" in text
    )


def _is_login_deep_page(url: str) -> bool:
    """验证码后的第二步：cursor.com/loginDeepPage（All set / Return to Cursor）。"""
    return "loginDeepPage" in (url or "")


def _is_web_app_logged_in(url: str) -> bool:
    """主站已登录。现在验证码后常直接落到 /agents，不再是 dashboard。"""
    text = url or ""
    if _is_login_deep_page(text) or "loginDeepControl" in text:
        return False
    if "authenticator.cursor.sh" in text or "cursor.com" not in text:
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


def _page_body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=1500) or ""
    except Exception:
        return ""


def _is_desktop_continue_step(page: Page) -> bool:
    """验证码后的第一步：Sign in to Cursor / Continue to sign in。"""
    text = _page_body_text(page)
    return (
        "Continue to sign in" in text
        or "complete your sign-in to Cursor desktop" in text
        or "继续登录" in text
    )


def _is_return_to_cursor_step(page: Page) -> bool:
    if _is_login_deep_page(_current_url(page)):
        return True
    text = _page_body_text(page)
    return "All set" in text and (
        "Return to Cursor" in text or "返回 Cursor" in text
    )


def _click_first_matching(page: Page, selectors: tuple[str, ...]) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() <= 0:
                continue
            loc.first.click(timeout=5000)
            return True
        except Exception:
            continue
    return False


_CONTINUE_SIGNIN_SELECTORS = (
    'button:has-text("Continue to sign in")',
    'button:has-text("Continue")',
    'button:has-text("继续登录")',
)
_RETURN_TO_CURSOR_SELECTORS = (
    'button:has-text("Return to Cursor")',
    'button:has-text("返回 Cursor")',
)


def _complete_desktop_signin_steps(page: Page, timeout: int = 60) -> bool:
    """验证码后：有桌面确认就点；已到 /agents 等主站页也算登录成功。"""
    deadline = time.time() + timeout
    clicked_continue = False
    clicked_return = False
    log.info("验证码后等待登录完成（桌面确认或主站 /agents）")

    while time.time() < deadline:
        url = _current_url(page)
        if _is_web_app_logged_in(url) or _peek_session_token(page):
            log.info(f"验证码后已进入已登录主站 url={url[:120]}")
            return True
        if not clicked_continue and _is_desktop_continue_step(page):
            if _click_first_matching(page, _CONTINUE_SIGNIN_SELECTORS):
                log.info("已点击 Continue to sign in")
                clicked_continue = True
                _human_pause(0.8, 1.6)
                continue

        if not clicked_return and _is_return_to_cursor_step(page):
            if _click_first_matching(page, _RETURN_TO_CURSOR_SELECTORS):
                log.info("已点击 Return to Cursor（loginDeepPage）")
                clicked_return = True
                _human_pause(0.8, 1.6)
                return True

        if clicked_continue and clicked_return:
            return True
        time.sleep(0.4)

    log.warning(
        f"桌面登录确认未完成 continue={clicked_continue} return={clicked_return} "
        f"url={_current_url(page)[:160]}"
    )
    return False


def _peek_session_token(page: Page) -> str:
    """读一次 WorkosCursorSessionToken，没有则返回空串。"""
    try:
        cookies = page.context.cookies()
    except Exception:
        return ""
    for cookie in cookies:
        if cookie.get("name") == "WorkosCursorSessionToken":
            return str(cookie.get("value") or "")
    return ""


def _wait_logged_in(page: Page, timeout: int = 60) -> bool:
    """主站已登录或 cookie 已有 session。loginDeepPage / 桌面确认页不算完成。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        url = _current_url(page)
        if _is_login_deep_page(url) or "loginDeepControl" in url:
            time.sleep(0.5)
            continue
        if _is_auth_success_page(url):
            return True
        if _is_web_app_logged_in(url):
            return True
        if "cursor.com" in url and "authenticator" not in url:
            return True
        if _peek_session_token(page):
            return True
        time.sleep(0.5)
    return False


def _ensure_session_cookie(page: Page) -> None:
    """成功页若还没有主站 cookie，主动打开 cursor.com 让 session 落到可采集域。"""
    if _peek_session_token(page):
        return
    log.info("认证已成功但未见 session cookie，打开 cursor.com 承接登录态")
    try:
        page.goto("https://cursor.com/", wait_until="domcontentloaded", timeout=30000)
        _human_pause(1.0, 2.0)
    except Exception as exc:
        log.warning(f"打开 cursor.com 承接 session 失败: {exc}")


def _is_blocked(page: Page) -> bool:
    try:
        body = page.locator("body").inner_text(timeout=2000)
        for kw in ["访问被阻止", "Access denied", "Access blocked"]:
            if kw in body:
                return True
    except Exception:
        pass
    return False


def _extract_session_token(page: Page, timeout: int = 8) -> tuple[str, str]:
    """短等 cookie；/success 页通常不会写入 WorkosCursorSessionToken。"""
    log.info("从浏览器 cookie 读取 WorkosCursorSessionToken ...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = _peek_session_token(page)
        if value:
            head = value.split("%3A%3A")[0][:8] if "%3A%3A" in value else "?"
            log.info(f"拿到 session token（len={len(value)}, user~{head}...）")
            return value, ""
        time.sleep(0.5)
    raise BrowserLoginError("登录后未在 cookie 中找到 WorkosCursorSessionToken")


_DEEP_CONTROL_READY_SELECTORS = (
    "text=You're currently logged in as:",
    "text=You are currently logged in as",
    "text=当前登录",
    'button:has-text("Yes, Log In")',
    'button:has-text("Yes, log me in")',
    'button:has-text("Yes")',
)


def _diagnose_deep_control(page: Page) -> str:
    """失败时留下 URL / 按钮文案，方便对照线上日志。"""
    try:
        info = page.evaluate(
            """() => ({
                url: location.href,
                title: document.title,
                buttons: Array.from(document.querySelectorAll("button"))
                    .map((btn) => (btn.innerText || "").trim())
                    .filter(Boolean)
                    .slice(0, 8),
                hint: ((document.body && document.body.innerText) || "").slice(0, 180),
            })"""
        )
        return (
            f"url={info.get('url')} title={info.get('title')} "
            f"buttons={info.get('buttons')} hint={info.get('hint')!r}"
        )
    except Exception as exc:
        return f"url={getattr(page, 'url', '?')} diagnose_failed={exc}"


def _wait_deep_control_ready(page: Page, timeout_ms: int = 20000) -> bool:
    """等 Deep Control 出现已登录提示或确认按钮，避免 goto 后立刻空点。"""
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        remaining_ms = max(400, int((deadline - time.time()) * 1000))
        slice_ms = min(4000, remaining_ms)
        for sel in _DEEP_CONTROL_READY_SELECTORS:
            try:
                page.wait_for_selector(sel, timeout=slice_ms)
                log.info(f"loginDeepControl 页已就绪: {sel}")
                return True
            except Exception:
                continue
        try:
            ready = page.evaluate(
                """() => {
                    const text = (document.body && document.body.innerText) || "";
                    if (/logged in as|当前登录/i.test(text)) return true;
                    return document.querySelectorAll(".min-h-screen").length >= 2;
                }"""
            )
            if ready:
                log.info("loginDeepControl 页已就绪（DOM）")
                return True
        except Exception:
            pass
    log.warning(f"loginDeepControl 页等待就绪超时: {_diagnose_deep_control(page)}")
    return False


def _click_deep_control_confirm(page: Page) -> None:
    """对齐可用实现：先点 .min-h-screen/.gap-4 第二个 button，再兜底文案。"""
    try:
        clicked = page.evaluate(
            """() => {
                try {
                    const button = document.querySelectorAll(".min-h-screen")[1]
                        .querySelectorAll(".gap-4")[1]
                        .querySelectorAll("button")[1];
                    if (button) { button.click(); return "layout"; }
                } catch (e) {}
                const buttons = Array.from(document.querySelectorAll("button"));
                const target = buttons.find((btn) => {
                    const text = (btn.innerText || "").toLowerCase();
                    return (
                        text.includes("yes")
                        || text.includes("log in")
                        || text.includes("登录")
                        || text.includes("确认")
                    );
                });
                if (target) { target.click(); return "text"; }
                return "";
            }"""
        )
        if clicked:
            log.info(f"已通过脚本点击 loginDeepControl 确认按钮（{clicked}）")
            return
    except Exception as exc:
        log.warning(f"loginDeepControl 脚本点击失败: {exc}")

    selectors = (
        'button:has-text("Yes, Log In")',
        'button:has-text("Yes, log me in")',
        'button:has-text("Yes")',
        'button:has-text("登录")',
        'button:has-text("确认")',
    )
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() <= 0:
                continue
            loc.first.click(timeout=5000)
            log.info(f"已点击 loginDeepControl 确认按钮: {sel}")
            return
        except Exception:
            continue
    raise BrowserLoginError(
        f"未找到 loginDeepControl 确认按钮（{_diagnose_deep_control(page)}）"
    )


def _resolve_session_tokens(page: Page, *, proxy: Optional[str] = None) -> tuple[str, str]:
    """优先读 cookie；成功页没有 cookie 时走 loginDeepControl + auth/poll。"""
    existing = _peek_session_token(page)
    if existing:
        head = existing.split("%3A%3A")[0][:8] if "%3A%3A" in existing else "?"
        log.info(f"拿到 session token（len={len(existing)}, user~{head}...）")
        return existing, ""
    log.info("cookie 无 session，改走 loginDeepControl PKCE 取 token")

    params = _generate_pkce()
    deep_url = (
        f"{CURSOR_LOGIN_DEEP_CONTROL}"
        f"?challenge={params['n']}&uuid={params['r']}&mode=login"
    )
    try:
        page.goto(deep_url, wait_until="domcontentloaded", timeout=45000)
        _human_pause(1.5, 2.5)
        _wait_deep_control_ready(page)
        try:
            _click_deep_control_confirm(page)
        except BrowserLoginError as exc:
            # 对齐可用实现：点不到确认按钮也不中断，仍去 auth/poll
            log.warning(f"确认按钮未点到，仍继续 auth/poll: {exc}")
        _human_pause(0.8, 1.5)
    except BrowserLoginError:
        raise
    except Exception as exc:
        raise BrowserLoginError(f"打开 loginDeepControl 失败: {exc}") from exc

    access, refresh = _poll_token(params["r"], params["s"], proxy=proxy)
    log.info("auth/poll 已拿到 access/refresh token")
    return access, refresh


# ═══════════════════════════════════════════════════════════════════════
# 对外 API
# ═══════════════════════════════════════════════════════════════════════

def login(
    email_addr: str,
    imap_password: str,
    *,
    imap_host: Optional[str] = None,
    imap_port: Optional[int] = None,
    headless: Optional[bool] = None,
    proxy: Optional[str] = None,
    force_fresh: bool = False,
) -> tuple[str, str]:
    """
    执行一次完整浏览器登录，返回 (access_token, refresh_token)。

    失败抛 BrowserLoginError。全局 Semaphore 串行，防资源爆炸。
    """
    headless = SETTINGS.headless if headless is None else headless
    # 桌面平台默认强制有痕登录（更容易通过 Turnstile，也便于人工观察）
    # 若确实要无头登录，可显式设置 BROWSER_LOGIN_FORCE_HEADED=false。
    force_headed = os.environ.get("BROWSER_LOGIN_FORCE_HEADED", "true").strip().lower() in ("1", "true", "yes", "on")
    if force_headed and _requires_system_chrome() and headless:
        log.warning("检测到桌面平台 HEADLESS=true，登录阶段强制切换为有痕模式")
        headless = False
    proxy = proxy if proxy is not None else SETTINGS.proxy

    with _LOGIN_SEMAPHORE:
        log.info(f"════ 登录 {email_addr} ════")
        start = time.time()
        if force_fresh:
            log.info("强制刷新登录态：清理旧浏览器 profile，跳过旧 session 快路径")
            _clear_user_data(email_addr)
        with sync_playwright() as pw:
            context, page = _launch(pw, headless=headless, proxy=proxy, email_addr=email_addr)
            try:
                since_ts = time.time() - 10

                # 快路径：profile 里可能已有有效 WorkosCursorSessionToken
                # 先访问 cursor.com 让 cookie domain 生效，再读 cookie
                if not force_fresh:
                    try:
                        log.info("检查 profile 里是否已有 session cookie ...")
                        page.goto("https://cursor.com/", wait_until="domcontentloaded", timeout=45000)
                        _human_pause(1.0, 2.0)
                        for c in page.context.cookies():
                            if c.get("name") == "WorkosCursorSessionToken" and c.get("value"):
                                value = c["value"]
                                head = value.split("%3A%3A")[0][:8] if "%3A%3A" in value else "?"
                                log.info(f"profile 已含有效 session（len={len(value)}, user~{head}...），跳过邮箱登录")
                                return value, ""
                        log.info("profile 无 session cookie，走邮箱验证码登录流程")
                    except Exception as e:
                        log.info(f"快路径检查失败（{e}），走常规登录")

                _goto_login_page(page)
                if _is_blocked(page):
                    raise BrowserLoginError("登录页被阻止（Cloudflare）")

                _submit_email_with_retry(page, email_addr)
                if _is_blocked(page):
                    raise BrowserLoginError("提交邮箱后被阻止")

                _click_magic_code_button(page)
                if _is_blocked(page):
                    raise BrowserLoginError("点邮箱验证码按钮后被阻止")

                code = email_client.fetch_verification_code(
                    email_addr, imap_password,
                    host=imap_host, port=imap_port,
                    since_ts=since_ts,
                )

                _fill_verification_code(page, code)

                if not _complete_desktop_signin_steps(page, timeout=60):
                    raise BrowserLoginError(
                        f"验证码后未进入已登录态 url={_current_url(page)[:160]}"
                    )
                log.info(f"{email_addr} 登录成功（{time.time()-start:.1f}s）")

                access, refresh = _resolve_session_tokens(page, proxy=proxy)
                log.info(f"{email_addr} 获取 token 成功")
                return access, refresh
            finally:
                try: context.close()
                except Exception: pass
