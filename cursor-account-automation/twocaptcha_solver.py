# twocaptcha_solver.py - 2Captcha Turnstile 求解器
#
# 与 turnstile_solver.py（CapSolver）并行的替代方案。
# 费用：€1.4/千次 ≈ ¥0.011/次
# 注册地址：https://2captcha.com
#
# 支持两种浏览器引擎：
#   - DrissionPage (tab 对象)
#   - Playwright (page 对象)

from __future__ import annotations

import os
import time

from twocaptcha import TwoCaptcha


def _get_api_key() -> str:
    key = os.environ.get("TWOCAPTCHA_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "未配置 TWOCAPTCHA_API_KEY。\n"
            "请在 .env 文件中添加：TWOCAPTCHA_API_KEY=your_api_key\n"
            "注册地址：https://2captcha.com"
        )
    return key


def _create_solver() -> TwoCaptcha:
    return TwoCaptcha(_get_api_key())


# ═══════════════════════════════════════════════════════════════════════
# sitekey 提取
# ═══════════════════════════════════════════════════════════════════════

_SITEKEY_EXTRACT_JS = """() => {
    // 1. observer 捕获
    try {
        const ct = window.__capturedTurnstile;
        if (ct && ct.sitekey && ct.sitekey.length > 10) return ct.sitekey;
    } catch(e) {}
    // 2. data-sitekey 属性
    for (const el of document.querySelectorAll('[data-sitekey]')) {
        const key = el.getAttribute('data-sitekey');
        if (key && key.length > 10) return key;
    }
    // 3. iframe src
    for (const f of document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]')) {
        try {
            const u = new URL(f.src);
            const k = u.searchParams.get('k') || u.searchParams.get('sitekey');
            if (k && k.length > 10) return k;
        } catch(e) {}
    }
    // 4. ___turnstile_cfg
    try {
        const walk = (obj) => {
            if (!obj || typeof obj !== 'object') return null;
            if (obj.sitekey && String(obj.sitekey).length > 10) return String(obj.sitekey);
            for (const v of Object.values(obj)) { const r = walk(v); if (r) return r; }
            return null;
        };
        const r = walk(window.___turnstile_cfg);
        if (r) return r;
    } catch(e) {}
    // 5. 脚本文本
    for (const s of document.scripts || []) {
        const m = (s.textContent || '').match(/sitekey["'\\s:]+(0x4[A-Za-z0-9_-]+)/i);
        if (m && m[1]) return m[1];
    }
    return '';
}"""

_INJECT_TOKEN_JS = """(token) => {
    let injected = false;
    // hidden inputs
    for (const sel of ['input[name="cf-turnstile-response"]', '[name="cf-chl-turnstile-response"]']) {
        const el = document.querySelector(sel);
        if (el) { el.value = token; injected = true; }
    }
    // widget inputs
    for (const w of document.querySelectorAll('[id^="cf-chl-widget"], .cf-turnstile, #cf-turnstile')) {
        for (const inp of w.querySelectorAll('input[type="hidden"]')) {
            inp.value = token; injected = true;
        }
    }
    // JS API override
    try { if (window.turnstile) { window.turnstile.getResponse = () => token; injected = true; } } catch(e) {}
    // callback
    try {
        for (const el of document.querySelectorAll('[data-callback]')) {
            const cb = el.getAttribute('data-callback');
            if (cb && window[cb]) { window[cb](token); injected = true; }
        }
    } catch(e) {}
    // 创建 hidden input 兜底
    if (!injected) {
        const inp = document.createElement('input');
        inp.type = 'hidden'; inp.name = 'cf-turnstile-response'; inp.value = token;
        const form = document.querySelector('form');
        if (form) { form.appendChild(inp); injected = true; }
    }
    return injected;
}"""


def _extract_sitekey_drissionpage(tab) -> str:
    """从 DrissionPage tab 对象提取 sitekey。"""
    try:
        return tab.run_js(_SITEKEY_EXTRACT_JS.replace("() => {", "(function() {").rstrip("}") + "})()") or ""
    except Exception:
        return ""


def _extract_sitekey_playwright(page) -> str:
    """从 Playwright page 对象提取 sitekey。"""
    try:
        return page.evaluate(_SITEKEY_EXTRACT_JS) or ""
    except Exception:
        return ""


def _inject_token_drissionpage(tab, token: str) -> bool:
    """将 token 注入 DrissionPage tab。"""
    try:
        js = _INJECT_TOKEN_JS.replace("(token) => {", f"(function() {{ const token = {repr(token)};")
        js = js.rstrip("}") + "})()"
        result = tab.run_js(js)
        print(f"[2Captcha] token 已注入 (injected={result})")
        return True
    except Exception as e:
        print(f"[2Captcha] token 注入失败: {e}")
        return False


def _inject_token_playwright(page, token: str) -> bool:
    """将 token 注入 Playwright page。"""
    try:
        result = page.evaluate(_INJECT_TOKEN_JS, token)
        print(f"[2Captcha] token 已注入 (injected={result})")
        return True
    except Exception as e:
        print(f"[2Captcha] token 注入失败: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════
# 核心求解
# ═══════════════════════════════════════════════════════════════════════

def solve_turnstile(
    website_url: str,
    sitekey: str,
    timeout: int = 60,
) -> str:
    """调用 2Captcha API 求解 Cloudflare Turnstile，返回 token。"""
    solver = _create_solver()
    solver.defaultTimeout = timeout
    solver.pollingInterval = 3

    print(f"[2Captcha] 求解中... sitekey={sitekey[:24]}..., url={website_url[:60]}")

    try:
        result = solver.turnstile(
            sitekey=sitekey,
            url=website_url,
        )
        token = result.get("code", "") if isinstance(result, dict) else str(result)
        if token:
            print(f"[2Captcha] 求解成功, token len={len(token)}")
            return token
        raise RuntimeError(f"2Captcha 返回空 token: {result}")
    except Exception as e:
        raise RuntimeError(f"2Captcha 求解失败: {e}") from e


# ═══════════════════════════════════════════════════════════════════════
# 完整流程（提取 sitekey → 求解 → 注入）
# ═══════════════════════════════════════════════════════════════════════

def solve_and_inject_turnstile(page_or_tab, timeout: int = 60) -> bool:
    """自动检测浏览器类型，提取 sitekey，求解并注入。

    支持 DrissionPage tab 和 Playwright page 对象。
    """
    is_playwright = hasattr(page_or_tab, "evaluate")

    if is_playwright:
        url = page_or_tab.url
        sitekey = _extract_sitekey_playwright(page_or_tab)
    else:
        url = page_or_tab.url or ""
        sitekey = _extract_sitekey_drissionpage(page_or_tab)

    sitekey = (sitekey or "").strip()

    if not sitekey:
        print("[2Captcha] 未能提取 sitekey，跳过求解")
        return False

    try:
        token = solve_turnstile(url, sitekey, timeout=timeout)
    except RuntimeError as e:
        print(f"[2Captcha] 求解失败: {e}")
        return False

    if is_playwright:
        return _inject_token_playwright(page_or_tab, token)
    else:
        return _inject_token_drissionpage(page_or_tab, token)
