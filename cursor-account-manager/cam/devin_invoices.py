"""Devin 发票导出：登录各账号读取发票页 → 下载每张发票的 PDF → 打包成 zip，一次性下载（不入库、不留存）。

发票按需导出：每次导出都是实时从 app.devin.ai 读取，**不在服务器上保存历史**：
- 下载的 PDF 先放在本次导出的临时工作目录，打包后立即删除；
- zip 放在系统临时目录，只保留“最近一次导出”这一份待下载文件；
- 页面点一次“下载”，文件流式返回后立即删除；没人下载 ``READY_TTL_SEC`` 秒后也会自动清掉；
- 想再下一次就重新导出。

zip 内容：``<账号>/<日期>-<发票号>.pdf`` + ``清单.csv``（含没拿到 PDF 的原因和读取失败的账号）。
不写数据库。账期才入库（见 ``devin_store.devin_cycles``）。

与每日采集共用进程内锁：导出期间不会同时跑采集，避免两个任务争用同一个 Chrome profile。
"""

from __future__ import annotations

import csv
import functools
import io
import re
import shutil
import tempfile
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
READY_TTL_SEC = 1800          # 导出完成后 30 分钟没人下载就自动清掉
_STALE_SEC = 2 * 3600         # 兜底清理：临时目录里超过 2 小时的残留（如下载中途断开）

_progress_lock = threading.Lock()
_progress: dict[str, Any] = {
    "running": False, "total": 0, "done": 0, "ok": 0, "failed": 0,
    "current": [], "started_at": 0, "updated_at": 0, "last_error": "",
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
    zip_path: str = ""
    total: int = 0
    success: int = 0
    failed: int = 0
    invoice_count: int = 0
    pdf_count: int = 0
    pdf_missing: int = 0
    amount_total_usd: float = 0.0
    lock_busy: bool = False
    failures: list[dict[str, Any]] = field(default_factory=list)


# ─── 待下载文件（只保留最近一次导出）────────────────────────────────

_ready_lock = threading.Lock()
_ready: dict[str, Any] | None = None


def _work_root() -> Path:
    root = Path(tempfile.gettempdir()) / "cam-devin-invoices"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _drop_file(path: str | Path | None) -> None:
    if path:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as exc:
            log.debug(f"删除临时文件失败 {path}: {exc}")


def sweep_stale(max_age_sec: int = _STALE_SEC) -> None:
    """清理临时目录里的陈旧残留（崩溃/下载中断留下的 zip 或工作目录）。当前待下载的文件不动。"""
    root = Path(tempfile.gettempdir()) / "cam-devin-invoices"
    if not root.exists():
        return
    with _ready_lock:
        keep = _ready["path"] if _ready else ""
    cutoff = time.time() - max_age_sec
    for p in root.iterdir():
        try:
            if str(p) == keep or p.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
        except OSError:
            pass


def _set_ready(path: Path, summary: InvoiceExportSummary) -> None:
    """登记新的待下载文件；上一份（没人下载的）直接作废并删除。"""
    global _ready
    with _ready_lock:
        old = _ready["path"] if _ready else ""
        _ready = {
            "path": str(path), "name": path.name, "size": path.stat().st_size, "created_at": int(time.time()),
            "invoices": summary.invoice_count, "pdf": summary.pdf_count, "pdf_missing": summary.pdf_missing,
            "accounts": summary.total, "failed_accounts": summary.failed,
        }
    if old and old != str(path):
        _drop_file(old)


def _expire_ready_locked() -> str:
    """调用方持有 _ready_lock；过期则清空并返回需要删除的文件路径。"""
    global _ready
    if _ready and time.time() - _ready["created_at"] > READY_TTL_SEC:
        path = _ready["path"]
        _ready = None
        return path
    return ""


def get_ready() -> dict[str, Any] | None:
    """页面展示用：有待下载的 zip 时返回摘要（不含服务器路径），否则 None。"""
    with _ready_lock:
        expired = _expire_ready_locked()
        info = {k: v for k, v in _ready.items() if k != "path"} if _ready else None
    _drop_file(expired)
    return info


def take_ready() -> tuple[Path, str] | None:
    """取走待下载文件（取走即清空，**只能下载一次**）。返回 (路径, 文件名)；没有则 None。
    调用方负责在响应发送完后删除该文件。"""
    global _ready
    with _ready_lock:
        expired = _expire_ready_locked()
        taken = _ready
        _ready = None
    _drop_file(expired)
    if not taken:
        return None
    path = Path(taken["path"])
    if not path.is_file():
        return None
    return path, taken["name"]


def clear_ready() -> None:
    """新导出开始时作废上一份没下载的文件。"""
    taken = take_ready()
    if taken:
        _drop_file(taken[0])


def discard_file(path: str | Path) -> None:
    """下载响应发完后的清理钩子。"""
    _drop_file(path)


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
    publish: bool = True,
) -> InvoiceExportSummary:
    """读取启用账号（或指定账号）的发票并下载 PDF、打包 zip；返回汇总。

    ``publish=True``（网页）：zip 登记为“待下载”，下载一次即删。
    ``publish=False``（CLI）：只返回 ``summary.zip_path``，由调用方自己搬走/删除。
    """
    store = store or get_default_devin_store()
    cfg = devin_settings()
    summary = InvoiceExportSummary()
    if not _run_lock.acquire(blocking=False):
        summary.lock_busy = True
        return summary
    workdir: Path | None = None
    try:
        if publish:
            clear_ready()
        sweep_stale()
        workdir = Path(tempfile.mkdtemp(prefix="run-", dir=_work_root()))
        fetcher = fetcher or functools.partial(collect_invoices, workdir=workdir)
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
                stamp = time.strftime("%Y%m%d-%H%M%S")
                path = _work_root() / f"{FILE_PREFIX}{stamp}.zip"
                n = 1
                while path.exists():          # 同一秒内重名（如 CLI 并发）不互相覆盖
                    n += 1
                    path = _work_root() / f"{FILE_PREFIX}{stamp}-{n}.zip"
                path.write_bytes(build_invoice_zip(rows, summary.failures))
                summary.file, summary.zip_path = path.name, str(path)
                if publish:
                    _set_ready(path, summary)
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
            _set(running=False, current=[])
        return summary
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)   # PDF 原文件不留存，已全部进了 zip
        _run_lock.release()
