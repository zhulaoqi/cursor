"""从 Cursor 账号看板页面解析当前套餐金额。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Callable, Optional

from .api_client import _split_session_token
from .config import CURSOR_WEB_BASE, SETTINGS
from .logger import get
from .models import Account

if TYPE_CHECKING:
    from .token_manager import TokenManager

log = get("plan")

_PLAN_BROWSER_SEM = threading.Semaphore(max(1, SETTINGS.invoice_active_context_limit))

# 消费页解析：部分账号进入 spending 后主内容渲染慢于侧边导航，等待窗口不能过短。
_SPENDING_POLL_MAX_ROUNDS = 16
_SPENDING_POLL_INTERVAL_MS = 700
_SPENDING_INIT_SETTLE_MS = 800
_SPENDING_NETWORKIDLE_TIMEOUT_MS = 12000


def _run_playwright_coroutine(coro) -> object:
    """patchright 依赖 asyncio；FastAPI 等已在跑事件循环时不能 ``asyncio.run``，放到独立线程执行。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=360)


@dataclass(frozen=True)
class PlanInfo:
    status: str
    amount: Optional[Decimal] = None
    error: str = ""


@dataclass(frozen=True)
class SpendingPanelInfo:
    """cursor.com/.../dashboard/spending 页面解析：套餐档位名 + On-Demand 状态。

    业务语义（与 Cursor Spending「On-Demand Usage」一致）：
    - **当前是否开通按需**：以 **Monthly Limit** 为准：下拉为 **Fixed** / **Unlimited** 视为开；**Disabled** 视为关；无三档英文时再试旧版 ``Enabled``/``Disabled``、``On``/``Off`` 及中文。
    - ``on_demand_enabled=True``：当前按需已开（按量超支开关打开）。
    - ``on_demand_enabled=False`` 且 ``on_demand_historical=True``：当前已关，但 **On-Demand Spending 行仍有正金额**（曾产生过按需消费）。
    - ``on_demand_historical=False`` 且 ``on_demand_enabled=False``：当前关且无历史金额（或未解析到金额）。

    ``plan_snapshot``：同页全文顺带解析出的套餐状态（仅 ``active`` / ``not_enabled`` 时有值，写入与 ``update_account_plan`` 相同字段）。
    """

    plan_name: str
    on_demand_enabled: Optional[bool]
    error: str = ""
    plan_snapshot: Optional[PlanInfo] = None
    on_demand_historical: bool = False


@dataclass
class SpendingPanelBatchItem:
    """批量消费页解析单账号结果。"""

    email: str
    info: Optional[SpendingPanelInfo] = None
    error: str = ""


@dataclass(frozen=True)
class OnDemandPanelParse:
    """消费页 On-Demand Usage 区域解析结果。"""

    currently_enabled: Optional[bool]
    had_historical_spend: bool
    spend_amount: Optional[Decimal] = None


_CURRENT_PLAN_TEXT_JS = """
() => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const isVisible = el => !!(el && (el.offsetParent !== null || el.getClientRects().length > 0));
  const hasMoney = s => /(?:\\$|USD\\s*)\\s*\\d[\\d,]*(?:\\.\\d+)?|\\d[\\d,]*(?:\\.\\d+)?\\s*(?:USD|美元)/i.test(s || '');
  const hasCurrentPlan = s => /current\\s+plan|当前\\s*套餐|当前\\s*计划/i.test(s || '');
  const hasNoPaidPlan = s => /upgrade\\s+to\\s+pro|requires\\s+a\\s+paid\\s+plan|free|升级\\s*到\\s*pro|需要付费套餐/i.test(s || '');

  const labelNodes = [...document.querySelectorAll('body *')].filter(el => {
    if (!isVisible(el)) return false;
    const ownText = norm([...el.childNodes]
      .filter(node => node.nodeType === Node.TEXT_NODE)
      .map(node => node.textContent || '')
      .join(' '));
    return hasCurrentPlan(ownText || el.textContent || '');
  });

  for (const label of labelNodes) {
    let el = label;
    for (let depth = 0; el && depth < 6; depth += 1, el = el.parentElement) {
      const text = norm(el.innerText || el.textContent || '');
      if (hasCurrentPlan(text) && hasMoney(text)) return text;
    }
    const siblingText = norm(label.parentElement?.innerText || label.parentElement?.textContent || '');
    if (hasCurrentPlan(siblingText) && hasMoney(siblingText)) return siblingText;
  }

  const nodes = [...document.querySelectorAll('section, main, article, div, li, tr, [role="region"], [role="group"]')]
    .filter(isVisible)
    .map(el => norm(el.innerText || el.textContent || ''))
    .filter(text => text && text.length <= 1200);

  for (const text of nodes) {
    if (hasCurrentPlan(text) && hasMoney(text)) return text;
  }

  const body = norm(document.body?.innerText || '');
  const idx = body.toLowerCase().search(/current\\s+plan|当前\\s*套餐|当前\\s*计划/i);
  if (idx >= 0) return body.slice(Math.max(0, idx - 120), idx + 500);
  if (hasNoPaidPlan(body)) return body.slice(0, 1200);
  return '';
}
"""


def extract_current_plan_amount_from_text(text: str) -> Optional[Decimal]:
    """只从 Current Plan 附近提取金额，避免把额度/用量数字误当套餐金额。"""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return None
    match_label = re.search(r"current\s+plan|当前\s*套餐|当前\s*计划", normalized, re.I)
    if not match_label:
        return None
    window = normalized[match_label.start(): match_label.start() + 500]
    money = re.search(
        r"(?:\$|USD\s*)\s*(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*(?:USD|美元)",
        window,
        re.I,
    )
    if not money:
        return None
    raw = (money.group(1) or money.group(2) or "").replace(",", "")
    try:
        amount = Decimal(raw)
        if not amount.is_finite():
            return None
        return amount
    except (InvalidOperation, ValueError):
        return None


def extract_current_plan_info_from_text(text: str) -> PlanInfo:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    amount = extract_current_plan_amount_from_text(normalized)
    if amount is not None:
        return PlanInfo(status="active", amount=amount, error="")
    no_plan_match = re.search(
        r"upgrade\s+to\s+pro|requires\s+a\s+paid\s+plan|(?:^|\s)free(?:\s|$)|升级\s*到\s*pro|需要付费套餐",
        normalized,
        re.I,
    )
    if no_plan_match:
        return PlanInfo(
            status="not_enabled",
            amount=None,
            error=_compact_text(no_plan_match.group(0), limit=120),
        )
    return PlanInfo(status="error", amount=None, error="Current Plan 套餐金额未解析")


_AGGREGATE_PAGE_TEXT_JS = """
() => {
  const parts = [];
  const push = (doc) => {
    try {
      if (doc && doc.body) parts.push(doc.body.innerText || '');
    } catch (e) {}
  };
  push(document);
  for (const f of document.querySelectorAll('iframe')) {
    try {
      push(f.contentDocument);
    } catch (e) {}
  }
  return parts.join('\\n\\n');
}
"""


def plan_snapshot_from_spending_full_text(full_text: str) -> Optional[PlanInfo]:
    """消费页聚合文本上解析套餐开通状态与金额（与 ``extract_current_plan_info_from_text`` 一致）。"""
    info = extract_current_plan_info_from_text(full_text)
    if info.status in ("active", "not_enabled"):
        return info
    return None


def extract_plan_name_from_spending_text(text: str) -> str:
    """从消费页全文提取 Current Plan 档位名（如 Ultra）。"""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return ""
    m = re.search(r"current\s+plan", normalized, re.I)
    if not m:
        return ""
    window = normalized[m.start() : m.start() + 420]
    name_m = re.search(
        r"\b(Ultra|Pro\s*Plus|Pro|Team|Business|Enterprise|Free|Hobby)\b",
        window,
        re.I,
    )
    if not name_m:
        return ""
    return re.sub(r"\s+", "", name_m.group(1))


def extract_on_demand_spending_amount_from_text(text: str) -> Optional[Decimal]:
    """从 On-Demand Spending 标题附近取美元金额（正数），不含 Current Plan 区域。"""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return None
    m = re.search(r"on[\s\-–—]*demand\s+spending", normalized, re.I)
    if not m:
        m = re.search(r"按需(?:支出|消费)", normalized, re.I)
    if not m:
        return None
    window = normalized[m.start() : m.start() + 480]
    ml = re.search(r"monthly\s+limit|每月(?:额度)?限额", window, re.I)
    sub = window[: ml.start()] if ml else window
    money = re.search(
        r"(?:\$|USD\s*)\s*(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*(?:USD|美元)",
        sub,
        re.I,
    )
    if not money:
        return None
    raw = (money.group(1) or money.group(2) or "").replace(",", "")
    try:
        amt = Decimal(raw)
        if not amt.is_finite() or amt <= 0:
            return None
        return amt
    except (InvalidOperation, ValueError):
        return None


def _extract_on_demand_sentence_enabled(normalized: str) -> Optional[bool]:
    """仅从文案 ``currently enabled/disabled`` 等推断当前按需开关（不含 Monthly Limit 块）。"""
    m = re.search(
        r"on[\s\-–—]*demand\s+spending\s+is\s+currently\s*(enabled|disabled)\b",
        normalized,
        re.I,
    )
    if m:
        return m.group(1).lower() == "enabled"
    m = re.search(
        r"on[\s\-–—]*demand\s+spending\s+is\s+(enabled|disabled)\b",
        normalized,
        re.I,
    )
    if m:
        return m.group(1).lower() == "enabled"
    m = re.search(
        r"on[\s\-–—]*demand\s+spending[^.?!]{0,160}?currently\s+(enabled|disabled)\b",
        normalized,
        re.I,
    )
    if m:
        return m.group(1).lower() == "enabled"
    if re.search(r"按需消费.{0,80}?(已开启|启用|开启)", normalized, re.I):
        return True
    if re.search(r"按需消费.{0,80}?(已关闭|未开启|关闭|禁用)", normalized, re.I):
        return False
    if re.search(r"按需支出.{0,80}?(已开启|启用|开启)", normalized, re.I):
        return True
    if re.search(r"按需支出.{0,80}?(已关闭|未开启|关闭|禁用)", normalized, re.I):
        return False
    return None


def parse_on_demand_panel_from_text(text: str) -> OnDemandPanelParse:
    """解析 On-Demand Usage：当前开关以 Monthly Limit 下拉为准，其次文案；曾开通=当前关且行内金额>0。"""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    spend = extract_on_demand_spending_amount_from_text(normalized)
    ml = _extract_on_demand_from_monthly_limit_block(normalized)
    if ml is not None:
        current = ml
    else:
        current = _extract_on_demand_sentence_enabled(normalized)
    historical = current is False and spend is not None and spend > 0
    return OnDemandPanelParse(
        currently_enabled=current,
        had_historical_spend=historical,
        spend_amount=spend,
    )


def describe_on_demand_parse_for_log(text: str) -> str:
    """供日志使用：说明为何 ``currently_enabled`` 可能为 None（未命中规则，不是页面字段真为 null）。"""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return "empty_text"
    bits: list[str] = []
    spend = extract_on_demand_spending_amount_from_text(normalized)
    bits.append(f"spend_dollar={spend if spend is not None else 'none'}")
    has_od = bool(re.search(r"on[\s\-–—]*demand\s+spending|按需(?:支出|消费)", normalized, re.I))
    bits.append(f"on_demand_heading={'yes' if has_od else 'no'}")
    m = re.search(r"monthly\s+limit|每月(?:额度)?限额", normalized, re.I)
    if m:
        chunk = normalized[m.start() : m.start() + 560]
        tail = re.sub(
            r"^\s*Monthly\s+Limit\s+Set a fixed amount or make it unlimited\.?\s*",
            "",
            chunk,
            flags=re.I,
        )
        save_m = re.search(r"\bSave\b", tail, re.I)
        head = tail[: save_m.start()] if save_m else tail
        toks = [
            x.group(1).lower()
            for x in re.finditer(
                r"\b(fixed|unlimited|disabled|enabled|off|on)\b",
                head,
                re.I,
            )
        ]
        bits.append(f"ml_tokens={toks!r}")
        ch1 = chunk.replace("\n", " ")
        bits.append(f"ml_chunk={_compact_text(ch1, limit=200)!r}")
        bits.append(f"ml_bool={_extract_on_demand_from_monthly_limit_block(normalized)!r}")
    else:
        bits.append("ml_heading=no")
    bits.append(f"sentence_bool={_extract_on_demand_sentence_enabled(normalized)!r}")
    anchor = re.search(r"on[\s\-–—]*demand\s+spending|按需|monthly\s+limit|每月", normalized, re.I)
    if anchor:
        lo = max(0, anchor.start() - 30)
        ar_one_line = normalized[lo : lo + 380].replace("\n", " ")
        bits.append(f"around_anchor={_compact_text(ar_one_line, limit=400)!r}")
    return " | ".join(bits)


def extract_on_demand_enabled_from_text(text: str) -> Optional[bool]:
    """解析当前按需是否开启（与 ``parse_on_demand_panel_from_text`` 的 ``currently_enabled`` 一致）。"""
    return parse_on_demand_panel_from_text(text).currently_enabled


def _extract_on_demand_from_monthly_limit_block(normalized: str) -> Optional[bool]:
    """从 Monthly Limit 区块推断按需是否开启。

    下拉为 **Fixed / Unlimited / Disabled**（见 Cursor Spending 页）：仅 **Disabled** 视为按需月限额关闭；
    **Fixed** 与 **Unlimited** 视为已选择非禁用档位（按需相关为开）。
    兼容旧文案 ``Enabled``/``Disabled``、``On``/``Off``。
    """
    m = re.search(r"monthly\s+limit", normalized, re.I)
    if not m:
        m = re.search(r"每月(?:额度)?限额", normalized, re.I)
    if not m:
        return None
    chunk = normalized[m.start() : m.start() + 560]
    # 去掉「Monthly Limit + 说明句」，避免说明里的 *unlimited* 与下拉项 Unlimited 混淆
    tail = re.sub(
        r"^\s*Monthly\s+Limit\s+Set a fixed amount or make it unlimited\.?\s*",
        "",
        chunk,
        flags=re.I,
    )
    save_m = re.search(r"\bSave\b", tail, re.I)
    head = tail[: save_m.start()] if save_m else tail

    # 勾选符在 innerText 里可能出现，优先取「档位词 + 勾选」
    check_m = re.search(
        r"\b(Fixed|Unlimited|Disabled)\b\s*[\u2713\u2714\u2715✓√]",
        head,
        re.I,
    )
    if check_m:
        sel = check_m.group(1).lower()
        if sel == "disabled":
            return False
        if sel in ("fixed", "unlimited"):
            return True

    tri = list(re.finditer(r"\b(Fixed|Unlimited|Disabled)\b", head, re.I))
    if tri:
        sel = tri[-1].group(1).lower()
        if sel == "disabled":
            return False
        if sel in ("fixed", "unlimited"):
            return True

    legacy = list(re.finditer(r"\b(disabled|enabled|off|on)\b", head, re.I))
    if legacy:
        last = legacy[-1].group(1).lower()
        if last in ("enabled", "on"):
            return True
        if last in ("disabled", "off"):
            return False

    if re.search(r"禁用", chunk):
        return False
    if re.search(r"固定|无限制|无限", chunk):
        return True
    return None


def _plan_page_urls() -> list[str]:
    base = CURSOR_WEB_BASE.rstrip("/")
    seen: set[str] = set()
    out: list[str] = []
    for path in ("/cn/dashboard/spending", "/en/dashboard/spending"):
        u = f"{base}{path}"
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _compact_text(value: object, *, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _looks_like_spending_shell_only(full_text: str) -> bool:
    """判断是否只拿到了侧边导航/骨架文本（主消费区尚未稳定渲染）。"""
    normalized = re.sub(r"\s+", " ", str(full_text or "")).strip()
    if not normalized:
        return True
    low = normalized.lower()
    has_nav = "spending" in low and (
        "billing" in low or "members" in low or "settings" in low
    )
    has_core = bool(re.search(
        r"current\s+plan|on[\s\-–—]*demand\s+spending|monthly\s+limit|"
        r"当前\s*套餐|按需(?:支出|消费)|每月(?:额度)?限额",
        normalized,
        re.I,
    ))
    if has_core:
        return False
    if has_nav:
        return True
    return len(normalized) < 80


def _spending_info_from_full_text(
    full_text: str,
    *,
    silent: bool,
    log_url: str = "",
    log_status: object = "unknown",
) -> Optional[SpendingPanelInfo]:
    """从消费页聚合文本解析；无有效字段时返回 None。"""
    low_head = full_text[:1200].lower()
    if re.search(r"sign\s+in|log\s+in|登录", low_head, re.I) and "current plan" not in low_head:
        raise RuntimeError("消费页疑似未登录（未看到 Current Plan），请检查账号登录态")
    plan_name = extract_plan_name_from_spending_text(full_text)
    od = parse_on_demand_panel_from_text(full_text)
    if not (
        plan_name
        or od.currently_enabled is not None
        or od.had_historical_spend
        or od.spend_amount is not None
    ):
        return None
    if not silent:
        msg = (
            f"消费页解析 url={log_url} status={log_status} "
            f"plan_name={plan_name!r} on_demand_enabled={od.currently_enabled} "
            f"on_demand_historical={od.had_historical_spend}"
        )
        if od.currently_enabled is None:
            msg += (
                " | on_demand_diag(未解析到开/关，非页面 null，而是规则未命中)="
                + describe_on_demand_parse_for_log(full_text)
            )
        log.info(msg)
    parse_error = ""
    if od.currently_enabled is None:
        parse_error = "未解析到 Monthly Limit 开关状态"
        if od.spend_amount is not None:
            parse_error += f"（On-Demand Spending=${od.spend_amount}）"
    return SpendingPanelInfo(
        plan_name=plan_name,
        on_demand_enabled=od.currently_enabled,
        error=parse_error,
        plan_snapshot=plan_snapshot_from_spending_full_text(full_text),
        on_demand_historical=od.had_historical_spend,
    )


async def _scroll_spending_page(page) -> None:
    try:
        await page.evaluate(
            "() => { const se = document.scrollingElement || document.body; "
            "if (se) se.scrollTop = se.scrollHeight; }"
        )
    except Exception:
        pass


async def _poll_spending_full_text(page) -> str:
    await _scroll_spending_page(page)
    await page.wait_for_timeout(_SPENDING_INIT_SETTLE_MS)
    full_text = ""
    for _ in range(_SPENDING_POLL_MAX_ROUNDS):
        await _scroll_spending_page(page)
        full_text = str(await page.evaluate(_AGGREGATE_PAGE_TEXT_JS) or "")
        if _spending_info_from_full_text(full_text, silent=True):
            return full_text
        await page.wait_for_timeout(_SPENDING_POLL_INTERVAL_MS)
    return full_text


async def _fetch_spending_panel_on_page(page, *, silent: bool) -> SpendingPanelInfo:
    """在当前 page 上依次尝试各 Spending URL，成功即返回。"""
    diagnostics: list[dict[str, object]] = []
    for url in _plan_page_urls():
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            status = response.status if response is not None else "unknown"
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=_SPENDING_NETWORKIDLE_TIMEOUT_MS
                )
            except Exception:
                pass
            full_text = await _poll_spending_full_text(page)
            info = _spending_info_from_full_text(
                full_text, silent=silent, log_url=page.url, log_status=status
            )
            if info is not None:
                return info
            if _looks_like_spending_shell_only(full_text):
                # 批量并发时会偶发“仅导航先出现，主内容晚到”，补一次延后重抓。
                await page.wait_for_timeout(1500)
                retry_text = await _poll_spending_full_text(page)
                info = _spending_info_from_full_text(
                    retry_text, silent=silent, log_url=page.url, log_status=status
                )
                if info is not None:
                    return info
                full_text = retry_text
            snippet = _compact_text(full_text, limit=300)
            diagnostics.append({
                "target_url": url,
                "final_url": page.url,
                "status": status,
                "text_snippet": snippet or "<empty_text>",
            })
        except Exception as e:
            diagnostics.append({
                "target_url": url,
                "final_url": getattr(page, "url", ""),
                "status": "exception",
                "text_snippet": f"{type(e).__name__}: {e}",
            })
            continue
    detail = _format_plan_diagnostics(diagnostics)
    if detail:
        raise RuntimeError(f"消费页解析失败; {detail}")
    raise RuntimeError("消费页解析失败; diagnostics=empty")


async def _fetch_spending_panel_reuse_browser(browser, cookie_val: str, *, silent: bool) -> SpendingPanelInfo:
    """在同一 Chromium 实例上新开 context，避免每账号 launch 浏览器。"""
    ctx = await browser.new_context()
    try:
        await ctx.add_cookies([{
            "name": "WorkosCursorSessionToken",
            "value": cookie_val,
            "domain": "cursor.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
        }])
        page = await ctx.new_page()
        return await _fetch_spending_panel_on_page(page, silent=silent)
    finally:
        await ctx.close()


def _format_plan_diagnostics(items: list[dict[str, object]]) -> str:
    parts: list[str] = []
    for idx, item in enumerate(items, start=1):
        snippet = _compact_text(item.get("text_snippet"), limit=240)
        parts.append(
            "attempt_page={idx} target_url={target_url} final_url={final_url} "
            "status={status} text_snippet={snippet!r}".format(
                idx=idx,
                target_url=item.get("target_url") or "",
                final_url=item.get("final_url") or "",
                status=item.get("status") or "unknown",
                snippet=snippet,
            )
        )
    return " | ".join(parts)


async def _fetch_plan_info_with_cookie(cookie_val: str) -> PlanInfo:
    from patchright.async_api import async_playwright

    diagnostics: list[dict[str, object]] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context()
            try:
                await ctx.add_cookies([{
                    "name": "WorkosCursorSessionToken",
                    "value": cookie_val,
                    "domain": "cursor.com",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                }])
                page = await ctx.new_page()
                for url in _plan_page_urls():
                    try:
                        response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        status = response.status if response is not None else "unknown"
                        try:
                            await page.wait_for_load_state("networkidle", timeout=12000)
                        except Exception:
                            pass
                        for _ in range(12):
                            text = str(await page.evaluate(_CURRENT_PLAN_TEXT_JS) or "")
                            info = extract_current_plan_info_from_text(text)
                            if info.status == "active":
                                log.info(f"套餐页面解析成功 url={page.url} status={status} amount={info.amount}")
                                return info
                            if info.status == "not_enabled":
                                log.info(f"套餐页面识别为未开通 url={page.url} status={status} reason={info.error}")
                                return info
                            await page.wait_for_timeout(1000)
                        snippet = _compact_text(text, limit=300)
                        diagnostics.append({
                            "target_url": url,
                            "final_url": page.url,
                            "status": status,
                            "text_snippet": snippet,
                        })
                        log.info(
                            f"套餐页面未解析到金额 url={page.url} status={status} text_snippet={snippet!r}"
                        )
                    except Exception as e:
                        diagnostics.append({
                            "target_url": url,
                            "final_url": getattr(page, "url", ""),
                            "status": "exception",
                            "text_snippet": f"{type(e).__name__}: {e}",
                        })
                        log.info(f"套餐页面解析跳过 url={url} error={type(e).__name__}: {e}")
                        continue
            finally:
                await ctx.close()
        finally:
            await browser.close()
    detail = _format_plan_diagnostics(diagnostics)
    if detail:
        log.info(f"套餐页面多次尝试仍无有效 Current Plan 文本，按未开通处理: {detail}")
    else:
        log.info("套餐页面诊断为空，按未开通处理")
    return PlanInfo(
        status="not_enabled",
        amount=None,
        error="消费页未解析到套餐信息，按未开通处理",
    )


def _spending_batch_max_parallel(account_count: int) -> int:
    concurrency = max(1, SETTINGS.spending_refresh_concurrency)
    active_limit_cfg = int(getattr(SETTINGS, "invoice_active_context_limit", 0))
    if active_limit_cfg > 0:
        return max(1, min(concurrency, account_count, active_limit_cfg))
    return max(1, min(concurrency, account_count))


async def _scrape_spending_panels_batch_async(
    accounts: list[Account],
    *,
    manager: "TokenManager",
    silent: bool,
    max_parallel: int,
    on_account: Optional[Callable[[str, int, int], None]] = None,
    on_result: Optional[Callable[[SpendingPanelBatchItem], None]] = None,
) -> list[SpendingPanelBatchItem]:
    """单 Chromium + 多 Context 并发解析消费页。"""
    import asyncio

    from patchright.async_api import async_playwright

    total = len(accounts)
    if total == 0:
        return []

    sem = asyncio.Semaphore(max_parallel)

    async def _one(acc: Account, browser, index: int) -> SpendingPanelBatchItem:
        email = (acc.email or "").strip().lower()
        if on_account:
            try:
                on_account(email, index, total)
            except Exception:
                pass
        async with sem:
            try:
                token = await asyncio.to_thread(manager.get_valid_token, acc)
                cookie_val, _ = _split_session_token(token)
                if not cookie_val:
                    raise RuntimeError("WorkosCursorSessionToken 为空，无法访问消费页")
                info = await _fetch_spending_panel_reuse_browser(
                    browser, cookie_val, silent=silent,
                )
                return SpendingPanelBatchItem(email=email, info=info)
            except Exception as e:
                return SpendingPanelBatchItem(
                    email=email,
                    error=f"{type(e).__name__}: {e}",
                )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            tasks = [asyncio.create_task(_one(acc, browser, idx)) for idx, acc in enumerate(accounts)]
            gathered: list[SpendingPanelBatchItem | BaseException] = []
            for fut in asyncio.as_completed(tasks):
                try:
                    item = await fut
                    gathered.append(item)
                    if isinstance(item, SpendingPanelBatchItem) and on_result:
                        try:
                            on_result(item)
                        except Exception:
                            pass
                except BaseException as e:
                    gathered.append(e)
        finally:
            await browser.close()

    out: list[SpendingPanelBatchItem] = []
    for item in gathered:
        if isinstance(item, SpendingPanelBatchItem):
            out.append(item)
        elif isinstance(item, BaseException):
            log.warning(f"[消费页批量] 任务异常: {item}")
    out.sort(key=lambda x: x.email.lower())
    return out


def fetch_spending_panels_batch(
    accounts: list[Account],
    *,
    manager: Optional["TokenManager"] = None,
    silent: bool = False,
    on_account: Optional[Callable[[str, int, int], None]] = None,
    on_result: Optional[Callable[[SpendingPanelBatchItem], None]] = None,
) -> list[SpendingPanelBatchItem]:
    """批量解析消费页（单 Chromium，并发受 SPENDING_REFRESH / INVOICE_ACTIVE 限制）。"""
    from .token_manager import get_default_manager

    if not accounts:
        return []
    mgr = manager or get_default_manager()
    max_parallel = _spending_batch_max_parallel(len(accounts))
    log.info(
        f"[消费页批量] accounts={len(accounts)} max_parallel={max_parallel} "
        f"SPENDING_REFRESH_CONCURRENCY={SETTINGS.spending_refresh_concurrency} "
        f"INVOICE_ACTIVE_CONTEXT_LIMIT={SETTINGS.invoice_active_context_limit}"
    )

    async def _run() -> list[SpendingPanelBatchItem]:
        return await _scrape_spending_panels_batch_async(
            accounts,
            manager=mgr,
            silent=silent,
            max_parallel=max_parallel,
            on_account=on_account,
            on_result=on_result,
        )

    with _PLAN_BROWSER_SEM:
        return _run_playwright_coroutine(_run())  # type: ignore[return-value]


async def _fetch_spending_panel_with_cookie(cookie_val: str, *, silent: bool = False) -> SpendingPanelInfo:
    from patchright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            return await _fetch_spending_panel_reuse_browser(browser, cookie_val, silent=silent)
        finally:
            await browser.close()


def fetch_spending_panel_from_dashboard(
    account: Account,
    *,
    manager: Optional["TokenManager"] = None,
    silent: bool = False,
) -> SpendingPanelInfo:
    """访问 Spending 页面，解析套餐档位名与 On-demand（按量）开关。

    ``silent=True`` 时不写 info 级解析日志（供定时静默任务使用）。
    """
    from .token_manager import get_default_manager

    mgr = manager or get_default_manager()
    token = mgr.get_valid_token(account)
    cookie_val, _ = _split_session_token(token)
    if not cookie_val:
        raise RuntimeError("WorkosCursorSessionToken 为空，无法访问消费页")
    with _PLAN_BROWSER_SEM:
        return _run_playwright_coroutine(_fetch_spending_panel_with_cookie(cookie_val, silent=silent))


def fetch_plan_info_from_dashboard(
    account: Account,
    *,
    manager: Optional["TokenManager"] = None,
) -> PlanInfo:
    """获取有效登录态后访问账号看板页面，解析 Current Plan 状态和金额。"""
    from .token_manager import get_default_manager

    mgr = manager or get_default_manager()
    token = mgr.get_valid_token(account)
    cookie_val, _ = _split_session_token(token)
    if not cookie_val:
        raise RuntimeError("WorkosCursorSessionToken 为空，无法访问账号看板")
    with _PLAN_BROWSER_SEM:
        return _run_playwright_coroutine(_fetch_plan_info_with_cookie(cookie_val))


def fetch_plan_amount_from_dashboard(
    account: Account,
    *,
    manager: Optional["TokenManager"] = None,
) -> Decimal:
    """获取有效登录态后访问账号看板页面，解析 Current Plan 金额。"""
    info = fetch_plan_info_from_dashboard(account, manager=manager)
    if info.status == "active" and info.amount is not None:
        return info.amount
    raise RuntimeError(info.error or f"套餐状态异常: {info.status}")
