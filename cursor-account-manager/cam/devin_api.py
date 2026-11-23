"""Devin 平台 HTTP 接口（挂载到 web_server 的 FastAPI app）。

所有路由前缀 ``/api/devin``；账号/快照/批次都存独立的 devin_* 表，不影响 Cursor 数据。
"""

from __future__ import annotations

import asyncio
import io
import json
import threading
import time
from datetime import date, datetime, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .devin_config import devin_settings
from .devin_errors import ERROR_LABELS, error_label
from .devin_import import (
    DevinImportError,
    build_template_xlsx,
    normalize_email,
    parse_accounts_file,
)
from .devin_invoices import (
    export_path,
    get_invoice_progress,
    list_exports,
    run_invoice_export,
)
from .devin_runner import build_alert, get_progress, is_running, run_devin_collection, RunSummary
from .devin_scheduler import get_devin_scheduler
from .devin_store import DevinStore, get_default_devin_store
from .alerting import send_alert
from .config import SETTINGS
from .logger import get

log = get("devin_api")
router = APIRouter(prefix="/api/devin", tags=["devin"])

HIGH_USAGE_PCT = 80.0


def _store() -> DevinStore:
    return get_default_devin_store()


# ─── 数据模型 ─────────────────────────────────────────────────────

class DevinAccountRow(BaseModel):
    email: str
    imap_password: str
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None
    owner_email: str = ""
    note: str = ""


class SaveDevinAccountsRequest(BaseModel):
    accounts: List[DevinAccountRow]
    overwrite: bool = False
    source: str = "upload"


class CollectRequest(BaseModel):
    emails: List[str] = []
    notify: bool = False


class StatusRequest(BaseModel):
    status: str


class NoteRequest(BaseModel):
    note: str = ""


# ─── 看板 ─────────────────────────────────────────────────────────

def _days_left(cycle_end: str | None, today: Optional[date] = None) -> Optional[int]:
    """距离账期终点（续费日）还有几天；已过期为负数，无法解析返回 None。"""
    if not cycle_end:
        return None
    try:
        end = datetime.strptime(cycle_end, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (end - (today or date.today())).days


def _kpis(rows: list[dict]) -> dict[str, Any]:
    active = [r for r in rows if r["status"] == "active"]
    with_data = [r for r in rows if r.get("snapshot_id")]
    pcts = [r["weekly_used_pct"] for r in with_data if r.get("weekly_used_pct") is not None]
    plans: dict[str, int] = {}
    for r in with_data:
        name = r.get("plan_name") or "未识别"
        plans[name] = plans.get(name, 0) + 1
    failed_latest = [r for r in active if r.get("last_attempt_ok") is False]
    return {
        "total": len(rows),
        "active": len(active),
        "disabled": len(rows) - len(active),
        "with_data": len(with_data),
        "never_collected": len([r for r in active if r.get("last_attempt_at") is None]),
        "failed_latest": len(failed_latest),
        "avg_weekly_used_pct": round(sum(pcts) / len(pcts), 2) if pcts else None,
        "high_usage": len([p for p in pcts if p >= HIGH_USAGE_PCT]),
        "idle": len([p for p in pcts if p < 5]),
        "renewing_soon": len([r for r in with_data if 0 <= (_days_left(r.get("cycle_end")) if r.get("cycle_end") else -1) <= 7]),
        "plan_distribution": plans,
        "on_demand_used_total": round(sum((r.get("on_demand_used_usd") or 0) for r in with_data), 2),
        "plan_price_total": round(sum((r.get("plan_price_usd") or 0) for r in with_data), 2),
    }


def _schedule_info() -> dict[str, Any]:
    cfg = devin_settings()
    sched = get_devin_scheduler()
    next_at = sched.next_at if sched else None
    return {
        "enabled": cfg.schedule_enable,
        "daily_at": cfg.daily_at,
        "timezone": getattr(SETTINGS, "bi_sync_biz_tz", "Asia/Shanghai"),
        "next_run_at": int(next_at.timestamp()) if next_at else None,
        "alert_enabled": cfg.alert_enable and SETTINGS.alert_bot_enable,
        "concurrency": cfg.concurrency,
    }


@router.get("/dashboard")
async def dashboard(q: str = ""):
    def build() -> dict[str, Any]:
        store = _store()
        rows = store.dashboard_rows()
        kw = (q or "").strip().lower()
        if kw:
            rows = [
                r for r in rows
                if kw in r["email"] or kw in (r["owner_email"] or "").lower() or kw in (r["note"] or "").lower()
            ]
        runs = store.list_runs(1)
        return {
            "rows": rows,
            "kpis": _kpis(rows),
            "last_run": runs[0] if runs else None,
            "progress": get_progress(),
            "schedule": _schedule_info(),
            "error_labels": ERROR_LABELS,
            "server_time": int(time.time()),
        }

    return await asyncio.to_thread(build)


@router.get("/progress")
async def progress():
    return get_progress()


@router.get("/runs")
async def list_runs(limit: int = 20):
    return {"runs": await asyncio.to_thread(_store().list_runs, limit)}


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    run = await asyncio.to_thread(_store().get_run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="批次不存在")
    return run


# ─── 采集 ─────────────────────────────────────────────────────────

def _start_background_run(emails: list[str], notify: bool) -> None:
    def target() -> None:
        try:
            run_devin_collection(trigger="manual", emails=emails or None, notify=notify)
        except Exception:
            log.exception("Devin 手动采集异常")

    threading.Thread(target=target, name="cam-devin-manual", daemon=True).start()


@router.post("/collect")
async def collect(req: CollectRequest):
    """后台启动一轮采集（emails 为空=全部启用账号）。进度见 /progress。"""
    if is_running() or get_invoice_progress()["running"]:
        raise HTTPException(status_code=409, detail="已有采集/发票导出任务在执行，请稍后")
    emails = [normalize_email(e) for e in req.emails if normalize_email(e)]
    if emails:
        known = {a["email"] for a in _store().list_accounts()}
        missing = [e for e in emails if e not in known]
        if missing:
            raise HTTPException(status_code=404, detail=f"账号不在 Devin 账号库: {', '.join(missing[:5])}")
    _start_background_run(emails, req.notify)
    return {"started": True, "emails": len(emails) or "all"}


@router.post("/collect-failed")
async def collect_failed():
    """只重采最近一次尝试失败的启用账号。"""
    if is_running():
        raise HTTPException(status_code=409, detail="已有采集任务在执行，请稍后")
    rows = await asyncio.to_thread(_store().dashboard_rows)
    emails = [r["email"] for r in rows if r["status"] == "active" and r.get("last_attempt_ok") is False]
    if not emails:
        raise HTTPException(status_code=422, detail="没有需要重试的失败账号")
    _start_background_run(emails, False)
    return {"started": True, "emails": len(emails)}


@router.post("/notify-last-run")
async def notify_last_run():
    """把最近一次批次的结果重新发一遍飞书卡片（验证通知通道/格式）。"""
    runs = await asyncio.to_thread(_store().list_runs, 1)
    if not runs:
        raise HTTPException(status_code=404, detail="还没有采集批次")
    run = runs[0]
    summary = RunSummary(
        run_id=run["run_id"], trigger="scheduler" if run["trigger"] == "scheduler" else "manual",
        total=run["total"], success=run["success"], failed=run["failed"], skipped=run["skipped"],
        duration_sec=max(0, (run["finished_at"] or int(time.time())) - run["started_at"]),
        failure_breakdown=run["failure_breakdown"], failures=run["failures_json"],
    )
    title, content, level = build_alert(summary)
    await asyncio.to_thread(send_alert, title, content, level=level)
    return {"ok": True, "alert_enabled": SETTINGS.alert_bot_enable, "title": title}


# ─── 账号管理 ─────────────────────────────────────────────────────

@router.get("/accounts")
async def list_accounts(q: str = "", limit: int = 0):
    rows = await asyncio.to_thread(_store().list_accounts, q, limit)
    for r in rows:
        r.pop("imap_password", None)  # 列表不回显授权码
    return {"count": len(rows), "accounts": rows}


@router.post("/upload")
async def upload_accounts(file: UploadFile = File(...)):
    """解析 CSV/Excel，返回预览（不落库）。"""
    data = await file.read()
    try:
        rows, warnings = parse_accounts_file(file.filename or "", data)
    except DevinImportError as exc:
        raise HTTPException(status_code=400, detail=f"文件解析失败: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"文件解析失败: {exc}") from exc
    if not rows:
        raise HTTPException(status_code=422, detail="未解析到有效账号，请检查文件格式")
    known = {a["email"] for a in _store().list_accounts()}
    dedup: dict[str, dict] = {}
    for r in rows:
        dedup[r.email] = r.to_dict()
    out = []
    for email, d in dedup.items():
        d["exists"] = email in known
        out.append(d)
    return {"count": len(out), "duplicates_in_file": len(rows) - len(out), "warnings": warnings[:50], "accounts": out}


@router.get("/accounts/template.xlsx")
async def download_template():
    return StreamingResponse(
        io.BytesIO(build_template_xlsx()),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="devin_accounts_template.xlsx"'},
    )


@router.post("/accounts/save")
async def save_accounts(req: SaveDevinAccountsRequest):
    store = _store()
    cfg_host, cfg_port = SETTINGS.default_imap_host, SETTINGS.default_imap_port
    existing = {a["email"] for a in store.list_accounts()}
    latest: dict[str, DevinAccountRow] = {}
    for acc in req.accounts:
        norm = normalize_email(acc.email)
        if norm:
            latest[norm] = acc
    saved: list[str] = []
    skipped: list[str] = []
    for email, acc in latest.items():
        if "@" not in email:
            raise HTTPException(status_code=400, detail=f"邮箱格式不正确: {email}")
        if not acc.imap_password.strip():
            raise HTTPException(status_code=400, detail=f"{email} 缺少 imap_password")
        if email in existing and not req.overwrite:
            skipped.append(email)
            continue
        store.upsert_account(
            email=email,
            imap_password=acc.imap_password.strip(),
            imap_host=(acc.imap_host or cfg_host).strip(),
            imap_port=acc.imap_port or cfg_port,
            owner_email=normalize_email(acc.owner_email),
            note=(acc.note or "").strip()[:500],
            source=req.source,
        )
        saved.append(email)
    log.info(f"Devin 账号库更新：保存 {len(saved)}，跳过 {len(skipped)}")
    return {"saved": len(saved), "skipped": len(skipped), "saved_emails": saved, "skipped_emails": skipped}


@router.delete("/accounts/{email:path}")
async def delete_account(email: str, purge: bool = False):
    norm = normalize_email(email)
    store = _store()
    if not store.get_account(norm):
        raise HTTPException(status_code=404, detail="账号不存在")
    store.delete_account(norm, purge_snapshots=purge)
    log.info(f"Devin 账号库删除：{norm} purge={purge}")
    return {"ok": True, "email": norm}


@router.post("/accounts/{email:path}/status")
async def set_status(email: str, req: StatusRequest):
    norm = normalize_email(email)
    if req.status not in ("active", "disabled"):
        raise HTTPException(status_code=400, detail="status 只能是 active / disabled")
    store = _store()
    if not store.get_account(norm):
        raise HTTPException(status_code=404, detail="账号不存在")
    store.set_status(norm, req.status)
    return {"ok": True, "email": norm, "status": req.status}


@router.post("/accounts/{email:path}/note")
async def set_note(email: str, req: NoteRequest):
    norm = normalize_email(email)
    store = _store()
    if not store.get_account(norm):
        raise HTTPException(status_code=404, detail="账号不存在")
    store.update_note(norm, req.note.strip())
    return {"ok": True}


# ─── 快照 ─────────────────────────────────────────────────────────

@router.get("/snapshots/{snapshot_id}")
async def get_snapshot(snapshot_id: int):
    snap = await asyncio.to_thread(_store().get_snapshot, snapshot_id)
    if not snap:
        raise HTTPException(status_code=404, detail="快照不存在")
    try:
        snap["raw"] = json.loads(snap.pop("raw_json") or "{}")
    except ValueError:
        snap["raw"] = {}
    return snap


@router.get("/history/{email:path}")
async def history(email: str, limit: int = 30):
    norm = normalize_email(email)
    rows = await asyncio.to_thread(_store().list_snapshots, norm, limit)
    for r in rows:
        r["error_label"] = error_label(r["error_code"]) if r["error_code"] else ""
    return {"email": norm, "snapshots": rows}


# ─── 账期（入库）─────────────────────────────────────────────────

@router.get("/cycles")
async def list_cycles(email: str = "", limit: int = 500):
    """账期明细：每账号每个计费周期一行，随成功采集自动刷新最新值与峰值。"""
    norm = normalize_email(email) if email else ""
    rows = await asyncio.to_thread(_store().list_cycles, norm, limit)
    today = date.today()
    for r in rows:
        left = _days_left(r["cycle_end"], today)
        r["days_left"] = left
        r["state"] = "current" if (left is not None and left >= 0) else "ended"
    return {"count": len(rows), "cycles": rows}


# ─── 发票（实时读取，直接导出 Excel，不入库）──────────────────────

class InvoiceExportRequest(BaseModel):
    emails: List[str] = []


def _start_invoice_export(emails: list[str]) -> None:
    def target() -> None:
        try:
            summary = run_invoice_export(emails=emails or None)
            if summary.lock_busy:
                log.warning("发票导出被跳过：已有采集任务占用")
        except Exception:
            log.exception("Devin 发票导出异常")

    threading.Thread(target=target, name="cam-devin-invoices", daemon=True).start()


@router.post("/invoices/export")
async def start_invoice_export(req: InvoiceExportRequest):
    """后台实时读取发票并导出 xlsx（emails 为空=全部启用账号）。进度见 /invoices/progress。"""
    if is_running() or get_invoice_progress()["running"]:
        raise HTTPException(status_code=409, detail="已有采集/发票导出任务在执行，请稍后")
    emails = [normalize_email(e) for e in req.emails if normalize_email(e)]
    if emails:
        known = {a["email"] for a in _store().list_accounts()}
        missing = [e for e in emails if e not in known]
        if missing:
            raise HTTPException(status_code=404, detail=f"账号不在 Devin 账号库: {', '.join(missing[:5])}")
    _start_invoice_export(emails)
    return {"started": True, "emails": len(emails) or "all"}


@router.get("/invoices/progress")
async def invoice_progress():
    return {"progress": get_invoice_progress(), "files": await asyncio.to_thread(list_exports, 20)}


@router.get("/invoices/files/{name}")
async def download_invoice_file(name: str):
    path = export_path(name)
    if path is None:
        raise HTTPException(status_code=404, detail="导出文件不存在")
    return FileResponse(
        path, filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ─── 导出 ─────────────────────────────────────────────────────────

def _ts(value: Optional[int]) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")


@router.get("/export.xlsx")
async def export_xlsx():
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    rows = await asyncio.to_thread(_store().dashboard_rows)
    wb = Workbook()
    ws = wb.active
    ws.title = "Devin 用量与套餐"
    headers = [
        "邮箱", "负责人", "状态", "套餐", "套餐价格(USD)", "计费周期", "账期起", "账期止(续费日)",
        "周额度已用(%)", "周额度重置时间", "日额度已用(%)", "按需已用(USD)", "按需余额(USD)",
        "消息用量上限(USD)", "数据时间", "最近一次采集", "最近失败原因", "备注",
    ]
    ws.append(headers)
    for r in rows:
        ws.append([
            r["email"], r["owner_email"], "启用" if r["status"] == "active" else "停用",
            r.get("plan_name") or "", r.get("plan_price_usd"), r.get("billing_period") or "", r.get("cycle_start") or "", r.get("cycle_end") or "",
            r.get("weekly_used_pct"), _ts(r.get("weekly_reset_at")), r.get("daily_used_pct"),
            r.get("on_demand_used_usd"), r.get("on_demand_balance_usd"), r.get("message_limit_usd"),
            _ts(r.get("data_collected_at")),
            ("成功" if r.get("last_attempt_ok") else "失败") if r.get("last_attempt_at") else "未采集",
            error_label(r["last_error_code"]) if r.get("last_error_code") else "", r.get("note") or "",
        ])
    fill = PatternFill(fill_type="solid", fgColor="EAF2FF")
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    widths = (30, 26, 8, 10, 14, 10, 12, 14, 14, 20, 14, 14, 14, 16, 20, 14, 26, 24)
    for idx, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = w
    ws.freeze_panes = "B2"

    cycles = await asyncio.to_thread(_store().list_cycles, "", 5000)
    cs = wb.create_sheet("账期")
    cs.append(["邮箱", "账期起", "账期止(续费日)", "计费周期", "套餐", "套餐价格(USD)", "周额度最新(%)", "周额度峰值(%)",
               "按需已用最新(USD)", "按需已用峰值(USD)", "按需余额(USD)", "采样次数", "首次采集", "最近采集"])
    for c in cycles:
        cs.append([
            c["email"], c["cycle_start"], c["cycle_end"], c["billing_period"], c["plan_name"], c["plan_price_usd"],
            c["weekly_used_pct"], c["peak_weekly_used_pct"], c["on_demand_used_usd"], c["peak_on_demand_used_usd"],
            c["on_demand_balance_usd"], c["samples"], _ts(c["first_seen_at"]), _ts(c["last_seen_at"]),
        ])
    for cell in cs[1]:
        cell.font = Font(bold=True)
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for idx, w in enumerate((30, 12, 14, 10, 10, 14, 14, 14, 16, 16, 14, 10, 20, 20), start=1):
        cs.column_dimensions[get_column_letter(idx)].width = w
    cs.freeze_panes = "B2"

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    name = f"devin_usage_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
