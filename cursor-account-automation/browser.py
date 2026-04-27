# browser.py - DrissionPage 浏览器管理
# 参考 wf-cursor-auto-free 的 browser_utils.py

from __future__ import annotations

import os
import random
import sys
import time

from DrissionPage import ChromiumOptions, Chromium

_EXTENSION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "turnstilePatch")


def _resolve_proxy(proxy: str | None) -> str:
    """若代理带认证，启动 pproxy 本地转发，返回浏览器可用的代理 URL。"""
    if not proxy or not proxy.strip():
        return ""
    from proxy_helper import start_proxy_tunnel
    return start_proxy_tunnel(proxy)


def _get_chrome_path() -> str | None:
    if sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    elif sys.platform == "win32":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    else:
        candidates = [
            "/usr/bin/google-chrome-stable",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
        ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def create_browser(
    *,
    headless: bool = False,
    proxy: str | None = None,
    user_agent: str | None = None,
) -> tuple:
    """创建浏览器实例，返回 (browser, tab)。

    关键：
      - 使用 Chromium（不是 ChromiumPage）+ latest_tab
      - 加载 turnstilePatch 扩展修复 CDP 鼠标事件坐标
      - credentials_enable_service=False 避免密码保存弹窗
    """
    co = ChromiumOptions()

    chrome_path = _get_chrome_path()
    if chrome_path:
        co.set_browser_path(chrome_path)

    # 加载 turnstilePatch 扩展
    if os.path.isdir(_EXTENSION_DIR):
        co.add_extension(_EXTENSION_DIR)
        print("[浏览器] 已加载 turnstilePatch 扩展")
    else:
        print(f"[浏览器] 警告：turnstilePatch 目录不存在: {_EXTENSION_DIR}")

    co.set_pref("credentials_enable_service", False)
    co.set_argument("--hide-crash-restore-bubble")

    if sys.platform == "darwin":
        co.set_argument("--no-sandbox")
        co.set_argument("--disable-gpu")

    if proxy:
        resolved = _resolve_proxy(proxy)
        if resolved:
            co.set_proxy(resolved)

    if user_agent:
        co.set_user_agent(user_agent)

    co.auto_port()
    co.headless(headless)

    browser = Chromium(co)
    tab = browser.latest_tab

    # 替换 HeadlessChrome 标识
    actual_ua = tab.run_js("return navigator.userAgent")
    if "HeadlessChrome" in (actual_ua or ""):
        new_ua = actual_ua.replace("HeadlessChrome", "Chrome")
        co.set_user_agent(new_ua)

    # 注入 turnstile patch：修复 CDP MouseEvent screenX/screenY 为 0 的问题
    # 扩展 content_scripts 在 DrissionPage 下不可靠，改用 CDP 注入
    _inject_turnstile_patch(tab)

    print(f"[浏览器] DrissionPage Chromium 已启动")
    return browser, tab


_TURNSTILE_PATCH_JS = """\
(function () {
  const randomInt = (min, max) =>
    Math.floor(Math.random() * (max - min + 1)) + min;

  // 1. 修复 CDP MouseEvent screenX/screenY 为 0（Turnstile 核心检测点）
  const patchProp = (proto, prop, min, max) => {
    const orig = Object.getOwnPropertyDescriptor(proto, prop);
    Object.defineProperty(proto, prop, {
      configurable: true,
      enumerable: true,
      get: function () {
        const real = orig && orig.get ? orig.get.call(this) : 0;
        return real === 0 ? randomInt(min, max) : real;
      },
    });
  };
  patchProp(MouseEvent.prototype, "screenX", 300, 1800);
  patchProp(MouseEvent.prototype, "screenY", 200, 900);
  patchProp(PointerEvent.prototype, "screenX", 300, 1800);
  patchProp(PointerEvent.prototype, "screenY", 200, 900);

  // 2. 隐藏 webdriver 标志
  Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined, configurable: true,
  });

  // 3. 修复 chrome.runtime 存在性检测
  if (!window.chrome) window.chrome = {};
  if (!window.chrome.runtime) {
    window.chrome.runtime = {
      connect: function() {},
      sendMessage: function() {},
    };
  }

  // 4. 修复 Notification.permission
  try {
    if (Notification.permission === 'denied') {
      Object.defineProperty(Notification, 'permission', {
        get: () => 'default', configurable: true,
      });
    }
  } catch(e) {}

  // 5. 修复 navigator.plugins 长度（自动化浏览器通常为 0）
  try {
    if (navigator.plugins.length === 0) {
      Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5], configurable: true,
      });
      Object.defineProperty(navigator, 'mimeTypes', {
        get: () => [1, 2], configurable: true,
      });
    }
  } catch(e) {}

  // 6. 修复 navigator.languages（空数组是自动化特征）
  try {
    if (!navigator.languages || navigator.languages.length === 0) {
      Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'], configurable: true,
      });
    }
  } catch(e) {}

  // 7. 修复 window.outerHeight/outerWidth（headless 时为 0）
  try {
    if (window.outerHeight === 0) {
      Object.defineProperty(window, 'outerHeight', {
        get: () => window.innerHeight + randomInt(70, 120), configurable: true,
      });
    }
    if (window.outerWidth === 0) {
      Object.defineProperty(window, 'outerWidth', {
        get: () => window.innerWidth + randomInt(0, 20), configurable: true,
      });
    }
  } catch(e) {}

  // 8. 修复 screen 属性
  try {
    if (screen.availHeight === 0 || screen.height === 0) {
      Object.defineProperty(screen, 'availHeight', {
        get: () => 900, configurable: true,
      });
      Object.defineProperty(screen, 'height', {
        get: () => 1080, configurable: true,
      });
      Object.defineProperty(screen, 'availWidth', {
        get: () => 1920, configurable: true,
      });
      Object.defineProperty(screen, 'width', {
        get: () => 1920, configurable: true,
      });
    }
  } catch(e) {}

  // 9. 覆盖 Permission API 查询（隐藏 notifications denied 状态）
  try {
    const origQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (desc) => {
      if (desc.name === 'notifications') {
        return Promise.resolve({ state: 'prompt', onchange: null });
      }
      return origQuery(desc);
    };
  } catch(e) {}

  // 10. 劫持 console.debug（CDP 连接后 console.debug 行为会改变，Turnstile 检测此特征）
  try {
    const _debug = console.debug;
    const _log = console.log;
    console.debug = function() { return _debug.apply(this, arguments); };
    Object.defineProperty(console.debug, 'toString', {
      value: () => 'function debug() { [native code] }',
    });
  } catch(e) {}

  // 11. 清理 ChromeDriver / CDP 注入的全局变量
  try {
    for (const key of Object.getOwnPropertyNames(window)) {
      if (/^cdc_|^__selenium|^__webdriver|^__driver/.test(key)) {
        try { delete window[key]; } catch(e) {}
      }
    }
  } catch(e) {}

  // 12. 修复 Error.stack 中的 CDP 痕迹
  try {
    const origStack = Object.getOwnPropertyDescriptor(Error.prototype, 'stack');
    if (origStack && origStack.get) {
      Object.defineProperty(Error.prototype, 'stack', {
        configurable: true,
        enumerable: false,
        get: function() {
          const stack = origStack.get.call(this);
          if (typeof stack !== 'string') return stack;
          return stack.split('\\n').filter(line =>
            !line.includes('__puppeteer') &&
            !line.includes('pptr:') &&
            !line.includes('DevToolsAPI') &&
            !line.includes('injectedScript')
          ).join('\\n');
        },
      });
    }
  } catch(e) {}

  // 13. 修复 navigator.connection.rtt（CDP 下可能异常）
  try {
    if (navigator.connection && navigator.connection.rtt === 0) {
      Object.defineProperty(navigator.connection, 'rtt', {
        get: () => 50, configurable: true,
      });
    }
  } catch(e) {}

  // 14. 修复 performance.memory（CDP 特征）
  try {
    if (performance.memory) {
      const mem = performance.memory;
      if (mem.jsHeapSizeLimit === 0) {
        Object.defineProperty(performance, 'memory', {
          get: () => ({
            totalJSHeapSize: 35000000,
            usedJSHeapSize: 25000000,
            jsHeapSizeLimit: 2172649472,
          }),
          configurable: true,
        });
      }
    }
  } catch(e) {}

  // 15. 非侵入式 sitekey 捕获：通过 MutationObserver 观测 Turnstile iframe
  // 不修改 window.turnstile，不 hook fetch/XHR，仅被动读取 DOM
  try {
    window.__capturedTurnstile = { sitekey: '', source: '', iframeSrc: '' };
    const extractSitekey = (src) => {
      if (!src) return '';
      try {
        const u = new URL(src);
        const k = u.searchParams.get('k') || u.searchParams.get('sitekey');
        if (k && k.length > 10) return k;
      } catch(e) {}
      const m = src.match(/\\/([0-9x][A-Za-z0-9_-]{15,})(?:\\/|$|\\?)/);
      return m ? m[1] : '';
    };
    const scan = () => {
      if (window.__capturedTurnstile.sitekey) return;
      const iframes = document.querySelectorAll('iframe');
      for (const f of iframes) {
        if (!f.src || !f.src.includes('challenge')) continue;
        window.__capturedTurnstile.iframeSrc = f.src.substring(0, 200);
        const key = extractSitekey(f.src);
        if (key) {
          window.__capturedTurnstile.sitekey = key;
          window.__capturedTurnstile.source = 'iframe-observer';
          return;
        }
      }
      const els = document.querySelectorAll('[data-sitekey]');
      for (const el of els) {
        const key = el.getAttribute('data-sitekey');
        if (key && key.length > 10) {
          window.__capturedTurnstile.sitekey = key;
          window.__capturedTurnstile.source = 'data-attr-observer';
          return;
        }
      }
    };
    const obs = new MutationObserver(() => { try { scan(); } catch(e) {} });
    try {
      obs.observe(document.documentElement || document, { childList: true, subtree: true, attributes: true });
    } catch(e) {}
    document.addEventListener('DOMContentLoaded', scan, { once: true });
    setTimeout(scan, 3000);
    setTimeout(scan, 8000);
  } catch(e) {}
})();
"""


def _inject_turnstile_patch(tab) -> None:
    """通过 CDP 在每个新文档加载前注入 turnstile patch。"""
    try:
        tab.run_cdp(
            "Page.addScriptToEvaluateOnNewDocument",
            source=_TURNSTILE_PATCH_JS,
        )
        print("[浏览器] turnstile patch 已通过 CDP 注入")
    except Exception as e:
        print(f"[浏览器] CDP 注入失败，回退到 run_js: {e}")
        tab.run_js(_TURNSTILE_PATCH_JS)


def warmup_browser(tab) -> None:
    """访问正常网站积累信任分 + 产生鼠标/滚动行为。"""
    urls = ["https://www.google.com", "https://github.com", "https://www.wikipedia.org"]
    url = random.choice(urls)
    print(f"[预热] 访问 {url} ...")
    try:
        tab.get(url)
        time.sleep(random.uniform(2, 4))
        # 模拟真人行为：移动鼠标、滚动
        tab.actions.move(random.randint(100, 500), random.randint(100, 400))
        time.sleep(random.uniform(0.5, 1))
        tab.actions.move(random.randint(-100, 100), random.randint(-80, 80), duration=0.4)
        time.sleep(random.uniform(0.3, 0.8))
        tab.scroll.down(random.randint(100, 300))
        time.sleep(random.uniform(1, 2))
    except Exception as e:
        print(f"[预热] 访问失败（不影响后续）: {e}")
    print("[预热] 完成")


def clear_browser_state(tab) -> None:
    """清空 cookies / localStorage / sessionStorage，获取干净会话。"""
    try:
        tab.run_cdp("Network.clearBrowserCookies")
        tab.run_cdp("Network.clearBrowserCache")
        tab.run_js("""
            try { localStorage.clear(); } catch(e) {}
            try { sessionStorage.clear(); } catch(e) {}
        """)
        print("[浏览器] 已清空 cookies / cache / storage")
    except Exception as e:
        print(f"[浏览器] 清空状态失败: {e}")


def close_browser(browser) -> None:
    try:
        browser.quit()
    except Exception:
        pass
