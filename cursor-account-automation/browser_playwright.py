# browser_playwright.py - Playwright 浏览器引擎（stealth 反检测 + 住宅代理）
#
# 对标 browser.py（DrissionPage），提供 Playwright 替代方案。
# 核心优势：
#   1. 内置 40+ 项自动化指纹修补（比 CDP 手动 patch 更全面）
#   2. 原生支持带认证的代理（无需 proxy_helper 中转）
#   3. 更真实的 TLS/HTTP2 指纹（Chromium 原生行为）

from __future__ import annotations

import os
import random
import time

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page, Playwright


STEALTH_JS = """\
(() => {
  const randomInt = (min, max) =>
    Math.floor(Math.random() * (max - min + 1)) + min;

  // 1. navigator.webdriver → undefined（不是 false）
  Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined, configurable: true,
  });

  // 2. chrome.runtime 完整模拟（含 loadTimes / csi）
  if (!window.chrome) window.chrome = {};
  if (!window.chrome.runtime) {
    window.chrome.runtime = {
      connect: function() {},
      sendMessage: function() {},
      onMessage: { addListener: function() {} },
      onConnect: { addListener: function() {} },
      id: undefined,
    };
  }
  if (!window.chrome.loadTimes) {
    window.chrome.loadTimes = function() {
      return {
        commitLoadTime: Date.now() / 1000 - Math.random() * 2,
        connectionInfo: 'h2',
        finishDocumentLoadTime: Date.now() / 1000 - Math.random(),
        finishLoadTime: Date.now() / 1000 - Math.random() * 0.5,
        firstPaintAfterLoadTime: 0,
        firstPaintTime: Date.now() / 1000 - Math.random() * 1.5,
        navigationType: 'Other',
        npnNegotiatedProtocol: 'h2',
        requestTime: Date.now() / 1000 - Math.random() * 3,
        startLoadTime: Date.now() / 1000 - Math.random() * 2.5,
        wasAlternateProtocolAvailable: false,
        wasFetchedViaSpdy: true,
        wasNpnNegotiated: true,
      };
    };
  }
  if (!window.chrome.csi) {
    window.chrome.csi = function() {
      return {
        onloadT: Date.now(),
        pageT: Math.random() * 5000 + 1000,
        startE: Date.now() - Math.random() * 5000,
        tran: 15,
      };
    };
  }

  // 3. CDP MouseEvent screenX/screenY 坐标修复
  const patchProp = (proto, prop, min, max) => {
    const orig = Object.getOwnPropertyDescriptor(proto, prop);
    Object.defineProperty(proto, prop, {
      configurable: true, enumerable: true,
      get: function () {
        const real = orig && orig.get ? orig.get.call(this) : 0;
        return real === 0 ? randomInt(min, max) : real;
      },
    });
  };
  patchProp(MouseEvent.prototype, 'screenX', 300, 1800);
  patchProp(MouseEvent.prototype, 'screenY', 200, 900);
  patchProp(PointerEvent.prototype, 'screenX', 300, 1800);
  patchProp(PointerEvent.prototype, 'screenY', 200, 900);

  // 4. navigator.plugins / mimeTypes
  try {
    if (navigator.plugins.length === 0) {
      Object.defineProperty(navigator, 'plugins', {
        get: () => {
          const arr = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
            { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
          ];
          arr.refresh = () => {};
          return arr;
        },
        configurable: true,
      });
      Object.defineProperty(navigator, 'mimeTypes', {
        get: () => {
          const arr = [
            { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format',
              enabledPlugin: { name: 'Chrome PDF Plugin' } },
            { type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format',
              enabledPlugin: { name: 'Chrome PDF Viewer' } },
          ];
          arr.refresh = () => {};
          return arr;
        },
        configurable: true,
      });
    }
  } catch(e) {}

  // 5. navigator.languages
  try {
    if (!navigator.languages || navigator.languages.length === 0) {
      Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'], configurable: true,
      });
    }
  } catch(e) {}

  // 6. window dimensions (headless 修复)
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

  // 7. screen 属性
  try {
    if (screen.availHeight === 0 || screen.height === 0) {
      const dims = { availHeight: 900, height: 1080, availWidth: 1920, width: 1920, colorDepth: 24, pixelDepth: 24 };
      for (const [k, v] of Object.entries(dims)) {
        Object.defineProperty(screen, k, { get: () => v, configurable: true });
      }
    }
  } catch(e) {}

  // 8. Notification.permission
  try {
    if (Notification.permission === 'denied') {
      Object.defineProperty(Notification, 'permission', {
        get: () => 'default', configurable: true,
      });
    }
  } catch(e) {}

  // 9. Permissions API
  try {
    const origQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (desc) => {
      if (desc.name === 'notifications') {
        return Promise.resolve({ state: 'prompt', onchange: null });
      }
      return origQuery(desc);
    };
  } catch(e) {}

  // 10. console.debug 原生签名
  try {
    const _debug = console.debug;
    console.debug = function() { return _debug.apply(this, arguments); };
    Object.defineProperty(console.debug, 'toString', {
      value: () => 'function debug() { [native code] }',
    });
  } catch(e) {}

  // 11. 清理 CDP / Selenium 全局变量
  try {
    for (const key of Object.getOwnPropertyNames(window)) {
      if (/^cdc_|^__selenium|^__webdriver|^__driver|^__playwright/.test(key)) {
        try { delete window[key]; } catch(e) {}
      }
    }
  } catch(e) {}

  // 12. Error.stack 过滤自动化痕迹
  try {
    const origStack = Object.getOwnPropertyDescriptor(Error.prototype, 'stack');
    if (origStack && origStack.get) {
      Object.defineProperty(Error.prototype, 'stack', {
        configurable: true, enumerable: false,
        get: function() {
          const stack = origStack.get.call(this);
          if (typeof stack !== 'string') return stack;
          return stack.split('\\n').filter(line =>
            !line.includes('__puppeteer') &&
            !line.includes('__playwright') &&
            !line.includes('pptr:') &&
            !line.includes('DevToolsAPI') &&
            !line.includes('injectedScript')
          ).join('\\n');
        },
      });
    }
  } catch(e) {}

  // 13. navigator.connection.rtt
  try {
    if (navigator.connection && navigator.connection.rtt === 0) {
      Object.defineProperty(navigator.connection, 'rtt', {
        get: () => randomInt(50, 150), configurable: true,
      });
    }
  } catch(e) {}

  // 14. performance.memory
  try {
    if (performance.memory && performance.memory.jsHeapSizeLimit === 0) {
      Object.defineProperty(performance, 'memory', {
        get: () => ({
          totalJSHeapSize: 35000000 + randomInt(0, 5000000),
          usedJSHeapSize: 25000000 + randomInt(0, 3000000),
          jsHeapSizeLimit: 2172649472,
        }),
        configurable: true,
      });
    }
  } catch(e) {}

  // 15. WebGL vendor/renderer spoofing
  try {
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
      if (param === 37445) return 'Google Inc. (Intel)';
      if (param === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.1)';
      return getParam.call(this, param);
    };
    const getParam2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(param) {
      if (param === 37445) return 'Google Inc. (Intel)';
      if (param === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.1)';
      return getParam2.call(this, param);
    };
  } catch(e) {}

  // 16. 被动 sitekey 捕获（供 2Captcha/CapSolver 使用）
  try {
    window.__capturedTurnstile = { sitekey: '', source: '', iframeSrc: '' };
    const extractSitekey = (src) => {
      if (!src) return '';
      try {
        const u = new URL(src);
        return u.searchParams.get('k') || u.searchParams.get('sitekey') || '';
      } catch(e) {}
      const m = src.match(/\\/([0-9x][A-Za-z0-9_-]{15,})(?:\\/|$|\\?)/);
      return m ? m[1] : '';
    };
    const scan = () => {
      if (window.__capturedTurnstile.sitekey) return;
      for (const f of document.querySelectorAll('iframe')) {
        if (!f.src || !f.src.includes('challenge')) continue;
        window.__capturedTurnstile.iframeSrc = f.src.substring(0, 200);
        const key = extractSitekey(f.src);
        if (key) { window.__capturedTurnstile.sitekey = key; window.__capturedTurnstile.source = 'iframe-observer'; return; }
      }
      for (const el of document.querySelectorAll('[data-sitekey]')) {
        const key = el.getAttribute('data-sitekey');
        if (key && key.length > 10) { window.__capturedTurnstile.sitekey = key; window.__capturedTurnstile.source = 'data-attr-observer'; return; }
      }
    };
    new MutationObserver(() => { try { scan(); } catch(e) {} })
      .observe(document.documentElement || document, { childList: true, subtree: true, attributes: true });
    document.addEventListener('DOMContentLoaded', scan, { once: true });
    setTimeout(scan, 3000);
    setTimeout(scan, 8000);
  } catch(e) {}
})();
"""


def _parse_proxy(proxy: str) -> dict | None:
    """解析代理 URL 为 Playwright 格式。
    支持 http://user:pass@host:port 和 http://host:port
    """
    if not proxy or not proxy.strip():
        return None

    import re
    proxy = proxy.strip()

    m = re.match(r"(https?)://([^:]+):([^@]+)@([^:/\s]+):(\d+)", proxy)
    if m:
        scheme, user, password, host, port = m.groups()
        return {
            "server": f"{scheme}://{host}:{port}",
            "username": user,
            "password": password,
        }

    m = re.match(r"(https?)://([^:/\s]+):(\d+)", proxy)
    if m:
        scheme, host, port = m.groups()
        return {"server": f"{scheme}://{host}:{port}"}

    return None


def create_playwright_browser(
    *,
    headless: bool = False,
    proxy: str | None = None,
) -> tuple[Playwright, Browser, BrowserContext, Page]:
    """创建 Playwright 浏览器实例。

    Returns: (playwright_instance, browser, context, page)
    """
    pw = sync_playwright().start()

    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-infobars",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-ipc-flooding-protection",
        "--password-store=basic",
        "--use-mock-keychain",
    ]

    proxy_config = _parse_proxy(proxy) if proxy else None

    browser = pw.chromium.launch(
        headless=headless,
        args=launch_args,
        proxy=proxy_config,
    )

    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        screen={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        color_scheme="light",
        java_script_enabled=True,
        has_touch=False,
        is_mobile=False,
        device_scale_factor=1,
        permissions=["geolocation"],
    )

    context.add_init_script(STEALTH_JS)

    page = context.new_page()

    if proxy_config:
        print(f"[Playwright] 代理: {proxy_config['server']}")
    print(f"[Playwright] 浏览器已启动 (headless={headless})")
    return pw, browser, context, page


def warmup_browser(page: Page) -> None:
    """访问正常网站积累信任分 + 产生行为信号。"""
    urls = ["https://www.google.com", "https://github.com", "https://www.wikipedia.org"]
    url = random.choice(urls)
    print(f"[预热] 访问 {url} ...")
    try:
        page.goto(url, timeout=15000, wait_until="domcontentloaded")
        time.sleep(random.uniform(2, 4))
        simulate_human_behavior(page)
        time.sleep(random.uniform(1, 2))
    except Exception as e:
        print(f"[预热] 访问失败（不影响后续）: {e}")
    print("[预热] 完成")


def simulate_human_behavior(page: Page) -> None:
    """模拟真人鼠标移动 + 滚动。"""
    try:
        x, y = random.randint(200, 800), random.randint(150, 500)
        page.mouse.move(x, y, steps=random.randint(10, 25))
        time.sleep(random.uniform(0.2, 0.5))

        for _ in range(random.randint(2, 4)):
            dx = random.randint(-150, 150)
            dy = random.randint(-100, 100)
            nx, ny = max(10, x + dx), max(10, y + dy)
            nx, ny = min(1900, nx), min(1060, ny)
            page.mouse.move(nx, ny, steps=random.randint(8, 20))
            x, y = nx, ny
            time.sleep(random.uniform(0.1, 0.4))

        page.mouse.wheel(0, random.randint(50, 200))
        time.sleep(random.uniform(0.3, 0.8))
        page.mouse.wheel(0, -random.randint(20, 80))
        time.sleep(random.uniform(0.2, 0.5))
    except Exception:
        pass


def simulate_typing(page: Page, selector: str, text: str) -> None:
    """模拟真人打字：先点击，再逐字输入（随机间隔）。"""
    page.click(selector)
    time.sleep(random.uniform(0.2, 0.5))
    for ch in text:
        page.keyboard.type(ch, delay=random.randint(50, 150))
        if random.random() < 0.1:
            time.sleep(random.uniform(0.2, 0.5))


def close_playwright(pw: Playwright, browser: Browser) -> None:
    try:
        browser.close()
    except Exception:
        pass
    try:
        pw.stop()
    except Exception:
        pass
