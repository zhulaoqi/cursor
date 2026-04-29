"""导出 AccountSnapshot → JSON / CSV / XLSX / PDF(发票)。"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
from urllib.parse import urlsplit, urlunsplit
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .api_client import CursorClient
from .config import SETTINGS
from .logger import get
from .models import Account, AccountSnapshot
from .token_manager import TokenManager, get_default_manager

log = get("export")


def _parse_usage_csv(csv_text: str) -> tuple[list[str], list[list[str]]]:
    """解析使用明细 CSV，返回 (headers, rows)。
    headers：原始列名，原样保留。
    rows：每行的字段值列表。
    """
    reader = csv.reader(io.StringIO(csv_text.strip()))
    raw_rows = list(reader)
    if not raw_rows:
        return [], []
    headers = raw_rows[0]
    data_rows = raw_rows[1:]
    return headers, data_rows


# ═══════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════

def _fmt_ts_ms(v: Any) -> str:
    """毫秒时间戳 → 'YYYY-MM-DD HH:MM:SS'；非法返回原值。"""
    if v in (None, "", 0):
        return ""
    try:
        ms = int(v)
        if ms > 10**12:  # 毫秒
            return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
        if ms > 10**9:   # 秒
            return datetime.fromtimestamp(ms).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        pass
    return str(v)


def _cents_to_usd(v: Any) -> Any:
    try:
        return round(float(v) / 100.0, 4)
    except (TypeError, ValueError):
        return v


def _dig(d: dict, *path, default=None):
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _safe_filename(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]+", "_", s)


def export_json(snapshots: Iterable[AccountSnapshot], out_dir: Path) -> list[Path]:
    """每账号写一份 JSON 到 out_dir/{email}.json。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for snap in snapshots:
        p = out_dir / f"{_safe_filename(snap.email)}.json"
        p.write_text(
            json.dumps(asdict(snap), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        paths.append(p)
    log.info(f"JSON 已写入 {out_dir}（{len(paths)} 个账号）")
    return paths


def _plan_name(plan: dict) -> str:
    if not isinstance(plan, dict):
        return ""
    for key in ("name", "planName", "plan", "subscription"):
        v = plan.get(key)
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            for k2 in ("name", "planName"):
                if v.get(k2):
                    return str(v[k2])
    return ""


def _usage_summary(usage: dict) -> dict[str, Any]:
    """从 GetCurrentPeriodUsage 里提取常用字段（结构可能变，尽量多拿）。"""
    if not isinstance(usage, dict):
        return {}
    flat: dict[str, Any] = {}
    for k in ("periodStart", "periodEnd", "startDate", "endDate"):
        if k in usage:
            flat[k] = usage[k]
    for k, v in usage.items():
        if isinstance(v, (int, float, str, bool)):
            flat[k] = v
    return flat


def export_csv(snapshots: list[AccountSnapshot], out_path: Path) -> Path:
    """
    汇总每账号一行：email, plan, period, 各项配额 / 用量 / 错误摘要。
    列是动态的（按 usage / plan 里出现的标量字段并集），但固定几列在前。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    for snap in snapshots:
        row: dict[str, Any] = {
            "email": snap.email,
            "fetched_at": snap.fetched_at,
            "plan": _plan_name(snap.plan),
            "errors": ",".join(snap.errors.keys()) if snap.errors else "",
        }
        for k, v in _usage_summary(snap.usage).items():
            row[f"usage.{k}"] = v
        # usage_limit 顶层标量
        if isinstance(snap.usage_limit, dict):
            for k, v in snap.usage_limit.items():
                if isinstance(v, (int, float, str, bool)):
                    row[f"limit.{k}"] = v
        # stripe 订阅状态
        if isinstance(snap.stripe, dict):
            for k, v in snap.stripe.items():
                if isinstance(v, (int, float, str, bool)):
                    row[f"stripe.{k}"] = v
        row["invoice_count"] = len(snap.invoices) if snap.invoices else 0
        rows.append(row)

    all_keys: list[str] = []
    seen = set()
    preferred = ["email", "fetched_at", "plan", "invoice_count", "errors"]
    for k in preferred:
        if k not in seen:
            all_keys.append(k); seen.add(k)
    for row in rows:
        for k in row.keys():
            if k not in seen:
                all_keys.append(k); seen.add(k)

    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in all_keys})

    log.info(f"CSV 汇总已写入 {out_path}（{len(rows)} 行，{len(all_keys)} 列）")
    return out_path


# ═══════════════════════════════════════════════════════════════════
# XLSX 导出（多 sheet：概览 / 使用明细 / 发票）
# ═══════════════════════════════════════════════════════════════════

# 账号概览 sheet 固定列顺序
_OVERVIEW_COLUMNS: list[tuple[str, str]] = [
    ("email",                          "邮箱"),
    ("plan",                           "套餐"),
    ("fetched_at_str",                 "抓取时间"),
    ("stripe.membershipType",          "会员类型"),
    ("stripe.subscriptionStatus",      "订阅状态"),
    ("stripe.isYearlyPlan",            "是否年付"),
    ("stripe.trialEligible",           "试用资格"),
    ("stripe.lastPaymentFailed",       "上次付款失败"),
    ("stripe.pendingCancellationDate", "待取消日期"),
    ("stripe.paymentId",               "Stripe Customer"),
    ("usage.billingCycleStart_str",    "计费周期开始"),
    ("usage.billingCycleEnd_str",      "计费周期结束"),
    ("usage.totalSpend_usd",           "已用 (USD)"),
    ("usage.includedSpend_usd",        "套餐内已用 (USD)"),
    ("usage.remaining_usd",            "剩余 (USD)"),
    ("usage.limit_usd",                "套餐额度 (USD)"),
    ("usage.totalPercentUsed",         "总使用率 %"),
    ("usage.autoPercentUsed",          "Auto 使用率 %"),
    ("usage.apiPercentUsed",           "API 使用率 %"),
    ("usage_events_count",             "使用事件数"),
    ("invoice_count",                  "发票数"),
    ("errors",                         "错误字段"),
]

_EVENT_COLUMNS: list[tuple[str, str]] = [
    ("timestamp_str",      "时间"),
    ("model",              "模型"),
    ("kind",               "类型"),
    ("requestsCosts",      "请求数"),
    ("usageBasedCosts",    "按量计费"),
    ("isTokenBasedCall",   "Token计费"),
    ("isChargeable",       "计费"),
    ("isHeadless",         "Headless"),
    ("inputTokens",        "输入Tokens"),
    ("outputTokens",       "输出Tokens"),
    ("cacheWriteTokens",   "缓存写入Tokens"),
    ("cacheReadTokens",    "缓存读取Tokens"),
    ("totalCents_usd",     "总成本(USD)"),
    ("chargedCents_usd",   "扣费(USD)"),
    ("discountPercentOff", "折扣%"),
    ("cursorTokenFee",     "CursorTokenFee"),
    ("owningUser",         "所属用户"),
]

_INVOICE_COLUMNS: list[tuple[str, str]] = [
    ("email",       "邮箱"),
    ("number",      "账单号"),
    ("id",          "账单ID"),
    ("status",      "状态"),
    ("created_str", "账单日期"),
    ("period_str",  "账期"),
    ("total_usd",   "金额(USD)"),
    ("currency",    "币种"),
    ("pdf_file",    "PDF文件"),
    ("pdf_url",     "PDF链接"),
    ("hosted_url",  "在线查看"),
]


def _overview_row(snap: AccountSnapshot) -> dict[str, Any]:
    usage = snap.usage or {}
    pu = usage.get("planUsage") or {}
    stripe = snap.stripe or {}
    row = {
        "email": snap.email,
        "plan": _plan_name(snap.plan) or stripe.get("individualMembershipType") or "",
        "fetched_at_str": _fmt_ts_ms(snap.fetched_at * 1000) if snap.fetched_at else "",
        "stripe.membershipType":        stripe.get("membershipType", ""),
        "stripe.subscriptionStatus":    stripe.get("subscriptionStatus", ""),
        "stripe.isYearlyPlan":          stripe.get("isYearlyPlan", ""),
        "stripe.trialEligible":         stripe.get("trialEligible", ""),
        "stripe.lastPaymentFailed":     stripe.get("lastPaymentFailed", ""),
        "stripe.pendingCancellationDate": stripe.get("pendingCancellationDate", ""),
        "stripe.paymentId":             stripe.get("paymentId", ""),
        "usage.billingCycleStart_str":  _fmt_ts_ms(usage.get("billingCycleStart")),
        "usage.billingCycleEnd_str":    _fmt_ts_ms(usage.get("billingCycleEnd")),
        "usage.totalSpend_usd":         _cents_to_usd(pu.get("totalSpend")),
        "usage.includedSpend_usd":      _cents_to_usd(pu.get("includedSpend")),
        "usage.remaining_usd":          _cents_to_usd(pu.get("remaining")),
        "usage.limit_usd":              _cents_to_usd(pu.get("limit")),
        "usage.totalPercentUsed":       pu.get("totalPercentUsed"),
        "usage.autoPercentUsed":        pu.get("autoPercentUsed"),
        "usage.apiPercentUsed":         pu.get("apiPercentUsed"),
        "usage_events_count":           len(snap.usage_events or []),
        "invoice_count":                len(snap.invoices or []),
        "errors":                       ",".join(sorted(snap.errors.keys())) if snap.errors else "",
    }
    return row


def _event_rows(snap: AccountSnapshot) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ev in snap.usage_events or []:
        if not isinstance(ev, dict):
            continue
        tu = ev.get("tokenUsage") or {}
        rows.append({
            "email":             snap.email,
            "timestamp_str":     _fmt_ts_ms(ev.get("timestamp")),
            "model":             ev.get("model", ""),
            "kind":              ev.get("kind", ""),
            "requestsCosts":     ev.get("requestsCosts"),
            "usageBasedCosts":   ev.get("usageBasedCosts"),
            "isTokenBasedCall":  ev.get("isTokenBasedCall"),
            "isChargeable":      ev.get("isChargeable"),
            "isHeadless":        ev.get("isHeadless"),
            "inputTokens":       tu.get("inputTokens"),
            "outputTokens":      tu.get("outputTokens"),
            "cacheWriteTokens":  tu.get("cacheWriteTokens"),
            "cacheReadTokens":   tu.get("cacheReadTokens"),
            "totalCents_usd":    _cents_to_usd(tu.get("totalCents")),
            "chargedCents_usd":  _cents_to_usd(ev.get("chargedCents")),
            "discountPercentOff": tu.get("discountPercentOff"),
            "cursorTokenFee":    ev.get("cursorTokenFee"),
            "owningUser":        ev.get("owningUser", ""),
        })
    return rows


def _invoice_rows(snap: AccountSnapshot, pdf_files: Optional[dict] = None) -> list[dict[str, Any]]:
    """pdf_files: {inv_number_or_id: filename} 记录实际下载的 PDF 文件名。"""
    rows: list[dict[str, Any]] = []
    for inv in snap.invoices or []:
        if not isinstance(inv, dict):
            continue
        total = inv.get("total") or inv.get("amount_due") or inv.get("amount_paid")
        created = inv.get("created") or inv.get("createdAt")
        created_ms = created * 1000 if isinstance(created, (int, float)) and created < 10**12 else created
        # 账期：优先用 period_start（账单周期开始），比 created 更能代表"哪个月的账单"
        period_start = inv.get("period_start") or inv.get("periodStart")
        period_end   = inv.get("period_end")   or inv.get("periodEnd")
        if period_start:
            ps_ms = period_start * 1000 if isinstance(period_start, (int, float)) and period_start < 10**12 else period_start
            pe_ms = period_end   * 1000 if isinstance(period_end,   (int, float)) and period_end   < 10**12 else period_end
            period_str = _fmt_ts_ms(ps_ms)[:10]  # 只取日期部分
            if pe_ms:
                period_str += f" ~ {_fmt_ts_ms(pe_ms)[:10]}"
        else:
            period_str = ""
        inv_id  = inv.get("id")     or inv.get("invoiceId")    or ""
        inv_num = inv.get("number") or inv.get("invoiceNumber") or ""
        pdf_key = inv_num or str(inv_id)
        pdf_file = (pdf_files or {}).get(pdf_key, "")
        rows.append({
            "email":       snap.email,
            "id":          inv_id,
            "number":      inv_num,
            "status":      inv.get("status", ""),
            "created_str": _fmt_ts_ms(created_ms),
            "period_str":  period_str,
            "total_usd":   _cents_to_usd(total) if isinstance(total, (int, float)) else total,
            "currency":    inv.get("currency", ""),
            "pdf_file":    pdf_file,
            "pdf_url":     inv.get("invoice_pdf") or inv.get("invoicePdf") or inv.get("pdf") or "",
            "hosted_url":  inv.get("hosted_invoice_url") or inv.get("hostedInvoiceUrl") or "",
        })
    return rows


def _apply_xlsx_style(wb_obj) -> dict:
    """返回通用样式对象字典。

    方向一优化（可读性）：
      - 字体 11pt，行高 24px，表头 30px
      - 数字列右对齐，日期列居中，文本左对齐
      - 表头：浅灰底 #EDEEF1，深色加粗，中粗下边框
      - 数据行：纯白底，仅底部细线分隔
      - 合计行：稍深灰底 #DDE0E6，加粗
    """
    import re as _re
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    thin   = Side(style="thin",   color="D5D8DC")
    medium = Side(style="medium", color="A0A7B4")
    none   = Side(style=None)

    return {
        # 表头
        "header_fill":    PatternFill("solid", fgColor="EDEEF1"),
        "header_font":    Font(bold=True, color="1A1A2E", size=11),
        "header_align":   Alignment(horizontal="center", vertical="center", wrap_text=False),
        "header_border":  Border(bottom=medium, left=none, right=none, top=none),
        # 数据行
        "row_fill":       PatternFill("solid", fgColor="FFFFFF"),
        "row_border":     Border(bottom=thin, left=none, right=none, top=none),
        "cell_font":      Font(size=11, color="2C2C3E"),
        "text_align":     Alignment(horizontal="left",   vertical="center"),
        "num_align":      Alignment(horizontal="right",  vertical="center"),
        "date_align":     Alignment(horizontal="center", vertical="center"),
        # 合计行
        "total_fill":     PatternFill("solid", fgColor="DDE0E6"),
        "total_font":     Font(bold=True, size=11, color="1A1A2E"),
        "total_border":   Border(top=thin, bottom=thin, left=none, right=none),
        # 辅助：日期模式（用于对齐判断）
        "_date_re":       _re.compile(r"^\d{4}-\d{2}"),
    }


def _cell_align(value: Any, styles: dict):
    """根据单元格值类型返回对齐方式。"""
    if isinstance(value, (int, float)):
        return styles["num_align"]
    if isinstance(value, str) and styles["_date_re"].match(value):
        return styles["date_align"]
    return styles["text_align"]


def _style_sheet(ws, n_cols: int, n_rows: int, styles: dict) -> None:
    """对 ws 应用简约商务样式（方向一）。"""
    # 表头行
    ws.row_dimensions[1].height = 30
    for col in range(1, n_cols + 1):
        c = ws.cell(row=1, column=col)
        c.fill      = styles["header_fill"]
        c.font      = styles["header_font"]
        c.alignment = styles["header_align"]
        c.border    = styles["header_border"]
    # 数据行：高度 + 字体 + 边框 + 值类型对齐
    for row in range(2, n_rows + 2):
        ws.row_dimensions[row].height = 24
        for col in range(1, n_cols + 1):
            c = ws.cell(row=row, column=col)
            c.fill      = styles["row_fill"]
            c.font      = styles["cell_font"]
            c.border    = styles["row_border"]
            c.alignment = _cell_align(c.value, styles)


def export_xlsx(
    snapshots: list[AccountSnapshot],
    out_path: Path,
    *,
    pdf_files_by_email: Optional[dict[str, dict[str, str]]] = None,
) -> Path:
    """导出多 Sheet Excel：账号概览 / 使用明细。商务配色版。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment
    from openpyxl.utils import get_column_letter

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    styles = _apply_xlsx_style(wb)

    def _write_sheet(ws, columns: list[tuple[str, str]], rows: list[dict[str, Any]]) -> None:
        keys   = [c[0] for c in columns]
        titles = [c[1] for c in columns]
        ws.append(titles)
        for r in rows:
            ws.append([r.get(k, "") for k in keys])
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(keys))}{len(rows)+1}"
        # 列宽自适应
        for col_idx, key in enumerate(keys, start=1):
            max_len = len(str(titles[col_idx - 1]))
            for r in rows:
                v = r.get(key, "")
                max_len = max(max_len, len(str(v)) if v is not None else 0)
            ws.column_dimensions[get_column_letter(col_idx)].width = max(14, min(max_len + 4, 56))
        _style_sheet(ws, len(keys), len(rows), styles)

    # Sheet 1: 账号概览
    ws1 = wb.active
    ws1.title = "账号概览"
    _write_sheet(ws1, _OVERVIEW_COLUMNS, [_overview_row(s) for s in snapshots])

    # Sheet 2: 使用明细
    ws2 = wb.create_sheet("使用明细")
    _ev_row_count = 0
    csv_texts = [s.usage_csv_text for s in snapshots if s.usage_csv_text]
    if csv_texts:
        all_headers: list[str] = []
        all_data: list[list[str]] = []
        for s in snapshots:
            if not s.usage_csv_text:
                continue
            hdrs, rows = _parse_usage_csv(s.usage_csv_text)
            if not all_headers:
                all_headers = hdrs
            all_data.extend(rows)

        ws2.append(all_headers)
        for row in all_data:
            ws2.append(row)
        ws2.freeze_panes = "A2"
        if all_data:
            ws2.auto_filter.ref = (
                f"A1:{get_column_letter(len(all_headers))}{len(all_data)+1}"
            )
        for col_idx, h in enumerate(all_headers, start=1):
            max_len = len(str(h))
            for row in all_data:
                if col_idx - 1 < len(row):
                    max_len = max(max_len, len(str(row[col_idx - 1])))
            ws2.column_dimensions[get_column_letter(col_idx)].width = max(14, min(max_len + 4, 56))
        _style_sheet(ws2, len(all_headers), len(all_data), styles)
        _ev_row_count = len(all_data)
        log.info(f"使用明细 sheet: CSV 格式，{_ev_row_count} 行，{len(all_headers)} 列")
    else:
        event_rows: list[dict[str, Any]] = []
        for s in snapshots:
            event_rows.extend(_event_rows(s))
        _write_sheet(ws2, _EVENT_COLUMNS, event_rows)
        _ev_row_count = len(event_rows)
        log.info(f"使用明细 sheet: API 格式，{_ev_row_count} 行")

    wb.save(out_path)
    log.info(f"XLSX 已写入 {out_path}（账号 {len(snapshots)} / 使用事件 {_ev_row_count}）")
    return out_path


def export_token_summary_xlsx(
    snapshots: list[AccountSnapshot],
    out_path: Path,
    start_date: str = "",
    end_date: str = "",
) -> Path:
    """汇总.xlsx：每个账号一行，各类 Token 合计。

    自动识别 CSV 中含 input/output/cache/token 的列并求和。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment
    from openpyxl.utils import get_column_letter

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # ── 识别 Token 相关列 ──────────────────────────────────────────
    _TOKEN_KW = ("input", "output", "cache", "token")

    def _is_token_col(name: str) -> bool:
        n = name.lower()
        return any(kw in n for kw in _TOKEN_KW)

    def _safe_int(v: str) -> int:
        try:
            return int(str(v).replace(",", "").strip())
        except Exception:
            return 0

    # ── 收集所有账号的 Token 数据 ──────────────────────────────────
    # 先从第一个有 CSV 的 snapshot 取列头
    token_col_names: list[str] = []
    for s in snapshots:
        if s.usage_csv_text:
            hdrs, _ = _parse_usage_csv(s.usage_csv_text)
            token_col_names = [h for h in hdrs if _is_token_col(h)]
            break

    rows_data: list[dict] = []
    for s in snapshots:
        rec: dict[str, Any] = {"账号": s.email, "统计开始": start_date, "统计截止": end_date}
        col_totals: dict[str, int] = {c: 0 for c in token_col_names}

        if s.usage_csv_text:
            hdrs, rows = _parse_usage_csv(s.usage_csv_text)
            col_idx_map = {h: i for i, h in enumerate(hdrs) if _is_token_col(h)}
            for row in rows:
                for col_name, idx in col_idx_map.items():
                    v = _safe_int(row[idx]) if idx < len(row) else 0
                    col_totals[col_name] = col_totals.get(col_name, 0) + v
            rec["记录条数"] = max(0, len(rows))
        else:
            rec["记录条数"] = len(s.usage_events)

        # 构建列名：总 {原列名} 用量
        for col_name in token_col_names:
            rec[f"总{col_name}用量"] = col_totals.get(col_name, 0)
        rows_data.append(rec)

    # ── 合计行 ──────────────────────────────────────────────────────
    if rows_data:
        total_row: dict[str, Any] = {"账号": "合计", "统计开始": "", "统计截止": ""}
        total_row["记录条数"] = sum(r.get("记录条数", 0) for r in rows_data)
        for col_name in token_col_names:
            k = f"总{col_name}用量"
            total_row[k] = sum(r.get(k, 0) for r in rows_data)
        rows_data.append(total_row)

    # ── 写 Excel ────────────────────────────────────────────────────
    wb = Workbook()
    styles = _apply_xlsx_style(wb)
    ws = wb.active
    ws.title = "Token汇总"

    if not rows_data:
        ws.append(["暂无数据"])
        wb.save(out_path)
        return out_path

    # 列顺序：固定前缀 + 各 token 列（Total Tokens 已含合计，不再追加冗余列）
    fixed_cols = ["账号", "统计开始", "统计截止", "记录条数"]
    token_display_cols = [f"总{c}用量" for c in token_col_names]
    all_cols = fixed_cols + token_display_cols

    ws.append(all_cols)
    n_data = len(rows_data) - 1  # 最后一行是合计，单独处理
    for i, r in enumerate(rows_data[:-1]):
        ws.append([r.get(c, "") for c in all_cols])

    # 合计行
    total = rows_data[-1]
    ws.append([total.get(c, "") for c in all_cols])

    ws.freeze_panes = "A2"
    # 列宽
    for col_idx, col_name in enumerate(all_cols, start=1):
        max_len = len(col_name)
        for r in rows_data:
            max_len = max(max_len, len(str(r.get(col_name, ""))))
        ws.column_dimensions[get_column_letter(col_idx)].width = max(14, min(max_len + 4, 56))

    # 应用样式
    _style_sheet(ws, len(all_cols), n_data, styles)

    # 合计行：浅灰底 + 加粗 + 上下边框 + 数字右对齐
    total_row_idx = n_data + 2
    ws.row_dimensions[total_row_idx].height = 26
    for col_idx, col_name in enumerate(all_cols, start=1):
        c = ws.cell(row=total_row_idx, column=col_idx)
        c.fill      = styles["total_fill"]
        c.font      = styles["total_font"]
        c.border    = styles["total_border"]
        c.alignment = _cell_align(c.value, styles)

    # "总Total Tokens用量" 列加粗，突出关键指标
    total_tokens_col = "总Total Tokens用量"
    if total_tokens_col in all_cols:
        token_col_idx = all_cols.index(total_tokens_col) + 1
        from openpyxl.styles import Font as _Font
        bold_token_font = _Font(bold=True, size=11, color="1A1A2E")
        for row in range(2, n_data + 2):
            c = ws.cell(row=row, column=token_col_idx)
            c.font = bold_token_font

    wb.save(out_path)
    log.info(f"汇总.xlsx 已写入 {out_path}（{len(rows_data)-1} 个账号）")
    return out_path


def _invoice_candidates(invoice: dict) -> tuple[str, str]:
    """从发票 dict 里推断 (pdf_url, invoice_id)。

    pdf_url 优先取 invoice_pdf / invoicePdf（Stripe 直链 PDF，预签名，无需认证）。
    hosted_invoice_url 是 HTML 页面不是 PDF，不用于下载。
    """
    if not isinstance(invoice, dict):
        return "", ""
    pdf_url = ""
    # 只取真正的 PDF 直链字段，跳过 hosted_invoice_url（HTML 页）
    for k in ("invoice_pdf", "invoicePdf", "pdf_url", "pdfUrl", "pdf"):
        v = invoice.get(k)
        if isinstance(v, str) and v.startswith("http"):
            pdf_url = v
            break
    inv_id = ""
    for k in ("number", "id", "invoiceNumber", "invoice_number"):
        v = invoice.get(k)
        if isinstance(v, (str, int)):
            inv_id = str(v)
            break
    return pdf_url, inv_id


def _invoice_month_tag(month_value: str) -> str:
    """前端 month 传值（如 2026-04）规范化为 YYYY.MM。"""
    s = (month_value or "").strip()
    if not s:
        return ""
    m = re.search(r"(\d{4})\D+(\d{1,2})", s)
    if not m:
        return ""
    y = m.group(1)
    mo = int(m.group(2))
    return f"{y}.{mo:02d}"


def _normalize_status_text(s: str) -> str:
    """标准化状态文本，用于命名。"""
    v = (s or "").strip().lower()
    if not v:
        return ""
    # 中文状态映射到统一英文，避免文件名不可读
    if "退款" in v:
        return "refunded"
    if "支付" in v and ("未" in v or "待" in v):
        return "open"
    if "支付" in v:
        return "paid"
    if "草稿" in v:
        return "draft"
    if "作废" in v:
        return "void"
    if "无法收款" in v:
        return "uncollectible"
    # 英文常见状态
    if any(k in v for k in ("unpaid", "not paid", "past due", "payment due")):
        return "open"
    for k in ("open", "refunded", "void", "uncollectible", "draft", "paid"):
        if k in v:
            return k
    return _safe_filename(v)


def _normalize_invoice_url(url: str) -> str:
    """标准化发票 URL，避免同一链接因大小写/尾斜杠差异匹配失败。"""
    s = (url or "").strip()
    if not s:
        return ""
    try:
        p = urlsplit(s)
        return urlunsplit((
            p.scheme.lower(),
            p.netloc.lower(),
            p.path.rstrip("/"),
            p.query,
            "",
        ))
    except Exception:
        return s


def _filter_paid_billing_items(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """只保留已支付账单，避免下载 Stripe 未支付账单/付款页。"""
    paid_items: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for url, status in items:
        normalized_status = _normalize_status_text(status)
        if normalized_status != "paid":
            continue
        normalized_url = _normalize_invoice_url(url)
        if not normalized_url or normalized_url in seen_urls:
            continue
        seen_urls.add(normalized_url)
        paid_items.append((url.strip(), normalized_status))
    return paid_items


def _unique_pdf_path(out_dir: Path, base_name: str) -> Path:
    """在 out_dir 下为 base_name（如 `xx-2026.04-paid.pdf`）生成不冲突的路径：
    首个不变；后续追加 `-2`、`-3`…… 直到不存在为止。
    """
    p = out_dir / base_name
    if not p.exists():
        return p
    stem = p.stem
    suffix = p.suffix
    i = 2
    while True:
        cand = out_dir / f"{stem}-{i}{suffix}"
        if not cand.exists():
            return cand
        i += 1


def _download_pdf_direct(pdf_url: str, save_path: Path) -> bool:
    """直接用 requests 下载 PDF（适用于 Stripe invoice_pdf 预签名链接）。"""
    import requests as _req
    try:
        resp = _req.get(pdf_url, timeout=30, stream=True,
                        headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with save_path.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        # 检查下载的确实是 PDF（文件头 %PDF）
        header = save_path.read_bytes()[:5]
        if not header.startswith(b"%PDF"):
            log.warning(f"下载内容不是 PDF（头部={header!r}），删除: {save_path.name}")
            save_path.unlink(missing_ok=True)
            return False
        return True
    except Exception as e:
        log.warning(f"直接下载失败: {e}")
        return False


def _extract_pdf_url_from_stripe_page(hosted_url: str) -> Optional[str]:
    """从 Stripe 托管发票页面 HTML 提取 PDF 直链（无需浏览器）。"""
    import requests as _req
    try:
        resp = _req.get(
            hosted_url, timeout=20,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                   "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"},
        )
        if not resp.ok:
            return None
        text = resp.text
        patterns = [
            r'"invoice_pdf"\s*:\s*"(https://[^"]+)"',
            r'"invoicePdf"\s*:\s*"(https://[^"]+)"',
            r'"pdf_url"\s*:\s*"(https://[^"]+)"',
            r'href="(https://[^"]+/pdf[^"]*)"',
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                url = m.group(1).replace("\\u0026", "&").replace("\\/", "/")
                log.info(f"从 Stripe 页面 HTML 提取到 PDF URL: {url[:80]}")
                return url
    except Exception as e:
        log.debug(f"HTML 提取 PDF URL 失败: {e}")
    return None


def _download_pdf_via_browser(hosted_url: str, save_path: Path) -> bool:
    """用 patchright 访问 Stripe 发票页，拦截 PDF 请求 URL，再用 requests 下载。
    适用于 invoice_pdf 无效或为空时的兜底方案。
    """
    import asyncio

    async def _run() -> Optional[str]:
        try:
            from patchright.async_api import async_playwright
        except ImportError:
            log.warning("patchright 未安装，无法用浏览器下载 PDF")
            return None

        intercepted: list[str] = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(accept_downloads=True)
            page = await ctx.new_page()

            # 拦截所有请求，记录包含 pdf 的 URL
            async def on_request(req):
                url = req.url.lower()
                if ".pdf" in url or "invoice_pdf" in url or "/pdf" in url:
                    intercepted.append(req.url)

            page.on("request", on_request)

            try:
                await page.goto(hosted_url, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(2000)

                # 若未拦截到，优先找账单/发票按钮，避免点到收据按钮
                if not intercepted:
                    for sel in _stripe_invoice_download_selectors():
                        btn = page.locator(sel).first
                        if await btn.count() == 0:
                            continue
                        text = await btn.inner_text()
                        if _is_receipt_download_text(text):
                            continue
                        href = await btn.get_attribute("href")
                        if href and href.startswith("http"):
                            intercepted.append(href)
                            break

                # 若还没有，点击 PDF 按钮并等待下载
                if not intercepted:
                    for sel in _stripe_invoice_download_selectors():
                        btn = page.locator(sel).first
                        if await btn.count() == 0:
                            continue
                        text = await btn.inner_text()
                        if _is_receipt_download_text(text):
                            continue
                        async with page.expect_download(timeout=15000) as dl:
                            await btn.click()
                        download = await dl.value
                        dl_path = save_path.parent / download.suggested_filename
                        await download.save_as(str(dl_path))
                        if dl_path != save_path:
                            dl_path.rename(save_path)
                        await browser.close()
                        return "DOWNLOADED"  # 已直接下载，无需再用 requests
            except Exception as e:
                log.warning(f"浏览器操作异常: {e}")
            finally:
                await browser.close()

        return intercepted[0] if intercepted else None

    try:
        result = asyncio.run(_run())
        if result is None:
            return False
        if result == "DOWNLOADED":
            # 检验文件
            header = save_path.read_bytes()[:5] if save_path.exists() else b""
            return header.startswith(b"%PDF")
        # 用拦截到的 URL 下载
        return _download_pdf_direct(result, save_path)
    except Exception as e:
        log.warning(f"浏览器下载 PDF 失败: {e}")
        return False


def _is_receipt_download_text(text: str) -> bool:
    """Stripe 已支付页同时有账单和收据按钮，收据不是我们要的文件。"""
    value = (text or "").strip().lower()
    return any(word in value for word in ("收据", "receipt"))


def _stripe_invoice_download_selectors() -> list[str]:
    """Stripe 页面下载选择器：先点账单/发票，最后才用通用 PDF 兜底。"""
    invoice_texts = (
        "下载账单",
        "下载发票",
        "Download invoice",
        "Invoice",
        "Invoice PDF",
    )
    selectors: list[str] = []
    for text in invoice_texts:
        selectors.extend([
            f'button:has-text("{text}")',
            f'a:has-text("{text}")',
            f'[role="button"]:has-text("{text}")',
        ])
    selectors.extend([
        ':text("PDF")',
        'a[href*=".pdf"]',
        'a[href*="/pdf"]',
        '[data-testid*="pdf"]',
        'a[download]',
    ])
    return selectors


_STATUS_JS = """
() => {
  const out = [];
  const STATUS_KEYS = ['paid','open','refunded','void','uncollectible','draft',
    '已支付','待支付','未支付','退款','草稿','作废','无法收款'];
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  for (const tr of document.querySelectorAll('tr')) {
    const a = tr.querySelector('a[href*="invoice.stripe.com"]');
    if (!a) continue;
    const tds = [...tr.querySelectorAll('td')];
    if (!tds.length) continue;
    let status = '';
    for (const td of tds) {
      if (td.classList.contains('capitalize')) {
        const t = norm(td.innerText || td.textContent);
        if (t) { status = t; break; }
      }
    }
    if (!status) {
      for (const td of tds) {
        const t = norm(td.innerText || td.textContent).toLowerCase();
        if (!t || t.length > 40) continue;
        if (STATUS_KEYS.some(k => t.includes(k.toLowerCase()))) { status = t; break; }
      }
    }
    out.push({ url: a.href || '', status });
  }
  return out;
}
"""

_BILLING_URLS = [
    "https://cursor.com/settings/billing",
    "https://cursor.com/dashboard/billing",
]


async def _fetch_billing_items_in_ctx(page) -> list[tuple[str, str]]:
    """在已有 page 对象上抓取账单页状态列表，返回 [(url, status), ...]。"""
    items: list[tuple[str, str]] = []
    for billing_url in _BILLING_URLS:
        for _attempt in (1, 2):
            try:
                await page.goto(billing_url, wait_until="load", timeout=20000)
                try:
                    await page.wait_for_selector("button, a[href]", timeout=8000)
                except Exception:
                    pass
                await page.wait_for_timeout(1500)
            except Exception:
                continue
            found_rows: list[dict] = await page.evaluate(_STATUS_JS)
            items = [
                (r["url"], _normalize_status_text(str(r.get("status", ""))))
                for r in (found_rows or [])
                if isinstance(r, dict)
                and str(r.get("url", "")).startswith("http")
                and _normalize_status_text(str(r.get("status", "")))
            ]
            log.info(f"账单页 {billing_url}: {len(found_rows or [])} 行, 可用 {len(items)}")
            if items:
                for u, s in items:
                    log.info(f"  status={s!r} url={u[:70]}")
                break
        if items:
            break
    return items


async def _download_account_all_pdfs(
    cookie_val: str,
    out_dir: Path,
    email_tag: str,
    month_tag: str,
    email: str,
    sem: Optional[asyncio.Semaphore] = None,
) -> dict[str, str]:
    """单 browser session：抓状态 + 下载所有 PDF。

    sem 仅在 asyncio.gather 并发模式下使用（控制协程级浏览器数量）。
    从 ThreadPoolExecutor 线程调用时传 None，由线程池本身控制并发。
    asyncio.Semaphore 不能跨 event loop 共享，线程模式下必须为 None。
    """
    from patchright.async_api import async_playwright

    async def _body() -> dict[str, str]:
        out_dir.mkdir(parents=True, exist_ok=True)
        pdf_files: dict[str, str] = {}
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    ctx = await browser.new_context(accept_downloads=True)
                    await ctx.add_cookies([{
                        "name": "WorkosCursorSessionToken",
                        "value": cookie_val,
                        "domain": "cursor.com",
                        "path": "/",
                        "httpOnly": True,
                        "secure": True,
                    }])

                    # ── Step 1: 抓账单状态 ──────────────────────────────
                    status_page = await ctx.new_page()
                    try:
                        items = await _fetch_billing_items_in_ctx(status_page)
                    finally:
                        await status_page.close()

                    paid_items = _filter_paid_billing_items(items)
                    if not paid_items:
                        log.warning(f"[{email}] 账单页未找到已支付发票行，跳过")
                        return {}

                    log.info(
                        f"[{email}] 账单页状态抓取成功: 总计 {len(items)} 条，"
                        f"已支付 {len(paid_items)} 条"
                    )

                    # ── Step 2: 下载每张 PDF（复用同一 ctx，无需重新认证）──
                    for idx, (hosted_url, status_raw) in enumerate(paid_items, start=1):
                        status_tag = _safe_filename(status_raw)
                        if not status_tag:
                            log.warning(f"[{email}] 账单 {idx} 无 status，跳过")
                            continue

                        base_name = _safe_filename(f"{email_tag}-{month_tag}-{status_tag}.pdf")
                        save_path = _unique_pdf_path(out_dir, base_name)
                        fname = save_path.name

                        log.info(f"[{email}] 下载已支付账单 {idx}/{len(paid_items)}: {fname}")
                        ok = False
                        for _attempt in range(1, 4):
                            ok = await _stripe_page_click_pdf(hosted_url, save_path, ctx)
                            if ok:
                                break
                            log.warning(f"[{email}] 账单 {idx} 第 {_attempt} 次失败，重试...")
                            await asyncio.sleep(3)

                        if ok:
                            pdf_files[str(idx)] = fname
                            log.info(f"[{email}] 账单 PDF 下载成功: {fname}")
                        else:
                            log.warning(f"[{email}] 账单 {idx} 重试 3 次均失败，跳过")
                finally:
                    await browser.close()
        except Exception as e:
            log.warning(f"[{email}] 浏览器账单下载整体异常: {e}")

        return pdf_files

    # asyncio.Semaphore 只在同一 event loop（asyncio.gather 模式）下使用。
    # 线程模式（ThreadPoolExecutor）调用时 sem=None，由线程池控制并发，
    # 跨 event loop 共享 asyncio.Semaphore 会导致死锁。
    if sem is not None:
        async with sem:
            return await _body()
    return await _body()


async def _billing_page_get_stripe_items(cookie_val: str) -> list[tuple[str, str]]:
    """用 patchright + Cookie 访问 Cursor 账单页，提取发票链接及状态。

    返回: [(hosted_invoice_url, status), ...]
    status 为账单列表状态列文本（会转小写）。
    """
    from patchright.async_api import async_playwright

    items: list[tuple[str, str]] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        await ctx.add_cookies([{
            "name": "WorkosCursorSessionToken",
            "value": cookie_val,
            "domain": "cursor.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
        }])
        page = await ctx.new_page()
        try:
            # 尝试几个可能的账单页 URL
            for billing_url in [
                "https://cursor.com/settings/billing",
                "https://cursor.com/dashboard/billing",
                "https://cursor.com/cn/dashboard/billing",
            ]:
                # 每个页面两次渲染尝试，降低偶发漏抓
                for _attempt in (1, 2):
                    await page.goto(billing_url, wait_until="load", timeout=20000)
                    await page.wait_for_timeout(2500)

                    # 同一行内逐 td 文本匹配状态关键字 + 类名 `capitalize` 兜底定位
                    found_rows: list[dict[str, str]] = await page.evaluate("""
                        () => {
                          const out = [];
                          const STATUS_KEYS = [
                            'paid', 'open', 'refunded', 'void', 'uncollectible', 'draft',
                            '已支付', '待支付', '未支付', '退款', '草稿', '作废', '无法收款'
                          ];
                          const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                          const rows = [...document.querySelectorAll('tr')];
                          for (const tr of rows) {
                            const a = tr.querySelector('a[href*="invoice.stripe.com"]');
                            if (!a) continue;
                            const tds = [...tr.querySelectorAll('td')];
                            if (!tds.length) continue;

                            let status = '';
                            // 1) Cursor 的状态列 td 一定带 capitalize 类
                            for (const td of tds) {
                              if (td.classList.contains('capitalize')) {
                                const t = norm(td.innerText || td.textContent);
                                if (t) { status = t; break; }
                              }
                            }
                            // 2) 兜底：逐 td 文本匹配已知状态关键字
                            if (!status) {
                              for (const td of tds) {
                                const t = norm(td.innerText || td.textContent).toLowerCase();
                                if (!t || t.length > 40) continue;
                                if (STATUS_KEYS.some(k => t.includes(k.toLowerCase()))) {
                                  status = t;
                                  break;
                                }
                              }
                            }
                            out.push({ url: a.href || '', status: status });
                          }
                          return out;
                        }
                    """)
                    items = [
                        (
                            row.get("url", ""),
                            _normalize_status_text(str(row.get("status", ""))),
                        )
                        for row in (found_rows or [])
                        if isinstance(row, dict)
                        and str(row.get("url", "")).startswith("http")
                        and _normalize_status_text(str(row.get("status", "")))
                    ]
                    log.info(
                        f"账单页 {billing_url}: 行数={len(found_rows or [])}, "
                        f"可用={len(items)}"
                    )
                    if items:
                        for u, s in items:
                            log.info(f"  行 status={s!r} url={u[:70]}")
                        break
                if items:
                    break
        except Exception as e:
            log.warning(f"账单页提取链接失败: {e}")
        finally:
            await browser.close()

    return items


async def _stripe_page_click_pdf(
    hosted_url: str,
    save_path: Path,
    ctx,  # playwright BrowserContext
) -> bool:
    """在 Stripe 发票页面点击 PDF 按钮下载。

    Stripe 是 React SPA，需要等 networkidle 后再等额外 4s。
    PDF 按钮可能是 <a>/<button>/<div> 任意元素，用宽泛 :text 匹配。
    """
    page = await ctx.new_page()
    try:
        # Stripe 是 React SPA，用 "load" 而非 "networkidle"
        # （networkidle 会因后台持续请求而超时）
        await page.goto(hosted_url, wait_until="load", timeout=30000)
        # 等 React 渲染完：先等 button 出现（最多 8s），再额外等 1s
        try:
            await page.wait_for_selector("button, a[href]", timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)

        import requests as _req

        # ── 诊断1：页面标题和当前 URL ──
        title = await page.title()
        log.info(f"Stripe 页面加载完成: title={title!r}, url={page.url[:80]}")

        # ── 诊断2：所有含 PDF 文字的元素（不限 tag）──
        pdf_els = await page.evaluate("""
            () => [...document.querySelectorAll('*')]
                .filter(e => e.children.length === 0 && e.innerText &&
                             e.innerText.toUpperCase().includes('PDF'))
                .slice(0, 10)
                .map(e => ({
                    tag:  e.tagName,
                    text: e.innerText.trim().slice(0, 80),
                    href: e.href  || '',
                    cls:  e.className.slice(0, 60),
                    id:   e.id    || '',
                }))
        """)
        log.info(f"Stripe 含PDF元素({len(pdf_els)}个): {pdf_els}")

        # ── 诊断3：所有可点击元素（a/button）前10个 ──
        clickable = await page.evaluate("""
            () => [...document.querySelectorAll('a, button')]
                .slice(0, 10)
                .map(e => ({
                    tag:  e.tagName,
                    text: e.innerText.trim().slice(0, 50),
                    href: e.href || '',
                }))
        """)
        log.info(f"Stripe 可点击元素(前10): {clickable}")

        # ── 选择器按优先级依次尝试 ──
        selectors = _stripe_invoice_download_selectors()

        for sel in selectors:
            try:
                el = page.locator(sel).first
                count = await el.count()
                log.info(f"  selector {sel!r} → {count} 个")
                if count == 0:
                    continue

                href = await el.get_attribute("href") or ""
                tag  = await el.evaluate("e => e.tagName")
                text = await el.inner_text()
                log.info(f"  命中: tag={tag}, text={text[:50]!r}, href={href[:80]!r}")
                if _is_receipt_download_text(text):
                    log.info(f"  跳过收据下载按钮: text={text[:50]!r}")
                    continue

                if href.startswith("http") and (".pdf" in href.lower() or "/pdf" in href.lower()):
                    resp = _req.get(href, timeout=30, stream=True,
                                    headers={"User-Agent": "Mozilla/5.0"})
                    resp.raise_for_status()
                    save_path.parent.mkdir(parents=True, exist_ok=True)
                    with save_path.open("wb") as f:
                        for chunk in resp.iter_content(8192):
                            if chunk:
                                f.write(chunk)
                    if save_path.exists() and save_path.read_bytes()[:4] == b"%PDF":
                        log.info(f"PDF href 下载成功: {save_path.name}")
                        return True

                log.info(f"  点击元素等待 download 事件…")
                try:
                    async with page.expect_download(timeout=15000) as dl_info:
                        await el.click()
                    download = await dl_info.value
                    save_path.parent.mkdir(parents=True, exist_ok=True)
                    await download.save_as(str(save_path))
                    if save_path.exists() and save_path.read_bytes()[:4] == b"%PDF":
                        log.info(f"PDF 点击下载成功: {save_path.name}")
                        return True
                except Exception as click_err:
                    log.info(f"  download 事件未触发({click_err})，检查新 tab…")
                    await page.wait_for_timeout(2000)
                    new_pages = [p for p in ctx.pages if p != page and ".pdf" in p.url.lower()]
                    if new_pages:
                        pdf_url = new_pages[0].url
                        log.info(f"  新 tab PDF URL: {pdf_url[:80]}")
                        await new_pages[0].close()
                        resp = _req.get(pdf_url, timeout=30, stream=True,
                                        headers={"User-Agent": "Mozilla/5.0"})
                        resp.raise_for_status()
                        save_path.parent.mkdir(parents=True, exist_ok=True)
                        with save_path.open("wb") as f:
                            for chunk in resp.iter_content(8192):
                                if chunk:
                                    f.write(chunk)
                        if save_path.exists() and save_path.read_bytes()[:4] == b"%PDF":
                            return True

            except Exception as e:
                log.info(f"  selector {sel!r} 异常: {e}")
                continue

        log.warning(
            f"Stripe 页面未找到PDF按钮。"
            f"title={title!r}, PDF元素={len(pdf_els)}个, 可点击={len(clickable)}个"
        )
        return False
    except Exception as e:
        log.warning(f"Stripe 页面访问失败: {e}")
        return False
    finally:
        await page.close()


def _download_invoices_all(
    snapshots: list,
    accounts: list,
    out_root: Path,
    *,
    manager,
    invoice_month: str,
    concurrency: int,
    progress_cb=None,
) -> dict:
    """并发下载所有账号账单。

    核心策略：ThreadPoolExecutor + 每线程独立 asyncio.run()。
    - patchright 的 async_playwright() 有全局异步锁，在同一 event loop 内无法
      真正并发启动多浏览器（asyncio.gather 方案仍是串行）
    - 每个线程拥有独立 event loop，patchright 全局锁仅在本线程内生效，
      多线程之间完全隔离，实现真正并发
    - 每账号合并"状态抓取 + PDF下载"为单次 asyncio.run()，浏览器实例减半

    返回 {email: {idx_str: filename}} 映射。
    """
    import concurrent.futures as _cf
    import threading as _threading
    from .api_client import _split_session_token

    month_tag = _invoice_month_tag(invoice_month)
    if not month_tag:
        return {}

    acc_by_email = {a.email: a for a in accounts}
    def _cb(email: str, phase: str, msg: str = "") -> None:
        if progress_cb:
            try:
                progress_cb(email, phase, msg)
            except Exception:
                pass

    log.info(
        f"[并发诊断] _download_invoices_all 启动: "
        f"snapshots={len(snapshots)}, concurrency={concurrency}, "
        f"max_workers={max(1, min(concurrency, len(snapshots)))}, "
        f"SETTINGS.invoice_download_concurrency={__import__('cam.config', fromlist=['SETTINGS']).SETTINGS.invoice_download_concurrency}"
    )

    def _one_thread(snap) -> tuple[str, dict]:
        """在独立线程中为单个账号完成状态抓取 + 下载，每线程有独立 event loop。"""
        import time as _time
        import threading as _threading
        _t0 = _time.time()
        log.info(
            f"[{snap.email}] ★ THREAD_ENTER tid={_threading.get_ident()} t={_t0:.3f}"
        )
        acc = acc_by_email.get(snap.email)
        if acc is None:
            return snap.email, {}
        try:
            token = manager.get_valid_token(acc)
        except Exception as e:
            log.warning(f"[{snap.email}] 获取 token 失败，跳过账单: {e}")
            return snap.email, {}

        log.info(
            f"[{snap.email}] ★ GOT_TOKEN dt={_time.time()-_t0:.3f}s"
        )

        cookie_val, _ = _split_session_token(token)
        if not cookie_val:
            log.warning(f"[{snap.email}] cookie 为空，跳过账单")
            return snap.email, {}

        email_tag = _safe_filename(snap.email)
        out_dir = out_root / email_tag / "invoices"

        _cb(snap.email, "invoice", "下载账单中...")
        log.info(
            f"[{snap.email}] ★ BEFORE_ASYNCIO_RUN dt={_time.time()-_t0:.3f}s"
        )
        try:
            pdf_files = asyncio.run(
                _download_account_all_pdfs(
                    cookie_val, out_dir, email_tag, month_tag, snap.email,
                    # sem=None：线程模式，ThreadPoolExecutor 本身控制并发
                    # asyncio.Semaphore 不可跨 event loop 共享，此处必须省略
                )
            )
        except Exception as e:
            log.warning(f"[{snap.email}] 账单下载异常: {e}")
            pdf_files = {}

        inv_count = len(pdf_files)
        _cb(snap.email, "done", f"账单 {inv_count} 份" if inv_count else "")
        return snap.email, pdf_files

    out: dict = {}
    max_workers = max(1, min(concurrency, len(snapshots)))
    try:
        with _cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = {pool.submit(_one_thread, snap): snap for snap in snapshots}
            for fut in _cf.as_completed(futs):
                snap = futs[fut]
                try:
                    email, pdf_files = fut.result()
                    out[email] = pdf_files
                except Exception as e:
                    log.warning(f"[{snap.email}] 账单下载任务异常: {e}")
                    out[snap.email] = {}
    except Exception as e:
        log.warning(f"账单并发下载整体失败: {e}")
        return {}
    return out


def _download_pdfs_via_billing_page(
    access_token: str,
    out_dir: Path,
    *,
    email_tag: str = "account",
    invoice_month: str = "",
    stripe_items: Optional[list[tuple[str, str]]] = None,
) -> dict[str, str]:
    """完整浏览器流程：
      Cursor 账单页 (View 链接) → Stripe 发票页 → 点击 PDF 按钮 → 下载。
    返回 {索引: filename} 映射（filename 包含状态名）。
    """
    import asyncio
    from .api_client import _split_session_token

    cookie_val, _ = _split_session_token(access_token)
    if not cookie_val:
        return {}

    async def _run() -> dict[str, str]:
        from patchright.async_api import async_playwright

        pdf_files: dict[str, str] = {}
        month_tag = _invoice_month_tag(invoice_month)
        if not month_tag:
            log.warning("账单月份为空或格式非法，跳过浏览器账单下载")
            return {}

        # Step1: 拿到所有 Stripe 发票 URL + 状态
        items = stripe_items if stripe_items is not None else await _billing_page_get_stripe_items(cookie_val)
        paid_items = _filter_paid_billing_items(items)
        if not paid_items:
            log.warning("账单页未找到已支付发票链接")
            return {}

        out_dir.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(accept_downloads=True)

            for idx, (hosted_url, status_raw) in enumerate(paid_items, start=1):
                status_tag = _safe_filename(_normalize_status_text(status_raw))
                if not status_tag:
                    log.warning(f"账单 {idx} 缺少 status，跳过")
                    continue
                base_name = _safe_filename(f"{email_tag}-{month_tag}-{status_tag}.pdf")
                save_path = _unique_pdf_path(out_dir, base_name)
                fname = save_path.name

                log.info(
                    f"浏览器下载已支付账单 {idx}/{len(paid_items)}: "
                    f"month={month_tag}, status={status_tag}, url={hosted_url[:80]}"
                )
                ok = False
                for _attempt in range(1, 4):  # 最多重试 3 次
                    ok = await _stripe_page_click_pdf(hosted_url, save_path, ctx)
                    if ok:
                        break
                    log.warning(f"账单 {idx} 第 {_attempt} 次下载失败，等待后重试...")
                    await asyncio.sleep(3)
                if ok:
                    pdf_files[str(idx)] = fname
                    log.info(f"账单 PDF 下载成功: {fname}")
                else:
                    log.warning(f"账单 {idx} 重试 3 次均失败，跳过")

            await browser.close()

        return pdf_files

    try:
        return asyncio.run(_run())
    except Exception as e:
        log.warning(f"浏览器账单下载整体失败: {e}")
        return {}


def _render_pdf_images_to_sheet(ws, pdf_paths: list[Path], email: str) -> None:
    """把 PDF 发票第一页渲染成图片，嵌入 Excel sheet（无表头）。
    每个账号前插一行 email 标签，后接发票图片。
    需要 pymupdf（pip install pymupdf）。
    """
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Font, PatternFill, Alignment

    try:
        import pymupdf as fitz   # 新版包名（pymupdf >= 1.24）
        has_fitz = True
    except ImportError:
        try:
            import fitz            # 旧版兼容（pip install pymupdf 仍可用 fitz）
            has_fitz = True
        except ImportError:
            has_fitz = False
            log.warning("pymupdf 未安装，账单 sheet 将仅显示文件名（pip install pymupdf）")

    valid_pdfs = [p for p in pdf_paths if p.exists() and p.stat().st_size > 512]
    if not valid_pdfs:
        return

    # 用当前 sheet 最后有内容的行之后插入
    cur_row = ws.max_row + (2 if ws.max_row > 1 else 1)

    # 账号标签行
    cell = ws.cell(row=cur_row, column=1, value=f"📧 {email}")
    cell.font = Font(bold=True, size=11, color="1155CC")
    cell.fill = PatternFill("solid", fgColor="EEF4FF")
    cell.alignment = Alignment(vertical="center")
    ws.row_dimensions[cur_row].height = 20
    ws.column_dimensions["A"].width = max(ws.column_dimensions["A"].width or 0, 100)
    cur_row += 1

    for pdf_path in valid_pdfs:
        if not has_fitz:
            # 无 pymupdf → 只写文件名
            ws.cell(row=cur_row, column=1, value=f"[PDF] {pdf_path.name}")
            cur_row += 1
            continue

        try:
            doc = fitz.open(str(pdf_path))
            page = doc[0]
            # 150 DPI：清晰度与文件大小的最佳平衡（官方推荐用 dpi= 参数）
            pix = page.get_pixmap(dpi=150, alpha=False)
            img_bytes = pix.tobytes("png")
            doc.close()

            from io import BytesIO
            img = XLImage(BytesIO(img_bytes))
            # 目标宽度 680px（Excel 列宽约 97 字符），等比缩放
            target_w = 680
            scale = target_w / pix.width
            img.width  = target_w
            img.height = int(pix.height * scale)
            img.anchor = f"A{cur_row}"
            ws.add_image(img)

            # 预留足够行高（Excel 行高单位是 pt，1px ≈ 0.75pt）
            rows_needed = max(int(img.height * 0.75 / 15) + 1, 2)
            for r in range(cur_row, cur_row + rows_needed):
                ws.row_dimensions[r].height = 15
            cur_row += rows_needed + 1  # 图片后空一行

        except Exception as e:
            log.warning(f"PDF 渲染失败 {pdf_path.name}: {e}")
            ws.cell(row=cur_row, column=1, value=f"[PDF 渲染失败] {pdf_path.name}: {e}")
            cur_row += 1


def _download_one_account_invoices(
    snap: AccountSnapshot,
    acc: Account,
    out_dir: Path,
    manager: TokenManager,
    invoice_month: str = "",
) -> dict[str, str]:
    """下载单账号所有账单 PDF，返回 {inv_key: filename} 映射。

    下载优先级：
      1. invoice_pdf 直链（Stripe 预签名，最快）
      2. 从 hosted_invoice_url 页面 HTML 提取 PDF URL
      3. patchright 浏览器：Stripe 发票页点击下载（有 hosted_invoice_url 时）
      4. patchright 浏览器：Cursor 账单页 View 按钮 → Stripe 页下载（API 无数据时兜底）
    """
    import asyncio
    from .api_client import _split_session_token

    def _get_billing_statuses(access_token: str) -> list[tuple[str, str]]:
        cookie_val, _ = _split_session_token(access_token)
        if not cookie_val:
            return []
        try:
            return asyncio.run(_billing_page_get_stripe_items(cookie_val))
        except Exception as e:
            log.warning(f"[{snap.email}] 账单页状态抓取失败: {e}")
            return []

    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_files: dict[str, str] = {}

    email_tag = _safe_filename(snap.email)
    month_tag = _invoice_month_tag(invoice_month)
    if not month_tag:
        log.warning(f"[{snap.email}] 账单月份为空或格式非法，跳过账单下载")
        return {}
    token: Optional[str] = None
    billing_items: list[tuple[str, str]] = []
    billing_status_by_url: dict[str, str] = {}
    try:
        token = manager.get_valid_token(acc)
        billing_items = _get_billing_statuses(token)
        billing_status_by_url = {
            _normalize_invoice_url(u): s
            for u, s in billing_items
            if isinstance(u, str) and u
        }
        if billing_items:
            log.info(f"[{snap.email}] 账单页状态抓取成功: {len(billing_items)} 条")
        else:
            log.warning(f"[{snap.email}] 账单页未抓到行级状态，跳过账单下载")
            return {}
    except Exception as e:
        log.warning(f"[{snap.email}] 获取 token/账单状态失败，跳过账单下载: {e}")
        return {}

    for idx, inv in enumerate(snap.invoices):
        number     = inv.get("number") or inv.get("invoiceNumber") or ""
        inv_id_raw = inv.get("id")     or inv.get("invoiceId")     or idx
        inv_key    = number or str(inv_id_raw)
        hosted = inv.get("hosted_invoice_url") or inv.get("hostedInvoiceUrl") or ""
        status_from_page = billing_status_by_url.get(
            _normalize_invoice_url(hosted), ""
        ) if hosted else ""
        status_tag = _safe_filename(_normalize_status_text(status_from_page))
        if not status_tag:
            log.warning(f"[{snap.email}] 账单 {inv_key} 未匹配到同一行 status，跳过")
            continue
        base_name  = _safe_filename(f"{email_tag}-{month_tag}-{status_tag}.pdf")
        save_path  = _unique_pdf_path(out_dir, base_name)
        fname      = save_path.name

        # ── 方式 1：invoice_pdf 直链 ──
        pdf_url, _ = _invoice_candidates(inv)
        if pdf_url and _download_pdf_direct(pdf_url, save_path):
            pdf_files[inv_key] = fname
            log.info(f"[{snap.email}] 账单 PDF 直链下载成功: {fname}")
            continue

        # ── 方式 2：从 hosted_invoice_url 提取 ──
        if hosted:
            extracted = _extract_pdf_url_from_stripe_page(hosted)
            if extracted and _download_pdf_direct(extracted, save_path):
                pdf_files[inv_key] = fname
                log.info(f"[{snap.email}] 账单 PDF HTML 提取下载成功: {fname}")
                continue

        # ── 方式 3：patchright 浏览器点击下载 ──
        if hosted:
            log.info(f"[{snap.email}] 尝试浏览器下载 PDF: {hosted[:80]}")
            if _download_pdf_via_browser(hosted, save_path):
                pdf_files[inv_key] = fname
                log.info(f"[{snap.email}] 账单 PDF 浏览器下载成功: {fname}")
                continue

        log.warning(
            f"[{snap.email}] 账单 {inv_key}: 前三种方式均失败，"
            f"invoice_pdf={'有' if pdf_url else '无'}, hosted={'有' if hosted else '无'}"
        )

    # ── 方式 4：API 无发票数据时，整体走浏览器账单页流程 ──
    if not snap.invoices and not pdf_files:
        log.info(f"[{snap.email}] API 账单为空，尝试浏览器账单页全流程下载")
        try:
            if token is None:
                token = manager.get_valid_token(acc)
            billing_pdfs = _download_pdfs_via_billing_page(
                token, out_dir,
                email_tag=email_tag,
                invoice_month=invoice_month,
                stripe_items=billing_items or None,
            )
            if billing_pdfs:
                pdf_files.update(billing_pdfs)
                log.info(f"[{snap.email}] 浏览器账单页下载: {len(billing_pdfs)} 个 PDF")
            else:
                log.warning(f"[{snap.email}] 浏览器账单页也未找到发票")
        except Exception as e:
            log.warning(f"[{snap.email}] 浏览器账单页流程失败: {e}")

    return pdf_files


def export_per_account(
    accounts: list[Account],
    snapshots: list[AccountSnapshot],
    out_root: Path,
    *,
    manager: Optional[TokenManager] = None,
    with_raw: bool = True,
    with_invoices: bool = True,
    with_detail_xlsx: bool = True,
    with_full_summary_xlsx: bool = True,
    with_summary: bool = True,
    invoice_month: str = "",
    start_date: str = "",
    end_date: str = "",
    progress_cb=None,          # callable(email: str, phase: str, msg: str = "")
    browser_semaphore=None,    # threading.Semaphore，限制并发浏览器实例数
) -> dict[str, dict[str, Any]]:
    """每个账号一个子目录：

        {out_root}/
          {email}/
            {email}.xlsx       ← Excel（账号概览 / 使用明细，可选）
            raw.json           ← 原始 JSON（可选）
            invoices/
              {id}.pdf ...     ← 发票 PDF（可选）
          汇总.xlsx             ← Token 汇总（可选）
          _summary.xlsx        ← 全账号明细汇总（可选，默认开启）

    progress_cb(email, phase, msg) 在关键节点回调：
      invoice  → 开始下载账单
      excel    → 开始生成报表（with_detail_xlsx=True 时）
      done     → 该账号全部完成
      summary  → 开始生成汇总（email 为空字符串）
    """
    def _cb(email: str, phase: str, msg: str = "") -> None:
        if progress_cb:
            try:
                progress_cb(email, phase, msg)
            except Exception:
                pass

    import concurrent.futures as _cf
    import threading as _threading

    mgr = manager or get_default_manager()
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    # ── Phase 1：并发下载账单 PDF ──────────────────────────────────
    # ThreadPoolExecutor 每线程独立运行一个 asyncio event loop + patchright 实例，
    # 并发数直接读配置，不依赖 threading.Semaphore._value（该属性反映的是当前
    # 剩余计数，若 semaphore 已被 acquire 过则会得到错误的值）。
    from .config import SETTINGS as _SETTINGS
    concurrency = max(1, _SETTINGS.invoice_download_concurrency)

    all_pdf_files: dict[str, dict[str, str]] = {}
    if with_invoices and snapshots:
        all_pdf_files = _download_invoices_all(
            snapshots, accounts, out_root,
            manager=mgr,
            invoice_month=invoice_month,
            concurrency=concurrency,
            progress_cb=progress_cb,
        )

    # ── Phase 2：Excel / raw JSON（不涉及浏览器，可安全并发）────────
    summary: dict[str, dict[str, Any]] = {}
    _lock = _threading.Lock()

    def _process_one(snap: AccountSnapshot) -> None:
        acc_dir = out_root / _safe_filename(snap.email)
        acc_dir.mkdir(parents=True, exist_ok=True)

        pdf_files = all_pdf_files.get(snap.email, {})

        xlsx_path = None
        if with_detail_xlsx:
            _cb(snap.email, "excel", "生成报表...")
            xlsx_path = acc_dir / f"{_safe_filename(snap.email)}.xlsx"
            export_xlsx([snap], xlsx_path, pdf_files_by_email={snap.email: pdf_files})

        entry: dict[str, Any] = {
            "dir": acc_dir, "xlsx": xlsx_path, "json": None,
            "invoices": len(pdf_files),
        }

        if with_raw:
            json_path = acc_dir / "raw.json"
            json_path.write_text(
                json.dumps(asdict(snap), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            entry["json"] = json_path

        with _lock:
            summary[snap.email] = entry

        # 只有 with_detail_xlsx 时才需要再发 done（invoice 下载阶段已发过）
        if with_detail_xlsx:
            inv_count = len(pdf_files)
            _cb(snap.email, "done",
                f"账单 {inv_count} 份" if inv_count else "")

    if with_detail_xlsx or with_raw:
        max_workers = min(len(snapshots), 8)
        with _cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = {pool.submit(_process_one, snap): snap for snap in snapshots}
            for fut in _cf.as_completed(futs):
                try:
                    fut.result()
                except Exception as e:
                    snap = futs[fut]
                    log.warning(f"[{snap.email}] export 阶段异常: {e}")
    else:
        # 仅补全 summary 字典（无需线程池）
        for snap in snapshots:
            acc_dir = out_root / _safe_filename(snap.email)
            acc_dir.mkdir(parents=True, exist_ok=True)
            pdf_files = all_pdf_files.get(snap.email, {})
            summary[snap.email] = {
                "dir": acc_dir, "xlsx": None, "json": None,
                "invoices": len(pdf_files),
            }

    # ── Phase 3：汇总 ──────────────────────────────────────────────
    if with_summary and snapshots:
        _cb("", "summary", "生成汇总报表...")
        if with_full_summary_xlsx:
            export_xlsx(snapshots, out_root / "_summary.xlsx",
                        pdf_files_by_email=all_pdf_files)
        export_token_summary_xlsx(
            snapshots,
            out_root / "汇总.xlsx",
            start_date=start_date,
            end_date=end_date,
        )
        _cb("", "zip_ready", "打包 ZIP 就绪")

    return summary


def download_invoices(
    accounts: list[Account],
    snapshots: list[AccountSnapshot],
    out_dir: Path,
    *,
    manager: Optional[TokenManager] = None,
) -> dict[str, int]:
    """为每个账号下载其 invoices 里的 PDF。返回 {email: 下载数量}。"""
    mgr = manager or get_default_manager()
    acc_by_email = {a.email: a for a in accounts}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    counts: dict[str, int] = {}
    for snap in snapshots:
        acc = acc_by_email.get(snap.email)
        if acc is None:
            continue
        if not snap.invoices:
            counts[snap.email] = 0
            continue

        try:
            token = mgr.get_valid_token(acc)
        except Exception as e:
            log.warning(f"[{snap.email}] 下载发票跳过: 无有效 token ({e})")
            counts[snap.email] = 0
            continue

        target_dir = out_dir / _safe_filename(snap.email)
        target_dir.mkdir(parents=True, exist_ok=True)

        n = 0
        with CursorClient(token) as client:
            for idx, inv in enumerate(snap.invoices):
                pdf_url, inv_id = _invoice_candidates(inv)
                if not pdf_url:
                    continue
                fname = f"{inv_id or idx}.pdf"
                save_path = target_dir / _safe_filename(fname)
                try:
                    client.download_invoice_pdf(pdf_url, save_path)
                    n += 1
                    log.info(f"[{snap.email}] 下载发票 {fname}")
                except Exception as e:
                    log.warning(f"[{snap.email}] 下载 {fname} 失败: {e}")
        counts[snap.email] = n
    return counts
