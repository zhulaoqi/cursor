"""Devin 发票导出：登录各账号读取发票页 → 下载每张发票的 PDF → 打包成 zip（不入库）。

发票按需导出：每次导出都是实时从 app.devin.ai 读取，结果只落在导出目录：
- ``devin-invoices-YYYYMMDD-HHMMSS.zip``：``<账号>/<日期>-<发票号>.pdf`` + ``清单.csv``（含没拿到 PDF 的原因）
- ``invoice-pdf/<账号>/``：本次下载的 PDF 原文件（zip 的来源，同名会被下次导出覆盖）

不写数据库。账期才入库（见 ``devin_store.devin_cycles``）。

与每日采集共用进程内锁：导出期间不会同时跑采集，避免两个任务争用同一个 Chrome profile。
"""

from __future__ import annotations

import csv
import io
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .devin_collector import InvoiceResult, collect_invoices
from .devin_config import devin_settings
from .devin_errors import error_label
from .devin_runner import _run_lock
from .devin_store import DevinStore, get_default_devin_store
from .logger import get

log = get("devin_invoices")

FILE_PREFIX = "devin-invoices-"
# zip 是当前格式；xlsx 是早期版本导出的文件，仍允许列出/下载
_FILE_RE = re.compile(r"^devin-invoices-[0-9]{8}-[0-9]{6}\.(?:zip|xlsx)$")

_progress_lock = threading.Lock()
_progress: dict[str, Any] = {
    "running": False, "total": 0, "done": 0, "ok": 0, "failed": 0,
    "current": [], "started_at": 0, "updated_at": 0, "last_file": "", "last_error": "",
    "last_invoices": 0, "last_pdf_missing": 0,
}


def get_invoice_progress() -> dict[str, Any]:
    with _progress_lock:
        snap = dict(_progress)
        snap["current"] = list(_progress["current"])
        return snap


def _set(**kwargs: Any) -> None:
    with _progress_lock:
        _progress.update(kwargs)
        _progress["updated_at"] = int(time.time())


@dataclass
class InvoiceExportSummary:
    file: str = ""
    total: int = 0
    success: int = 0
    failed: int = 0
    invoice_count: int = 0
    pdf_count: int = 0
    pdf_missing: int = 0
    amount_total_usd: float = 0.0
    lock_busy: bool = False
    failures: list[dict[str, Any]] = field(default_factory=list)


def is_valid_export_name(name: str) -> bool:
    return bool(_FILE_RE.fullmatch(name or ""))


def list_exports(limit: int = 20) -> list[dict[str, Any]]:
    base = devin_settings().export_dir
    if not base.exists():
        return []
    files = [p for p in base.glob(f"{FILE_PREFIX}*") if p.is_file() and is_valid_export_name(p.name)]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {"name": p.name, "size": p.stat().st_size, "modified_at": int(p.stat().st_mtime)}
        for p in files[: max(1, limit)]
    ]


def export_path(name: str) -> Path | None:
    if not is_valid_export_name(name):
        return None
    path = devin_settings().export_dir / name
    return path if path.is_file() else None


_SOURCE_LABELS = {"direct": "官方直链", "download": "发票页下载", "print": "发票页打印（非官方文件）"}


def _safe_dir(email: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]+", "_", email)


def build_invoice_zip(rows: list[dict[str, Any]], failures: list[dict[str, Any]]) -> bytes:
    """rows: 每张发票一行（含 email、pdf_file）；failures: 读取失败的账号。

    zip 里只放 PDF（``<账号>/<文件名>.pdf``）和一份 ``清单.csv``。没拿到 PDF 的发票不放文件，
    但会在清单里写明原因，不会悄悄漏掉。
    """
    out = io.BytesIO()
    list_buf = io.StringIO()
    writer = csv.writer(list_buf)
    writer.writerow(["账号", "负责人", "发票日期", "状态", "金额(USD)", "发票号", "PDF文件", "PDF来源", "备注", "发票链接"])
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in sorted(rows, key=lambda x: (x["email"], x.get("invoice_date", ""))):
            arc = ""
            src = Path(r["pdf_file"]) if r.get("pdf_file") else None
            if src is not None and src.is_file():
                arc = f"{_safe_dir(r['email'])}/{src.name}"
                zf.write(src, arc)
            writer.writerow([
                r["email"], r.get("owner_email", ""), r.get("invoice_date", ""), r.get("status", ""),
                "" if r.get("amount_usd") is None else f"{r['amount_usd']:.2f}",
                r.get("number", ""), arc, _SOURCE_LABELS.get(r.get("pdf_source", ""), ""),
                "" if arc else (r.get("pdf_error") or "未取得 PDF"), r.get("invoice_url", ""),
            ])
        for f in failures:
            writer.writerow([f["email"], "", "", "", "", "", "", "", f"读取失败：{f.get('label', '')} {f.get('message', '')}".strip(), ""])
        # utf-8-sig：Excel 双击打开中文不乱码
        zf.writestr("清单.csv", "\ufeff" + list_buf.getvalue())
    return out.getvalue()


def run_invoice_export(
    *,
    emails: list[str] | None = None,
    store: DevinStore | None = None,
    fetcher: Callable[[dict[str, Any]], InvoiceResult] | None = None,
) -> InvoiceExportSummary:
    """读取启用账号（或指定账号）的发票并下载 PDF、打包 zip；返回汇总。"""
    store = store or get_default_devin_store()
    fetcher = fetcher or collect_invoices
    cfg = devin_settings()
    summary = InvoiceExportSummary()
    if not _run_lock.acquire(blocking=False):
        summary.lock_busy = True
        return summary
    try:
        wanted = {e.strip().lower() for e in emails or [] if e and e.strip()}
        accounts = store.list_accounts()
        if wanted:
            accounts = [a for a in accounts if a["email"].lower() in wanted]
        targets = [a for a in accounts if a["status"] == "active"]
        summary.total = len(targets)
        started = time.time()
        _set(running=True, total=len(targets), done=0, ok=0, failed=0, current=[],
             started_at=int(started), last_error="")
        rows: list[dict[str, Any]] = []
        lock = threading.Lock()

        def work(account: dict[str, Any]) -> None:
            email = account["email"]
            with _progress_lock:
                _progress["current"].append(email)
            try:
                try:
                    res = fetcher(account)
                except Exception as exc:  # noqa: BLE001
                    from .devin_errors import classify_exception

                    code, message = classify_exception(exc)
                    res = InvoiceResult(email=email, ok=False, error_code=code, error_message=message)
            finally:
                with _progress_lock:
                    if email in _progress["current"]:
                        _progress["current"].remove(email)
            with lock:
                if res.ok:
                    summary.success += 1
                    for inv in res.invoices:
                        rows.append({**inv, "email": email, "owner_email": account.get("owner_email", "")})
                else:
                    summary.failed += 1
                    summary.failures.append({
                        "email": email, "code": res.error_code or "unknown",
                        "label": error_label(res.error_code),
                        "message": (res.error_message or "").split(" [现场:")[0][:300],
                    })
                _set(done=summary.success + summary.failed, ok=summary.success, failed=summary.failed)

        try:
            if targets:
                with ThreadPoolExecutor(max_workers=cfg.concurrency, thread_name_prefix="cam-devin-inv") as pool:
                    list(pool.map(work, targets))
            summary.invoice_count = len(rows)
            summary.pdf_count = sum(1 for r in rows if r.get("pdf_file") and Path(r["pdf_file"]).is_file())
            summary.pdf_missing = summary.invoice_count - summary.pdf_count
            summary.amount_total_usd = round(sum((r.get("amount_usd") or 0) for r in rows), 2)
            if targets:
                cfg.export_dir.mkdir(parents=True, exist_ok=True)
                path = cfg.export_dir / f"{FILE_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}.zip"
                path.write_bytes(build_invoice_zip(rows, summary.failures))
                summary.file = path.name
                log.info(
                    f"Devin 发票导出完成 file={path.name} accounts={summary.total} ok={summary.success} "
                    f"failed={summary.failed} invoices={summary.invoice_count} pdf={summary.pdf_count} "
                    f"pdf_missing={summary.pdf_missing}"
                )
        except Exception as exc:
            _set(last_error=str(exc)[:300])
            log.exception("Devin 发票导出异常")
            raise
        finally:
            _set(running=False, current=[], last_file=summary.file,
                 last_invoices=summary.invoice_count, last_pdf_missing=summary.pdf_missing)
        return summary
    finally:
        _run_lock.release()
