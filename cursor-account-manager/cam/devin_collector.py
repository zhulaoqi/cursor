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
    extract_from_api,
    extract_invoices_from_api,
    has_core_data,
    merge_invoices,
    merge_results,
    parse_invoices_text,
    parse_usage_text,
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


def _scrape(page: Page, email: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
    responses: list[Any] = []

    def on_response(resp: Any) -> None:
        try:
            url = resp.url
            if DEVIN_API_HOST in url and ("billing" in url or "quota" in url or "usage" in url or "plan" in url or "subscription" in url):
                responses.append(resp)
        except Exception:
            pass

    page.on("response", on_response)
    texts: list[str] = []
    try:
        page.goto(DEVIN_USAGE_URL, wait_until="domcontentloaded")
        _wait_usage_rendered(page)
        usage_text = _body_text(page)
        texts.append(usage_text)
        text_data = parse_usage_text(usage_text)

        # 套餐信息不全时再看 /settings/plans（价格/续费日期通常在这里）
        if not (text_data.get("plan_name") and text_data.get("renews_at")):
            try:
                page.goto(DEVIN_PLANS_URL, wait_until="domcontentloaded")
                _wait_until(lambda: bool(re.search(r"current\s+plan|renews", _body_text(page), re.I)), 15, 1.0)
                plans_text = _body_text(page)
                texts.append(plans_text)
                for key, val in parse_usage_text(plans_text).items():
                    text_data.setdefault(key, val)
            except Exception as exc:
                log.debug(f"[{email}] plans 页读取失败（忽略）: {exc}")
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    payloads = _read_api_payloads(responses)
    api_data = extract_from_api(payloads)
    data, source = merge_results(api_data, text_data)
    raw = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "page_url": page.url,
        "api": payloads,
        "text": [t[:6000] for t in texts],
    }
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


def _invoice_links(page: Page, context: Any, expected: int) -> list[str]:
    """按行序取每张发票的外链：优先读 <a href>；"View invoice" 是按钮时点击并捕获新开页面的 URL。"""
    try:
        anchors = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({text: (e.innerText || '').trim(), href: e.href}))",
        )
    except Exception:
        anchors = []
    links = [a["href"] for a in anchors if re.search(r"view invoice|invoice", a.get("text", ""), re.I) and a.get("href", "").startswith("http")]
    if len(links) >= expected:
        return links[:expected]

    links = []
    try:
        buttons = page.get_by_text(re.compile(r"^\s*View invoice", re.I))
        count = min(buttons.count(), expected, 24)
    except Exception:
        return []
    for i in range(count):
        try:
            with context.expect_page(timeout=8000) as popup_info:
                buttons.nth(i).click()
            popup = popup_info.value
            try:
                popup.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            links.append(popup.url)
            popup.close()
        except Exception:
            links.append("")
    return links


def _download_pdf(context: Any, url: str, dest: Path) -> str:
    try:
        resp = context.request.get(url, timeout=30000)
        if resp.ok and resp.body()[:4] == b"%PDF":
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(resp.body())
            return str(dest)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"发票 PDF 下载失败 {url}: {exc}")
    return ""


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
        _wait_until(lambda: bool(_INVOICES_RENDERED.search(_body_text(page))) , 25, 1.0)
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

    cfg = devin_settings()
    if cfg.invoice_pdf:
        safe = re.sub(r"[^A-Za-z0-9._@-]+", "_", email)
        for inv in invoices:
            inv["pdf_file"] = ""
            if inv.get("pdf_url"):
                name = f"{inv['invoice_date']}-{inv.get('number') or 'invoice'}.pdf"
                inv["pdf_file"] = _download_pdf(context, inv["pdf_url"], cfg.export_dir / "invoice-pdf" / safe / name)
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
