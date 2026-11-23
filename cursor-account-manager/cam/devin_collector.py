"""Devin 登录 + 用量/套餐采集（浏览器）。

流程（均已用 app.devin.ai 公开页面核对过的入口/选择器）：
1. 复用账号专属 Chrome profile（``devin__<email>``，与 Cursor profile 隔离）。
2. 打开 ``/settings/usage``：已登录直接进入（Cookie 快路径，不收验证码）；
   未登录会落到登录页（``#auth1-email`` + "Log in" 按钮）。
3. 邮箱登录：填邮箱 → Log in → "Verify your identity" 六位验证码 → 从 IMAP 取码 → 输入。
4. 登录后监听页面自己的 ``api.devin.ai`` JSON 响应 + 读取可见文本，解析套餐/用量。
5. 失败时落截图 + 页面文本到 ``data/artifacts/devin/``，错误里带路径，方便远程排查。
"""

from __future__ import annotations

import base64
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from patchright.sync_api import Page, sync_playwright

from . import browser_login, email_client
from .devin_config import (
    DEVIN_API_HOST,
    DEVIN_INVOICES_URL,
    DEVIN_PLANS_URL,
    DEVIN_USAGE_URL,
    devin_settings,
)
from .devin_errors import DevinError, classify_exception
from .devin_parser import (
    cycle_from_invoices,
    extract_from_api,
    extract_invoices_from_api,
    has_core_data,
    merge_invoices,
    missing_fields,
    merge_results,
    parse_invoices_text,
    parse_usage_text,
    stripe_pdf_url,
)
from .logger import get

log = get("devin")

_EMAIL_INPUT = "#auth1-email"
_CODE_INPUT_SELECTORS = (
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"]',
    'input[name="code"]',
    'input[data-index="0"]',
)
_VERIFY_TEXT = re.compile(r"verify your identity|verification code|enter the code|check your email", re.I)
_BAD_CODE_TEXT = re.compile(r"invalid (?:or expired )?code|incorrect code|code (?:is )?(?:invalid|expired|incorrect)|try again", re.I)
_NOT_FOUND_TEXT = re.compile(r"no account|not found|doesn'?t exist|does not exist|not registered|suspended|disabled|blocked|banned", re.I)

_SEMAPHORE_LOCK = threading.Lock()
_semaphore: threading.Semaphore | None = None
_semaphore_size = 0


def _get_semaphore() -> threading.Semaphore:
    """按 DEVIN_CONCURRENCY 建信号量；配置变化时重建（测试/热改配置）。"""
    global _semaphore, _semaphore_size
    size = devin_settings().concurrency
    with _SEMAPHORE_LOCK:
        if _semaphore is None or size != _semaphore_size:
            _semaphore = threading.Semaphore(size)
            _semaphore_size = size
        return _semaphore


@dataclass
class CollectResult:
    email: str
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    login_used: bool = False
    duration_ms: int = 0


# ─── 页面辅助 ─────────────────────────────────────────────────────

def _body_text(page: Page) -> str:
    try:
        return page.inner_text("body", timeout=5000)
    except Exception:
        return ""


def _is_login_page(page: Page) -> bool:
    try:
        return page.locator(_EMAIL_INPUT).count() > 0 and page.locator(_EMAIL_INPUT).first.is_visible()
    except Exception:
        return False


def _is_code_step(page: Page) -> bool:
    text = _body_text(page)
    if _VERIFY_TEXT.search(text):
        return True
    for sel in _CODE_INPUT_SELECTORS:
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


def _is_logged_in(page: Page) -> bool:
    url = page.url or ""
    if "app.devin.ai" not in url or "/login" in url or "/auth" in url:
        return False
    if _is_login_page(page) or _is_code_step(page):
        return False
    text = _body_text(page).strip()
    # 登录页文本以 "Log in" 开头；已登录页含侧边栏/设置内容
    return bool(text) and not text.lower().startswith("log in")


def _wait_until(predicate, timeout: float, interval: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _save_artifacts(page: Page | None, email: str, tag: str) -> str:
    """失败现场：截图 + 页面文本，返回目录说明（拼进错误信息）。"""
    if page is None:
        return ""
    try:
        base = devin_settings().artifacts_dir
        base.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._@-]+", "_", email)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        stem = base / f"{safe}-{tag}-{stamp}"
        try:
            page.screenshot(path=str(stem) + ".png", full_page=True, timeout=8000)
        except Exception:
            pass
        text = _body_text(page)
        Path(str(stem) + ".txt").write_text(f"url={page.url}\n\n{text}", encoding="utf-8")
        return f" [现场: {stem}.png/.txt]"
    except Exception as exc:  # pragma: no cover - 兜底
        log.debug(f"保存现场失败: {exc}")
        return ""


# ─── 登录 ─────────────────────────────────────────────────────────

def _fill_code(page: Page, code: str) -> None:
    """6 位验证码：OTP 组件通常自动跳格，统一聚焦首格后逐位键入。"""
    target = None
    for sel in _CODE_INPUT_SELECTORS:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                target = loc.first
                break
        except Exception:
            continue
    if target is None:
        # 兜底：第一个可见且不是邮箱的 input
        for loc in page.locator("input:visible").all():
            try:
                if (loc.get_attribute("id") or "") != "auth1-email":
                    target = loc
                    break
            except Exception:
                continue
    if target is None:
        raise DevinError("login_failed", "验证码页未找到输入框")
    target.click()
    browser_login._human_pause(0.2, 0.5)
    for ch in code:
        page.keyboard.type(ch, delay=60)
    browser_login._human_pause(0.4, 0.9)
    # 某些实现需要手动点确认
    for name in ("Verify", "Continue", "Confirm", "Submit"):
        try:
            btn = page.get_by_role("button", name=re.compile(rf"^{name}$", re.I))
            if btn.count() > 0 and btn.first.is_enabled():
                btn.first.click()
                break
        except Exception:
            continue


def _login_with_email_code(page: Page, account: dict[str, Any]) -> None:
    email = account["email"]
    cfg = devin_settings()
    page.locator(_EMAIL_INPUT).first.click()
    browser_login._human_pause(0.2, 0.5)
    page.locator(_EMAIL_INPUT).first.fill("")
    for ch in email:
        page.keyboard.type(ch, delay=50)
    requested_at = time.time() - 5
    browser_login._human_pause(0.3, 0.8)
    page.get_by_role("button", name=re.compile(r"^Log in$", re.I)).first.click()

    # 等验证码步骤或明确的错误
    def step_ready() -> bool:
        return _is_code_step(page) or _is_logged_in(page)

    if not _wait_until(step_ready, 30):
        hint = _save_artifacts(page, email, "no-code-step")
        text = _body_text(page)
        if _NOT_FOUND_TEXT.search(text):
            raise DevinError("account_not_found", f"提交邮箱后页面提示异常{hint}")
        raise DevinError("login_failed", f"提交邮箱后未进入验证码步骤{hint}")
    if _is_logged_in(page):
        return

    last_error = ""
    for attempt in (1, 2):
        code = email_client.fetch_verification_code(
            email,
            account["imap_password"],
            host=account.get("imap_host") or None,
            port=account.get("imap_port") or None,
            timeout_sec=cfg.code_timeout_sec,
            since_ts=requested_at,
            sender_keywords=cfg.sender_keywords,
            extra_search_queries=tuple(
                [f'(FROM "{k}")' for k in cfg.sender_keywords]
                + [f'(HEADER Subject "{k}")' for k in cfg.sender_keywords]
                + ["(UNSEEN)"]
            ),
            product_label="Devin",
        )
        _fill_code(page, code)
        ok = _wait_until(lambda: _is_logged_in(page) or bool(_BAD_CODE_TEXT.search(_body_text(page))), 40, 1.5)
        if ok and _is_logged_in(page):
            return
        last_error = "验证码被拒绝" if ok else "输入验证码后未进入应用"
        # 再试一次：点 Resend（若有）并重新取更新的验证码
        requested_at = time.time() - 5
        try:
            resend = page.get_by_role("button", name=re.compile(r"resend", re.I))
            if resend.count() > 0:
                resend.first.click()
        except Exception:
            pass
    hint = _save_artifacts(page, email, "code-rejected")
    raise DevinError("code_invalid" if "拒绝" in last_error else "login_failed", f"{last_error}{hint}")


# ─── 采集 ─────────────────────────────────────────────────────────

def _read_api_payloads(responses: list[Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for resp in responses:
        try:
            url = resp.url
            if url in seen:
                continue
            body = resp.json()
        except Exception:
            continue
        seen.add(url)
        payloads.append({"url": url, "status": getattr(resp, "status", 0), "body": body})
    return payloads


def _wait_usage_rendered(page: Page, timeout: float = 25) -> None:
    def rendered() -> bool:
        text = _body_text(page)
        return bool(re.search(r"\d+(?:\.\d+)?\s*%\s*used", text, re.I) or re.search(r"current\s+plan", text, re.I))

    _wait_until(rendered, timeout, 1.0)
    time.sleep(1.0)  # 让 API 响应尽量到齐


# “Message usage limit” 的金额（如 20）是页面上输入框里的值，innerText 里没有，必须读 input.value。
# 从标签文字往上找最近的、含输入框的祖先，取其中输入框的值。
_MESSAGE_LIMIT_JS = """
() => {
  const labels = [...document.querySelectorAll('body *')].filter(e =>
    [...e.childNodes].some(c => c.nodeType === 3 && /message\\s+usage\\s+limit/i.test(c.textContent || '')));
  for (const label of labels) {
    let node = label;
    for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
      const input = node.querySelector(
        'input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=search]), textarea');
      if (input) return input.value === '' ? null : String(input.value);
    }
  }
  return null;
}
"""


def _read_message_limit(page: Page) -> float | None:
    """读取 “Message usage limit” 输入框里的金额；读不到返回 None。"""
    try:
        raw = page.evaluate(_MESSAGE_LIMIT_JS)
    except Exception:
        return None
    if raw is None:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", str(raw))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _cycle_from_invoice_page(page: Page, data: dict[str, Any]) -> dict[str, Any]:
    """读发票页文本，用最近一张续费发票推算账期（只在页面没给账期时兜底）。"""
    page.goto(DEVIN_INVOICES_URL, wait_until="domcontentloaded")
    _wait_until(lambda: _invoices_rendered(_body_text(page)), 20, 1.0)
    time.sleep(1.0)
    rows = parse_invoices_text(_body_text(page))
    return cycle_from_invoices(
        rows, billing_period=data.get("billing_period") or "month", plan_price=data.get("plan_price_usd"),
    )


def _scrape(page: Page, email: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """读取用量页（必要时再读套餐页），**轮询到核心项齐全或超时**，避免页面还在加载就读到半截数据。"""
    responses: list[Any] = []

    def on_response(resp: Any) -> None:
        try:
            url = resp.url
            if DEVIN_API_HOST in url and ("billing" in url or "quota" in url or "usage" in url or "plan" in url or "subscription" in url):
                responses.append(resp)
        except Exception:
            pass

    def snapshot_data(text_data: dict[str, Any]) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
        payloads = _read_api_payloads(responses)
        data, source = merge_results(extract_from_api(payloads), text_data)
        return data, source, payloads

    page.on("response", on_response)
    texts: list[str] = []
    text_data: dict[str, Any] = {}
    cycle_source = "page"
    try:
        page.goto(DEVIN_USAGE_URL, wait_until="domcontentloaded")
        _wait_usage_rendered(page)

        # 反复读取用量页文本并合并，直到核心项齐全或超时（页面是异步加载的，一次读取常常是半截）
        texts.append("")
        deadline = time.time() + 40
        while True:
            text = _body_text(page)
            if text:
                texts[0] = text
            for key, val in parse_usage_text(text).items():
                if val is not None:
                    text_data[key] = val
            if text_data.get("message_limit_usd") is None:
                limit = _read_message_limit(page)
                if limit is not None:
                    text_data["message_limit_usd"] = limit
            merged, _src, _payloads = snapshot_data(text_data)
            if not missing_fields(merged) or time.time() >= deadline:
                break
            time.sleep(1.5)

        # 消息用量上限的输入框常比其它数据晚一点渲染：页面有这个标签但值还没读到，再多等几秒
        if text_data.get("message_limit_usd") is None and re.search(r"message\s+usage\s+limit", texts[0] or "", re.I):
            for _ in range(4):
                limit = _read_message_limit(page)
                if limit is not None:
                    text_data["message_limit_usd"] = limit
                    break
                page.wait_for_timeout(1500)

        # 用量页仍缺套餐/账期（价格、续费日常在套餐页）→ 再读 /settings/plans
        data_now, _s, _p = snapshot_data(text_data)
        if any(x in missing_fields(data_now) for x in ("套餐", "账期")) or data_now.get("plan_price_usd") is None:
            try:
                page.goto(DEVIN_PLANS_URL, wait_until="domcontentloaded")
                _wait_until(lambda: bool(re.search(r"current\s+plan|renews", _body_text(page), re.I)), 20, 1.0)
                plans_text = _body_text(page)
                texts.append(plans_text)
                for key, val in parse_usage_text(plans_text).items():
                    text_data.setdefault(key, val)
                time.sleep(1.0)
            except Exception as exc:
                log.debug(f"[{email}] plans 页读取失败（忽略）: {exc}")

        # 用量页和套餐页都没有账期 → 用发票页兜底（最近一张续费发票日期 = 本账期起点）
        data_now, _s, _p = snapshot_data(text_data)
        if "账期" in missing_fields(data_now):
            try:
                fallback = _cycle_from_invoice_page(page, data_now)
                if fallback:
                    for key, val in fallback.items():
                        text_data.setdefault(key, val)
                    cycle_source = "invoice"
                    log.info(f"[{email}] 账期由发票页推算: {fallback}")
            except Exception as exc:
                log.debug(f"[{email}] 发票页推算账期失败（忽略）: {exc}")
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    data, source, payloads = snapshot_data(text_data)
    missing = missing_fields(data)
    raw = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "page_url": page.url,
        "api": payloads,
        "text": [t[:6000] for t in texts],
        "missing": missing,
        "cycle_source": cycle_source if data.get("renews_at") else "",
    }
    if missing:
        # 数据不全：留下截图 + 页面文本，方便对照真实页面调整解析
        raw["artifact"] = _save_artifacts(page, email, "incomplete")
        log.warning(f"[{email}] Devin 数据不全 missing={missing} {raw['artifact']}")
    return data, source, raw


def _with_session(account: dict[str, Any], work, state: dict[str, Any]):
    """在账号专属 profile 里打开浏览器并确保已登录，然后执行 ``work(page, context)``。

    受 DEVIN_CONCURRENCY 限流；``state["login_used"]`` 记录这次是否走了验证码登录。
    异常原样抛出，由调用方分类。
    """
    email = account["email"]
    cfg = devin_settings()
    with _get_semaphore():
        with sync_playwright() as pw:
            context, page = browser_login._launch(pw, cfg.headless, cfg.proxy, f"devin__{email}")
            try:
                page.goto(DEVIN_USAGE_URL, wait_until="domcontentloaded")
                _wait_until(lambda: _is_login_page(page) or _is_logged_in(page), 25, 1.0)
                if not _is_logged_in(page):
                    if not _is_login_page(page):
                        hint = _save_artifacts(page, email, "no-login-form")
                        raise DevinError("login_failed", f"未出现登录表单，当前 {page.url}{hint}")
                    state["login_used"] = True
                    _login_with_email_code(page, account)
                    browser_login._human_pause(1.0, 2.0)
                return work(page, context)
            finally:
                try:
                    context.close()
                except Exception:
                    pass


def collect_account(account: dict[str, Any]) -> CollectResult:
    """采集单账号套餐/用量（阻塞）。异常都转成 CollectResult，不向外抛。"""
    email = account["email"]
    started = time.time()
    state: dict[str, Any] = {"login_used": False}

    def work(page: Page, _context: Any):
        data, source, raw = _scrape(page, email)
        if not has_core_data(data):
            hint = _save_artifacts(page, email, "no-data")
            raise DevinError("page_parse", f"页面已登录但未解析到数据，url={page.url}{hint}")
        return data, source, raw

    try:
        data, source, raw = _with_session(account, work, state)
        return CollectResult(
            email=email, ok=True, data=data, source=source, raw=raw,
            login_used=state["login_used"], duration_ms=int((time.time() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001
        code, message = classify_exception(exc)
        log.warning(f"[{email}] Devin 采集失败 code={code}: {message}")
        return CollectResult(
            email=email, ok=False, error_code=code, error_message=message,
            login_used=state["login_used"], duration_ms=int((time.time() - started) * 1000),
        )


# ─── 发票（只导出，不入库）────────────────────────────────────────

@dataclass
class InvoiceResult:
    email: str
    ok: bool
    invoices: list[dict[str, Any]] = field(default_factory=list)
    error_code: str = ""
    error_message: str = ""
    login_used: bool = False
    duration_ms: int = 0


_NO_INVOICES_TEXT = re.compile(r"no invoices|no invoice (?:found|history)|nothing to show|you don'?t have any invoices", re.I)
_INVOICES_RENDERED = re.compile(r"view invoice|download|no invoices", re.I)


def _invoices_rendered(text: str) -> bool:
    """发票页是否渲染完：窄窗口下没有 “View invoice” 文字，所以也要认“已解析出发票行”。"""
    return bool(_INVOICES_RENDERED.search(text) or parse_invoices_text(text))


def _invoice_links(page: Page, context: Any, expected: int) -> list[str]:
    """按行序取每张发票的外链：优先读 <a href>；"View invoice" 是按钮时点击并捕获新开页面的 URL。"""
    try:
        anchors = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({text: (e.innerText || '').trim(), href: e.href}))",
        )
    except Exception:
        anchors = []
    links = pick_invoice_links(anchors)
    if len(links) >= expected:
        return links[:expected]

    return _links_from_rows(page, context, expected)


# 找“发票行”（日期 + 状态 + 金额都在同一个最内层元素里）并给它们按页面顺序打上序号。
# 窗口较窄时 “View invoice” 文字会被收起，只剩一个绿色箭头，所以不能靠文字找入口，要靠行来定位。
_MARK_ROWS_JS = """
() => {
  const re = /(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\\.?\\s+\\d{1,2},?\\s+\\d{4}\\s+(?:paid|open|void|draft|uncollectible|failed|refunded|pending|past due|unpaid|overdue|processing|canceled|cancelled)\\s+-?\\s*\\$/i;
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  document.querySelectorAll('[data-cam-inv-row]').forEach(e => e.removeAttribute('data-cam-inv-row'));
  const hits = [...document.querySelectorAll('body *')].filter(e => re.test(norm(e.innerText)));
  const rows = hits.filter(e => !hits.some(o => o !== e && e.contains(o)));
  rows.forEach((e, i) => e.setAttribute('data-cam-inv-row', String(i)));
  return rows.length;
}
"""
_LIST_PAGE_RE = re.compile(r"/settings/invoices/?(?:[?#].*)?$")


def _mark_invoice_rows(page: Page) -> int:
    try:
        return int(page.evaluate(_MARK_ROWS_JS))
    except Exception:
        return 0


def _links_from_rows(page: Page, context: Any, expected: int) -> list[str]:
    """逐行取发票外链：行里有 <a href> 就直接读；只有箭头图标时就点击它，
    捕获新开标签页（或当前页跳转）的地址。"""
    links: list[str] = []
    count = min(_mark_invoice_rows(page), expected, 24)
    list_url = page.url
    for i in range(count):
        link = ""  # 每行恰好追加一个结果（取不到就是空串），保证与表格行一一对应
        try:
            _mark_invoice_rows(page)  # 上一次点击/返回后 DOM 可能重绘，重新打标
            row = page.locator(f'[data-cam-inv-row="{i}"]').first
            if row.count() > 0:
                link = _row_anchor_href(row) or _click_row_for_link(page, context, row, list_url)
        except Exception as exc:  # noqa: BLE001
            log.debug(f"取第 {i + 1} 行发票链接失败: {exc}")
        links.append(link)
    return links


def _row_anchor_href(row: Any) -> str:
    """行里直接有 <a href>（不是指回发票列表页本身）就读它。"""
    try:
        anchors = row.locator("a[href]")
        for j in range(min(anchors.count(), 4)):
            cand = anchors.nth(j).evaluate("e => e.href") or ""
            if cand.startswith("http") and not _LIST_PAGE_RE.search(cand):
                return cand
    except Exception:
        pass
    return ""


def _settle_popup_url(popup: Any, timeout: float = 10.0) -> str:
    """新标签页刚创建时地址常是空的/about:blank，且可能先经过 Devin 的跳转页；
    等它落到 http 地址（优先等到 stripe.com），超时就取最后一个 http 地址。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        url = popup.url or ""
        if url.startswith("http"):
            last = url
            if "stripe.com" in url:
                return url
        # 必须用 Playwright 自己的等待：time.sleep 不会处理浏览器事件，popup.url 不会更新
        try:
            popup.wait_for_timeout(400)
        except Exception:
            break
    return popup.url if (popup.url or "").startswith("http") else last


def _click_row_for_link(page: Page, context: Any, row: Any, list_url: str) -> str:
    """窄窗口下行里只有一个绿色箭头：点它，捕获新标签页地址；没有新标签页就看当前页是否跳走了。"""
    clickables = row.locator("a, button, [role=button], svg")
    target = clickables.last if clickables.count() else row
    try:
        with context.expect_page(timeout=8000) as popup_info:
            target.click()
        popup = popup_info.value
        url = _settle_popup_url(popup)
        try:
            popup.close()
        except Exception:
            pass
        return url
    except Exception:
        pass
    # 没有新标签页：可能在当前页直接跳到了发票页，记下地址再退回列表
    if page.url != list_url and page.url.startswith("http") and not _LIST_PAGE_RE.search(page.url):
        url = page.url
        try:
            page.go_back(wait_until="domcontentloaded")
            _wait_until(lambda: _invoices_rendered(_body_text(page)), 15, 1.0)
        except Exception as exc:  # noqa: BLE001
            log.debug(f"退回发票列表失败: {exc}")
        return url
    return ""


def pick_invoice_links(anchors: list[dict[str, str]]) -> list[str]:
    """从页面所有 <a> 里挑出 “View invoice →” 的外链（与表格行同序）。

    只认以 View invoice 开头的链接文字：左侧导航里也有一个叫 “Invoices” 的链接
    （指向 /settings/invoices），不能当成某张发票的链接。"""
    out: list[str] = []
    for a in anchors:
        href = a.get("href", "")
        if not re.match(r"^\s*view invoice", a.get("text", ""), re.I) or not href.startswith("http"):
            continue
        if re.search(r"/settings/invoices/?(?:[?#].*)?$", href):
            continue
        out.append(href)
    return out


def _is_pdf(body: bytes | None) -> bool:
    return bool(body) and body[:4] == b"%PDF"


def _write_pdf(dest: Path, body: bytes) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return str(dest)


def _fetch_pdf(context: Any, url: str) -> bytes | None:
    """用浏览器上下文（带登录/会话 Cookie）直接 GET，校验 %PDF 头。"""
    try:
        resp = context.request.get(url, timeout=30000)
        body = resp.body() if resp.ok else None
        return body if _is_pdf(body) else None
    except Exception as exc:  # noqa: BLE001
        log.debug(f"发票 PDF 直链失败 {url}: {exc}")
        return None


_INVOICE_NO_RE = re.compile(r"(?:账单编号|发票编号|Invoice\s+number|Invoice\s+#)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9\-]{3,40})", re.I)


def invoice_number_from_text(text: str) -> str:
    """Stripe 发票页上的编号（中文页“账单编号 OVNB3L63-0001” / 英文页 “Invoice number …”）。"""
    m = _INVOICE_NO_RE.search(text or "")
    return m.group(1) if m else ""


def _pdf_via_hosted_page(
    context: Any, url: str, dest: Path, email: str, save_artifact: bool, meta: dict[str, Any] | None = None
) -> tuple[str, str, str]:
    """打开 Stripe 托管发票页，点“下载账单 / Download invoice”按钮拿官方 PDF（与 Cursor 侧同一套选择器，
    跳过“下载收据”）；按钮点不出来时，把整页打印成 PDF 兜底。返回 (路径, 来源, 失败原因)。
    页面上的发票编号会写进 ``meta["number"]``，用来给 PDF 命名。"""
    from .exporter import _is_receipt_download_text, _stripe_invoice_download_selectors

    page = context.new_page()
    try:
        page.goto(url, wait_until="load", timeout=30000)
        try:
            page.wait_for_selector("button, a[href]", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        if meta is not None:
            meta["number"] = meta.get("number") or invoice_number_from_text(_body_text(page))

        clicks = 0
        for sel in _stripe_invoice_download_selectors():
            if clicks >= 4:  # 每次点击最多等 15s，别被宽泛选择器拖太久
                break
            try:
                el = page.locator(sel).first
                if el.count() == 0 or _is_receipt_download_text(el.inner_text()):
                    continue
                href = el.get_attribute("href") or ""
                if href.startswith("http") and "pdf" in href.lower():
                    body = _fetch_pdf(context, href)
                    if body:
                        return _write_pdf(dest, body), "download", ""
                clicks += 1
                before = set(context.pages)
                try:
                    with page.expect_download(timeout=15000) as dl_info:
                        el.click()
                    dl_info.value.save_as(str(dest))
                    if dest.exists() and _is_pdf(dest.read_bytes()[:8]):
                        return str(dest), "download", ""
                    dest.unlink(missing_ok=True)
                except Exception:
                    # 没触发下载事件：Stripe 有时在新标签页里直接打开 PDF，用它的地址再取一次
                    page.wait_for_timeout(1500)
                    for extra in [p for p in context.pages if p not in before and p != page]:
                        extra_url = extra.url
                        try:
                            extra.close()
                        except Exception:
                            pass
                        if extra_url.startswith("http") and "pdf" in extra_url.lower():
                            body = _fetch_pdf(context, extra_url)
                            if body:
                                return _write_pdf(dest, body), "download", ""
            except Exception:
                continue

        try:  # 兜底：把发票页本身打印成 PDF（内容完整，但不是 Stripe 官方文件）
            session = context.new_cdp_session(page)
            printed = session.send("Page.printToPDF", {"printBackground": True, "preferCSSPageSize": True})
            body = base64.b64decode(printed.get("data", ""))
            if _is_pdf(body):
                return _write_pdf(dest, body), "print", ""
        except Exception as exc:  # noqa: BLE001
            log.debug(f"[{email}] 发票页打印 PDF 失败: {exc}")
        hint = _save_artifacts(page, email, "invoice-pdf") if save_artifact else ""
        return "", "", f"发票页没找到可下载的 PDF 按钮{hint}"
    except Exception as exc:  # noqa: BLE001
        return "", "", f"打开发票页失败: {str(exc)[:120]}"
    finally:
        try:
            page.close()
        except Exception:
            pass


def _download_invoice_pdf(
    context: Any, inv: dict[str, Any], dest: Path, email: str, save_artifact: bool = False,
    meta: dict[str, Any] | None = None,
) -> tuple[str, str, str]:
    """拿到一张发票的 PDF：API 给的 pdf 直链 → Stripe 直链推导 → 打开发票页点下载 → 打印页面。

    返回 (本地路径, 来源 direct/download/print, 失败原因)；成功时失败原因为空串。"""
    hosted = inv.get("invoice_url") or ""
    for url in (inv.get("pdf_url") or "", stripe_pdf_url(hosted)):
        if url:
            body = _fetch_pdf(context, url)
            if body:
                return _write_pdf(dest, body), "direct", ""
    if not hosted:
        return "", "", "没有取到发票链接（页面里的 View invoice 未能解析）"
    last = ""
    for _attempt in range(2):
        path, source, last = _pdf_via_hosted_page(
            context, hosted, dest, email, save_artifact and _attempt == 1, meta,
        )
        if path:
            return path, source, ""
        time.sleep(2)
    return "", "", last


def _pdf_name(inv: dict[str, Any], used: set[str]) -> str:
    """同一账号内唯一的 PDF 文件名：日期-发票号（缺发票号用状态+金额）。"""
    tail = inv.get("number") or f"{(inv.get('status') or 'invoice').lower()}-{inv.get('amount_usd') if inv.get('amount_usd') is not None else 'na'}"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{inv.get('invoice_date') or 'unknown'}-{tail}").strip("_")
    name, i = f"{base}.pdf", 2
    while name in used:
        name, i = f"{base}-{i}.pdf", i + 1
    used.add(name)
    return name


def _scrape_invoices(page: Page, context: Any, email: str) -> list[dict[str, Any]]:
    responses: list[Any] = []

    def on_response(resp: Any) -> None:
        try:
            if DEVIN_API_HOST in resp.url and ("invoice" in resp.url or "billing" in resp.url):
                responses.append(resp)
        except Exception:
            pass

    page.on("response", on_response)
    try:
        page.goto(DEVIN_INVOICES_URL, wait_until="domcontentloaded")
        _wait_until(lambda: _invoices_rendered(_body_text(page)), 25, 1.0)
        time.sleep(1.0)
        text = _body_text(page)
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    text_rows = parse_invoices_text(text)
    if not text_rows and not _NO_INVOICES_TEXT.search(text):
        hint = _save_artifacts(page, email, "no-invoices-parse")
        raise DevinError("page_parse", f"发票页未解析到发票行，url={page.url}{hint}")
    api_rows = extract_invoices_from_api(_read_api_payloads(responses))
    links = _invoice_links(page, context, len(text_rows)) if text_rows else []
    invoices = merge_invoices(text_rows, api_rows, links)

    # 发票交付物就是 PDF：逐张下载，保存到 <导出目录>/invoice-pdf/<账号>/，由导出任务统一打包
    safe = re.sub(r"[^A-Za-z0-9._@-]+", "_", email)
    folder = devin_settings().export_dir / "invoice-pdf" / safe
    used: set[str] = set()
    artifact_saved = False
    for inv in invoices:
        name = _pdf_name(inv, used)
        meta: dict[str, Any] = {}
        path, source, err = _download_invoice_pdf(
            context, inv, folder / name, email, save_artifact=not artifact_saved, meta=meta,
        )
        if path and not inv.get("number") and meta.get("number"):
            # 发票页上才有发票编号：用它给文件改名（日期-编号.pdf），清单里也能对上
            inv["number"] = meta["number"]
            used.discard(name)
            new_path = Path(path).with_name(_pdf_name(inv, used))
            try:
                Path(path).replace(new_path)
                path = str(new_path)
            except OSError as exc:
                used.add(name)
                log.debug(f"[{email}] PDF 改名失败（保留原名）: {exc}")
        inv["pdf_file"], inv["pdf_source"], inv["pdf_error"] = path, source, err
        if err:
            artifact_saved = artifact_saved or "现场" in err
            log.warning(f"[{email}] 发票 {inv.get('invoice_date')} PDF 未拿到: {err}")
    return invoices


def collect_invoices(account: dict[str, Any]) -> InvoiceResult:
    """登录并读取该账号的发票列表（阻塞）。异常都转成 InvoiceResult，不向外抛。"""
    email = account["email"]
    started = time.time()
    state: dict[str, Any] = {"login_used": False}
    try:
        invoices = _with_session(account, lambda page, context: _scrape_invoices(page, context, email), state)
        return InvoiceResult(
            email=email, ok=True, invoices=invoices,
            login_used=state["login_used"], duration_ms=int((time.time() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001
        code, message = classify_exception(exc)
        log.warning(f"[{email}] Devin 发票读取失败 code={code}: {message}")
        return InvoiceResult(
            email=email, ok=False, error_code=code, error_message=message,
            login_used=state["login_used"], duration_ms=int((time.time() - started) * 1000),
        )
