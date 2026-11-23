"""Devin 用量/套餐解析：纯函数，不依赖浏览器，便于单测。

两条来源互为兜底：
1. ``extract_from_api``：解析页面自己发出的 api.devin.ai JSON（字段名来自前端 bundle：
   usedPercent / resetAt / overageBalanceDollars / weeklyQuotaResetAtUnix ...）
2. ``parse_usage_text``：解析 /settings/usage、/settings/plans 页面可见文本
   （"Current plan / Max / $200/month / Renews on ... / Weekly quota N% used / Resets in N days ..."）

合并规则见 ``merge_results``：API 有值优先，其余用文本补齐。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONEY = r"\$\s*([\d,]+(?:\.\d+)?)"
_MONTH_NAME = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE_PATTERNS = (
    re.compile(rf"{_MONTH_NAME}\.?\s+(\d{{1,2}})(?!\d)(?:st|nd|rd|th)?(?:,?\s*(\d{{4}}))?", re.I),
    re.compile(rf"(\d{{1,2}})\s+{_MONTH_NAME}\.?(?:,?\s*(\d{{4}}))?", re.I),
    re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"),
    re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})"),
)
_PLAN_BLACKLIST = (
    "manage", "upgrade", "change", "view", "billing", "renews", "cancel", "contact",
    "learn", "usage", "current", "per", "month", "year", "plan", "quota", "resets",
    "balance", "on-demand", "on demand", "invoice", "payment", "settings",
)


def _to_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", ""))
    except ValueError:
        return None


def parse_date_text(text: str, *, now: datetime | None = None) -> str:
    """把 'December 13, 2026' / '2026-12-13' / '12/13/2026' 等转成 YYYY-MM-DD；失败返回空串。"""
    if not text:
        return ""
    now = now or datetime.now(timezone.utc)
    m = _DATE_PATTERNS[0].search(text)
    if m:
        month = _MONTHS.get(m.group(1).lower()[:3])
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else None
        return _build_date(year, month, day, now)
    m = _DATE_PATTERNS[1].search(text)
    if m:
        day = int(m.group(1))
        month = _MONTHS.get(m.group(2).lower()[:3])
        year = int(m.group(3)) if m.group(3) else None
        return _build_date(year, month, day, now)
    m = _DATE_PATTERNS[2].search(text)
    if m:
        return _build_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), now)
    m = _DATE_PATTERNS[3].search(text)
    if m:
        year = int(m.group(3))
        year += 2000 if year < 100 else 0
        return _build_date(year, int(m.group(1)), int(m.group(2)), now)
    return ""


def _build_date(year: int | None, month: int | None, day: int, now: datetime) -> str:
    if not month:
        return ""
    try:
        if year is None:
            year = now.year
            candidate = datetime(year, month, day)
            if candidate.date() < (now - timedelta(days=1)).date():
                candidate = datetime(year + 1, month, day)
            return candidate.strftime("%Y-%m-%d")
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def parse_reset_seconds(text: str) -> int | None:
    """'Resets in 5 days 3 hours' → 秒数；无法解析返回 None。"""
    m = re.search(r"resets?\s+in\s+([^\n.]{1,60})", text, re.I)
    if not m:
        return None
    seg = m.group(1)
    total = 0
    found = False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(d(?:ays?)?|h(?:ours?|rs?)?|m(?:in(?:ute)?s?)?|w(?:eeks?)?)\b", seg, re.I):
        found = True
        n = float(num)
        u = unit.lower()
        if u.startswith("w"):
            total += n * 7 * 86400
        elif u.startswith("d"):
            total += n * 86400
        elif u.startswith("h"):
            total += n * 3600
        else:
            total += n * 60
    return int(total) if found else None


def _percent_near(text: str, label_re: str, span: int = 260) -> tuple[float | None, int | None]:
    """在标签后 span 个字符内找 'N% used'（或 'N% remaining'），并返回 reset 相对秒数。"""
    m = re.search(label_re, text, re.I)
    if not m:
        return None, None
    window = text[m.start(): m.start() + span]
    used = re.search(r"(\d+(?:\.\d+)?)\s*%\s*used", window, re.I)
    pct: float | None = None
    if used:
        pct = float(used.group(1))
    else:
        remain = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:remaining|left)", window, re.I)
        if remain:
            pct = round(100 - float(remain.group(1)), 4)
    return pct, parse_reset_seconds(window)


def _plan_name_from_lines(lines: list[str]) -> str:
    for idx, line in enumerate(lines):
        if re.search(r"current\s+plan", line, re.I):
            inline = re.sub(r"(?i).*current\s+plan\s*[:\-]?\s*", "", line).strip()
            candidates = ([inline] if inline else []) + lines[idx + 1: idx + 7]
            for cand in candidates:
                name = re.sub(r"(?i)\s+plan$", "", cand).strip()
                if not name or name.startswith("$") or len(name) > 30:
                    continue
                low = name.lower()
                if any(low == b or low.startswith(b + " ") for b in _PLAN_BLACKLIST):
                    continue
                if re.search(r"\d{3,}", name):
                    continue
                return name
    return ""


_PRICE_RE = re.compile(_MONEY + r"\s*/?\s*(month|mo|year|yr|annually|monthly)\b", re.I)


def _current_plan_price(lines: list[str], plan: str) -> tuple[float, str] | None:
    """只取“当前套餐”那张卡片里的价格，绝不取页面上别的套餐卡（Pro $20 / Teams $80 …）。

    1) "Current plan" 标签之后的几行里找 `$N/month`；
    2) 没有标签时，找“套餐名独占一行 + 紧随其后的价格行”；
    3) 都找不到就不返回，宁缺毋错。
    """
    def pick(chunk: str) -> tuple[float, str] | None:
        m = _PRICE_RE.search(chunk)
        if not m:
            return None
        val = _to_float(m.group(1))
        if val is None:
            return None
        return val, ("year" if m.group(2).lower().startswith(("y", "a")) else "month")

    for idx, line in enumerate(lines):
        if re.search(r"current\s+plan", line, re.I):
            found = pick("\n".join(lines[idx: idx + 5]))
            if found:
                return found
    if plan:
        for idx, line in enumerate(lines):
            if line.strip().lower() == plan.lower():
                found = pick("\n".join(lines[idx + 1: idx + 3]))
                if found:
                    return found
    return None


_RENEW_RE = re.compile(
    r"(?:renews?|renewal(?:\s+date)?|next\s+(?:billing|payment|invoice)(?:\s+date)?|"
    r"billing\s+(?:period|cycle)\s+(?:ends|end\s+date))\s*(?:on|date|at)?\s*[:\-]?\s*([^\n]{3,40})",
    re.I,
)
# 已设置取消/到期的套餐没有 “Renews on”，页面会写 “Ends on / Expires on / Cancels on”
_END_RE = re.compile(
    r"\b(?:ends|expires|cancels|valid\s+until|active\s+until)\s*(?:on|at)?\s*[:\-]?\s*([^\n]{3,40})",
    re.I,
)
_PERIOD_LABEL_RE = re.compile(r"billing\s+(?:period|cycle)|current\s+period", re.I)
_RANGE_SPLIT_RE = re.compile(r"\s*[–—]\s*|\s+-\s+|\s+(?:to|through)\s+", re.I)


def _range_dates(segment: str, now: datetime) -> tuple[str, str]:
    """'Nov 13 – Dec 13, 2026' / 'Nov 13, 2026 to Dec 13, 2026' → (起, 止)；任一端失败返回空串。"""
    parts = [p for p in _RANGE_SPLIT_RE.split(segment.strip(), maxsplit=1) if p.strip()]
    if len(parts) != 2:
        return "", ""
    end = parse_date_text(parts[1], now=now)
    if not end:
        return "", ""
    start_text = parts[0]
    if not re.search(r"\d{4}", start_text):  # 起点常省略年份：沿用终点的年，跨年则减一
        end_dt = datetime.strptime(end, "%Y-%m-%d")
        probe = parse_date_text(f"{start_text} {end_dt.year}", now=now)
        if probe and probe > end:
            probe = parse_date_text(f"{start_text} {end_dt.year - 1}", now=now)
        start = probe
    else:
        start = parse_date_text(start_text, now=now)
    if not start or start >= end:
        return "", ""
    return start, end


def parse_billing_period(text: str, *, now: datetime | None = None) -> dict[str, Any]:
    """从页面文本找账期：优先 “Billing period A – B” 区间（起止都有），其次 “Renews on / Next billing date”，
    最后才是 “Ends/Expires/Cancels on”（已取消的套餐）。只返回识别到的 ``renews_at`` / ``cycle_start``。"""
    now = now or datetime.now(timezone.utc)
    out: dict[str, Any] = {}
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for idx, line in enumerate(lines):
        if not _PERIOD_LABEL_RE.search(line):
            continue
        # 标签和日期区间可能同一行，也可能在下一行
        for chunk in (_PERIOD_LABEL_RE.split(line, maxsplit=1)[-1], *(lines[idx + 1: idx + 2])):
            start, end = _range_dates(chunk.lstrip(" :\t"), now)
            if start and end:
                return {"cycle_start": start, "renews_at": end}
    flat = "\n".join(lines)
    for regex in (_RENEW_RE, _END_RE):
        for m in regex.finditer(flat):
            date = parse_date_text(m.group(1), now=now)
            if date:
                out["renews_at"] = date
                return out
    return out


def parse_usage_text(text: str, *, now: datetime | None = None) -> dict[str, Any]:
    """解析页面可见文本，只返回识别到的字段。"""
    now = now or datetime.now(timezone.utc)
    out: dict[str, Any] = {}
    if not text:
        return out
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    flat = "\n".join(lines)

    plan = _plan_name_from_lines(lines)
    if not plan:
        m = re.search(r"\b(Free|Core|Teams?|Pro|Max|Enterprise)\s+plan\b", flat, re.I)
        if m:
            plan = m.group(1).title()
    if plan:
        out["plan_name"] = plan

    price_m = _current_plan_price(lines, plan)
    if price_m:
        out["plan_price_usd"] = price_m[0]
        out["billing_period"] = price_m[1]

    out.update(parse_billing_period(flat, now=now))

    for key, label_re in (("weekly", r"weekly(?:\s+quota|\s+usage|\s+limit)?"), ("daily", r"daily(?:\s+quota|\s+usage|\s+limit)?")):
        pct, reset_sec = _percent_near(flat, label_re)
        if pct is not None:
            out[f"{key}_used_pct"] = pct
            if reset_sec is not None:
                out[f"{key}_reset_at"] = int(now.timestamp()) + reset_sec

    # 不能把“Purchase on-demand usage”按钮文案后面的套餐价格（$20/month）当成按需已用；
    # 标签与金额之间最多隔两行，避免跨区块误取。
    m = re.search(r"(?<!purchase )on[-\s]?demand\s+usage(?:[^$\n]*\n){0,2}[^$\n]*" + _MONEY, flat, re.I) or re.search(
        r"on[-\s]?demand[^$]{0,40}spent[^$]{0,20}" + _MONEY, flat, re.I
    )
    if m:
        out["on_demand_used_usd"] = _to_float(m.group(1))

    m = re.search(r"(?:remaining\s+)?balance[^$]{0,40}" + _MONEY, flat, re.I)
    if m:
        out["on_demand_balance_usd"] = _to_float(m.group(1))

    m = re.search(r"(?:message\s+)?usage\s+limit[^$]{0,80}" + _MONEY, flat, re.I)
    if m:
        out["message_limit_usd"] = _to_float(m.group(1))

    return out


# ─── API JSON ──────────────────────────────────────────────────────

def _parse_time_value(value: Any) -> int | None:
    """epoch 秒/毫秒 或 ISO 字符串 → epoch 秒。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:
            return None
        return int(v / 1000) if v > 1e11 else int(v)
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return _parse_time_value(int(s))
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return None


def _walk(node: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], dict]]:
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from _walk(v, path + (str(k),))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, path + (str(i),))


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return _to_float(value)
    return None


def extract_from_api(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """payloads: [{"url": str, "body": <json>}]，返回识别到的字段。"""
    out: dict[str, Any] = {}

    def put(key: str, value: Any) -> None:
        if value is not None and value != "" and key not in out:
            out[key] = value

    for item in payloads:
        url = str(item.get("url") or "").lower()
        body = item.get("body")
        for path, node in _walk(body):
            joined = ".".join(path).lower()
            window = "weekly" if "weekly" in joined else "daily" if "daily" in joined else ""
            used = node.get("usedPercent", node.get("used_percent"))
            if used is not None and window:
                put(f"{window}_used_pct", _num(used))
                put(f"{window}_reset_at", _parse_time_value(node.get("resetAt", node.get("reset_at"))))
            for key, window_name in (("weeklyQuotaResetAtUnix", "weekly"), ("dailyQuotaResetAtUnix", "daily")):
                if key in node:
                    put(f"{window_name}_reset_at", _parse_time_value(node.get(key)))
            for key, window_name in (("weeklyQuotaRemainingPercent", "weekly"), ("dailyQuotaRemainingPercent", "daily")):
                remain = _num(node.get(key))
                if remain is not None:
                    put(f"{window_name}_used_pct", round(100 - remain, 4))
            bal = _num(node.get("overageBalanceDollars"))
            if bal is not None:
                put("on_demand_balance_usd", bal)
            if "on-demand" in url or "on_demand" in url or "ondemand" in joined:
                consumed = _num(node.get("dollarsConsumed"))
                if consumed is not None:
                    put("on_demand_used_usd", consumed)
            for key in ("planName", "plan_name", "planDisplayName", "tierName"):
                val = node.get(key)
                if isinstance(val, str) and val.strip():
                    put("plan_name", val.strip())
            if "invoice" in url or "invoice" in joined:
                continue  # 发票对象里的 period_end 是历史账期，不是当前账期
            for target, keys in (("renews_at", _PERIOD_END_KEYS), ("cycle_start", _PERIOD_START_KEYS)):
                for key in keys:
                    ts = _parse_time_value(node.get(key))
                    if ts:
                        put(target, datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"))
                        break
    return out


_PERIOD_END_KEYS = (
    "currentPeriodEnd", "current_period_end", "nextBillingDate", "next_billing_date", "nextBillingAt",
    "renewsAt", "renews_at", "renewalDate", "renewal_date", "billingCycleEnd", "billing_cycle_end",
    "subscriptionEnd", "periodEnd", "period_end",
)
_PERIOD_START_KEYS = (
    "currentPeriodStart", "current_period_start", "billingCycleStart", "billing_cycle_start",
    "periodStart", "period_start",
)


def merge_results(api: dict[str, Any], text: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """API 优先、文本补齐；返回 (数据, 来源标记 api / dom / api+dom / "")。"""
    merged = dict(text)
    used_api = bool(api)
    for key, val in api.items():
        if val is not None:
            merged[key] = val
    # 账期日期以页面可见为准：API 时间戳是 UTC，转成日期可能与页面（本地日期）和发票差一天
    for key in ("renews_at", "cycle_start"):
        if text.get(key):
            merged[key] = text[key]
    used_dom = any(key not in api for key in text)
    source = "+".join(x for x, flag in (("api", used_api), ("dom", used_dom)) if flag)
    return merged, source


def has_core_data(data: dict[str, Any]) -> bool:
    """判定采集是否成功：至少拿到套餐名或任一用量数字。"""
    return bool(
        data.get("plan_name")
        or data.get("weekly_used_pct") is not None
        or data.get("daily_used_pct") is not None
        or data.get("on_demand_used_usd") is not None
        or data.get("on_demand_balance_usd") is not None
    )


# ─── 账期 ──────────────────────────────────────────────────────────

def _add_months(d: datetime, months: int) -> datetime:
    """按自然月加减，日期超出当月天数时取月末（3/31 - 1 月 = 2/28）。"""
    idx = d.year * 12 + (d.month - 1) + months
    year, month = divmod(idx, 12)
    month += 1
    day = d.day
    while day > 28:
        try:
            return d.replace(year=year, month=month, day=day)
        except ValueError:
            day -= 1
    return d.replace(year=year, month=month, day=day)


def cycle_start_from_end(cycle_end: str, billing_period: str = "month") -> str:
    """账期起点 = 续费日（账期终点）往前推一个计费周期；输入输出均为 YYYY-MM-DD，非法返回空串。"""
    try:
        end = datetime.strptime(cycle_end, "%Y-%m-%d")
    except (TypeError, ValueError):
        return ""
    months = 12 if (billing_period or "").lower() in ("year", "annual", "yearly") else 1
    return _add_months(end, -months).strftime("%Y-%m-%d")


# ─── 发票 ──────────────────────────────────────────────────────────

_INVOICE_STATUSES = (
    "paid", "open", "void", "draft", "uncollectible", "failed", "refunded",
    "pending", "past due", "unpaid", "overdue", "processing", "canceled", "cancelled",
)
_INVOICE_ROW_RE = re.compile(
    rf"(?P<date>{_MONTH_NAME}\.?\s+\d{{1,2}},?\s+\d{{4}})\s+"
    rf"(?P<status>{'|'.join(_INVOICE_STATUSES)})\s+"
    r"(?P<amount>-?\s*\$\s*[\d,]+(?:\.\d+)?|\$\s*-\s*[\d,]+(?:\.\d+)?)",
    re.I,
)


def parse_invoices_text(text: str) -> list[dict[str, Any]]:
    """解析发票页可见文本里的表格行：``Nov 13, 2026  Paid  $213.20`` → 列表（页面顺序）。"""
    out: list[dict[str, Any]] = []
    for m in _INVOICE_ROW_RE.finditer(text or ""):
        date = parse_date_text(m.group("date"))
        amount_text = m.group("amount").replace(" ", "")
        negative = "-" in amount_text
        amount = _to_float(amount_text.replace("-", "").replace("$", ""))
        if not date or amount is None:
            continue
        out.append({
            "invoice_date": date,
            "status": m.group("status").strip().title(),
            "amount_usd": -amount if negative else amount,
        })
    return out


def _first(node: dict, *keys: str) -> Any:
    for k in keys:
        if k in node and node[k] not in (None, ""):
            return node[k]
    return None


def extract_invoices_from_api(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从页面自己的 API 响应里找发票对象（Stripe 风格：hosted_invoice_url / invoice_pdf / number / created）。

    只用来补充文本里拿不到的字段（发票链接、PDF、发票号）；金额以页面可见文本为准。
    """
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payloads:
        for _path, node in _walk(item.get("body")):
            link = _first(node, "hosted_invoice_url", "hostedInvoiceUrl", "invoice_url", "invoiceUrl", "url")
            pdf = _first(node, "invoice_pdf", "invoicePdf", "pdf_url", "pdfUrl", "pdf")
            number = _first(node, "number", "invoice_number", "invoiceNumber")
            inv_id = _first(node, "id", "invoice_id", "invoiceId")
            if not (pdf or (link and isinstance(link, str) and "http" in link and ("invoice" in link or "stripe" in link))):
                continue
            ts = _parse_time_value(_first(node, "created", "date", "invoice_date", "invoiceDate", "period_end", "createdAt"))
            date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else ""
            key = str(inv_id or number or link or pdf)
            if key in seen:
                continue
            seen.add(key)
            amount = None
            dollars = _num(_first(node, "amountDollars", "amount_dollars"))
            if dollars is not None:
                amount = dollars
            else:
                cents = _first(node, "amount_paid", "amountPaid", "total", "amount_due")
                if isinstance(cents, int) and not isinstance(cents, bool):
                    amount = cents / 100
            found.append({
                "invoice_date": date,
                "status": str(_first(node, "status") or "").title(),
                "amount_usd": amount,
                "number": str(number or ""),
                "invoice_url": str(link or "") if isinstance(link, str) and link.startswith("http") else "",
                "pdf_url": str(pdf or "") if isinstance(pdf, str) and pdf.startswith("http") else "",
            })
    return found


def merge_invoices(
    text_rows: list[dict[str, Any]],
    api_rows: list[dict[str, Any]],
    links: list[str] | None = None,
) -> list[dict[str, Any]]:
    """合并：页面文本行为主（金额/状态以可见为准）；按日期匹配 API 补发票号/PDF/链接；
    ``links`` 是页面里 "View invoice" 的 href（与文本行同序）。无文本行时回退到 API 行。"""
    links = list(links or [])
    pool: dict[str, list[dict[str, Any]]] = {}
    for a in api_rows:
        pool.setdefault(a.get("invoice_date", ""), []).append(a)
    merged: list[dict[str, Any]] = []
    for idx, row in enumerate(text_rows):
        item = {"number": "", "invoice_url": "", "pdf_url": "", **row}
        bucket: list[dict[str, Any]] = []
        for shift in (0, -1, 1):  # API 时间戳是 UTC，页面显示本地日期，允许差一天
            try:
                day = (datetime.strptime(item["invoice_date"], "%Y-%m-%d") + timedelta(days=shift)).strftime("%Y-%m-%d")
            except ValueError:
                break
            bucket = pool.get(day) or []
            if bucket:
                break
        if bucket:
            api = bucket.pop(0)
            for k in ("number", "invoice_url", "pdf_url"):
                item[k] = item.get(k) or api.get(k, "")
        if not item["invoice_url"] and idx < len(links) and links[idx]:
            item["invoice_url"] = links[idx]
        merged.append(item)
    if not text_rows:
        for a in api_rows:
            merged.append({"number": "", "invoice_url": "", "pdf_url": "", **a})
    return merged


_STRIPE_HOSTED_RE = re.compile(r"^https://invoice\.stripe\.com/i/(acct_[^/?#]+)/([^/?#]+)(\?[^#]*)?")


def stripe_pdf_url(hosted_url: str) -> str:
    """Stripe 托管发票页 → 直链 PDF（invoice.stripe.com/i/<acct>/<id> → pay.stripe.com/invoice/<acct>/<id>/pdf）。
    不是 Stripe 托管页返回空串；是否真是 PDF 由调用方校验 ``%PDF`` 头。"""
    m = _STRIPE_HOSTED_RE.match(hosted_url or "")
    if not m:
        return ""
    return f"https://pay.stripe.com/invoice/{m.group(1)}/{m.group(2)}/pdf{m.group(3) or ''}"


def cycle_from_invoices(
    rows: list[dict[str, Any]],
    *,
    billing_period: str = "month",
    plan_price: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """页面读不到账期时的兜底：续费发票日期 = 本账期起点，终点 = 起点 + 一个计费周期。

    Devin 按订阅周期开票，所以最近一张“续费”发票的日期就是当前账期的起点。
    有套餐价格时只看金额 ≥ 套餐价的已支付发票（排除按需充值的小额发票）。
    推算出的终点已经过去（订阅可能已停）时不返回，宁缺毋错。
    """
    now = now or datetime.now(timezone.utc)
    paid = [r for r in rows if (r.get("status") or "").lower() == "paid" and r.get("invoice_date")]
    if plan_price:
        big = [r for r in paid if (r.get("amount_usd") or 0) >= plan_price * 0.9]
        paid = big or paid
    if not paid:
        return {}
    start = max(r["invoice_date"] for r in paid)
    end = cycle_start_from_end_forward(start, billing_period)
    if not end or end < (now - timedelta(days=1)).strftime("%Y-%m-%d"):
        return {}
    return {"cycle_start": start, "renews_at": end}


def cycle_start_from_end_forward(cycle_start: str, billing_period: str = "month") -> str:
    """账期起点 + 一个计费周期 = 终点（cycle_start_from_end 的反向）。"""
    try:
        start = datetime.strptime(cycle_start, "%Y-%m-%d")
    except (TypeError, ValueError):
        return ""
    months = 12 if (billing_period or "").lower() in ("year", "annual", "yearly") else 1
    return _add_months(start, months).strftime("%Y-%m-%d")


def missing_fields(data: dict[str, Any]) -> list[str]:
    """按 Devin 的额度模型判断还缺哪些核心项（中文名，给页面/日志用）。

    Free 套餐没有续费日和固定周额度，不按缺失处理。
    """
    plan = (data.get("plan_name") or "").strip().lower()
    if not plan:
        return ["套餐"]
    if plan == "free":
        return []
    missing: list[str] = []
    if not data.get("renews_at"):
        missing.append("账期")
    if data.get("weekly_used_pct") is None:
        missing.append("周额度")
    if data.get("on_demand_used_usd") is None and data.get("on_demand_balance_usd") is None:
        missing.append("按需用量")
    return missing
