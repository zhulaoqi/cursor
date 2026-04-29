"""FastAPI Web 服务 — Cursor Account Manager 二期。

端点：
  GET  /                           → 前端页面 (index.html)
  POST /api/upload                 → 上传 CSV/Excel，返回解析出的账号列表
  POST /api/run                    → 执行拉取任务，SSE 流式推送进度
  GET  /api/stream/{task_id}       → SSE 进度流
  GET  /api/task/{task_id}         → 查询任务快照（刷新后用于重连恢复）
  GET  /api/download/{token}       → 下载汇总 Excel 文件
  GET  /api/download_zip/{task_id} → 打包下载所有账号文件（ZIP）
  GET  /api/status                 → 服务状态
"""

from __future__ import annotations

import asyncio
import csv
import datetime
import io
import json
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import zipfile

import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import exporter, fetcher
from .account_store import load_accounts
from .config import SETTINGS
from .logger import get
from .models import Account, AccountSnapshot

log = get("web")


def _purge_old_tmp(max_age_sec: int = 3600) -> None:
    """删除 Temp 目录下所有 cam_web_* 且修改时间超过 max_age_sec 的目录。"""
    import shutil as _shutil
    try:
        tmp_root = Path(tempfile.gettempdir())
        cutoff = time.time() - max_age_sec
        for d in tmp_root.glob("cam_web_*"):
            if d.is_dir() and d.stat().st_mtime < cutoff:
                _shutil.rmtree(d, ignore_errors=True)
                log.info(f"已清理旧临时目录: {d.name}")
    except Exception as _e:
        log.debug(f"清理临时目录失败（忽略）: {_e}")


def _schedule_cleanup(path: Path, delay_sec: int = 600) -> None:
    """delay_sec 秒后在后台线程删除 path 目录。"""
    import shutil as _shutil

    def _do():
        time.sleep(delay_sec)
        try:
            if path.exists():
                _shutil.rmtree(path, ignore_errors=True)
                log.info(f"已定时清理临时目录: {path.name}")
        except Exception as _e:
            log.debug(f"定时清理失败（忽略）: {_e}")

    t = threading.Thread(target=_do, daemon=True)
    t.start()


# ─── 应用 ────────────────────────────────────────────────────────
app = FastAPI(title="Cursor Account Manager", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC), name="static")


# ─── 运行中任务状态 ────────────────────────────────────────────────
_tasks: Dict[str, Dict[str, Any]] = {}
_download_files: Dict[str, Path] = {}
_task_lock = threading.Lock()

IMAP_HOST_DEFAULT = SETTINGS.default_imap_host
IMAP_PORT_DEFAULT = SETTINGS.default_imap_port
BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))


# ─── 数据模型 ─────────────────────────────────────────────────────

class AccountRow(BaseModel):
    email: str
    imap_password: str
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None


class RunRequest(BaseModel):
    accounts: List[AccountRow]
    month: Optional[str] = None        # "YYYY-MM" 账单月份：发票过滤 + 文件命名
    date_from: Optional[str] = None    # "YYYY-MM-DD" 使用明细起始日（含），转 Unix 秒
    date_to: Optional[str] = None      # "YYYY-MM-DD" 使用明细结束日（含）
    with_invoices: bool = True
    with_summary: bool = True
    with_raw: bool = False


# ─── 辅助 ─────────────────────────────────────────────────────────

def _parse_date_range_to_utc_ts(
    date_from: Optional[str],
    date_to: Optional[str],
) -> tuple[Optional[int], Optional[int]]:
    """把前端选择的北京时间自然日转换成 Cursor API 需要的 UTC 秒时间戳。"""
    start_ts: Optional[int] = None
    end_ts: Optional[int] = None
    if date_from:
        start_dt = datetime.datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=BEIJING_TZ)
        start_ts = int(start_dt.timestamp())
    if date_to:
        end_dt = (
            datetime.datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=BEIJING_TZ)
            + datetime.timedelta(days=1, seconds=-1)
        )
        end_ts = int(end_dt.timestamp())
    return start_ts, end_ts


def _fetch_targets_for_run(*, with_summary: bool) -> tuple[str, ...]:
    """根据导出选项决定本次需要拉取的 API 数据项。"""
    if with_summary:
        return fetcher.DEFAULT_WHAT
    return tuple(item for item in fetcher.DEFAULT_WHAT if item != "usage_events")


def _should_mark_accounts_done_after_export(*, with_invoices: bool) -> bool:
    """没有账单下载阶段时，Web 层需要在导出完成后补账号终态。"""
    return not with_invoices


def _has_zip_output_requested(*, with_summary: bool, with_invoices: bool, with_raw: bool) -> bool:
    """ZIP 里至少会有一种用户请求的导出内容。"""
    return with_summary or with_invoices or with_raw


def _normalize_email(email: str) -> str:
    """统一邮箱：去空白/零宽字符并转小写，避免 CSV/Excel 导入更新不命中。"""
    if not email:
        return ""
    s = str(email).strip().lower()
    # 常见不可见字符：BOM / 零宽空格 / 零宽连接符
    s = re.sub(r"[\u200b\u200c\u200d\ufeff\u2060]", "", s)
    # 邮箱中不应包含空白，出现时直接移除
    s = re.sub(r"\s+", "", s)
    return s


def _parse_csv_bytes(data: bytes) -> List[AccountRow]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows: List[AccountRow] = []
    for r in reader:
        # CSV 列名兼容：大小写/前后空白
        d = {(str(k).strip().lower() if k is not None else ""): (v or "") for k, v in r.items()}
        email = _normalize_email(d.get("email", ""))
        pw = (
            d.get("imap_password")
            or d.get("imap_pwd")
            or d.get("password")
            or ""
        ).strip()
        if not email or not pw:
            continue
        host = (d.get("imap_host") or "").strip() or None
        port_raw = (d.get("imap_port") or "").strip()
        port = int(port_raw) if port_raw.isdigit() else None
        rows.append(AccountRow(email=email, imap_password=pw, imap_host=host, imap_port=port))
    return rows


def _parse_excel_bytes(data: bytes) -> List[AccountRow]:
    from openpyxl import load_workbook
    # data_only=True：读取公式格子的缓存值而非公式本身
    wb = load_workbook(io.BytesIO(data), data_only=True)

    # 尝试所有 sheet，取解析出账号数最多的那个
    best_rows: List[AccountRow] = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        headers: List[str] = []
        rows: List[AccountRow] = []
        for row_idx, row in enumerate(ws.iter_rows(values_only=True)):
            # 跳过全空行
            if all(c is None or str(c).strip() == "" for c in row):
                continue
            if not headers:
                headers = [str(c).strip().lower() if c is not None else "" for c in row]
                # 必须包含 email 列才认为是数据表头
                if "email" not in headers:
                    break
                continue
            d = dict(zip(headers, row))
            email = _normalize_email(str(d.get("email") or ""))
            pw = str(
                d.get("imap_password") or d.get("imap_pwd") or d.get("password") or ""
            ).strip()
            if not email or not pw or email.lower() in ("none", "email"):
                continue
            host_v = d.get("imap_host")
            host = str(host_v).strip() if host_v and str(host_v).strip() not in ("None", "") else None
            port_v = d.get("imap_port")
            port = int(port_v) if port_v and str(port_v).strip().isdigit() else None
            rows.append(AccountRow(email=email, imap_password=pw, imap_host=host, imap_port=port))
        if len(rows) > len(best_rows):
            best_rows = rows
        log.debug(f"Excel sheet '{sheet_name}': 解析到 {len(rows)} 个账号")

    log.info(f"Excel 解析完成，共 {len(best_rows)} 个有效账号（{len(wb.sheetnames)} 个 sheet）")
    return best_rows


def _safe_filename(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]+", "_", s)


# ─── 路由 ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = _STATIC / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.post("/api/upload")
async def upload_accounts(file: UploadFile = File(...)):
    """解析上传的 CSV / Excel，返回识别到的账号列表（不执行任何拉取）。"""
    data = await file.read()
    fname = (file.filename or "").lower()
    try:
        if fname.endswith(".csv") or fname.endswith(".txt"):
            rows = _parse_csv_bytes(data)
        elif fname.endswith(".xlsx") or fname.endswith(".xls"):
            rows = _parse_excel_bytes(data)
        else:
            # 先尝试 CSV，失败再 Excel
            try:
                rows = _parse_csv_bytes(data)
            except Exception:
                rows = _parse_excel_bytes(data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"文件解析失败: {e}")

    if not rows:
        raise HTTPException(status_code=422, detail="未解析到有效账号，请检查文件格式")

    return {"count": len(rows), "accounts": [r.model_dump() for r in rows]}


@app.post("/api/run")
async def run_task(req: RunRequest):
    """启动拉取任务，返回 task_id，前端用 /api/stream/{task_id} 监听进度。"""
    if not req.accounts:
        raise HTTPException(status_code=400, detail="账号列表为空")

    task_id = secrets.token_hex(8)
    with _task_lock:
        _tasks[task_id] = {
            "status": "pending",
            "total": len(req.accounts),
            "done": 0,
            "ok": [],
            "fail": [],
            "events": [],        # SSE 消息队列（滑动窗口，最多保留 _MAX_EVENTS 条）
            "events_offset": 0,  # 已被裁剪掉的条数，SSE cursor 需加上此偏移
            "started_at": time.time(),
            "finished_at": None,
        }

    def _worker():
        task = _tasks[task_id]
        task["status"] = "running"
        _push(task_id, "start", {"total": len(req.accounts)})

        # 解析日期范围 → Unix 秒时间戳
        start_ts: Optional[int] = None
        end_ts: Optional[int] = None
        try:
            start_ts, end_ts = _parse_date_range_to_utc_ts(req.date_from, req.date_to)
        except Exception as e:
            log.warning(f"日期解析失败，忽略日期过滤: {e}")

        accounts = [
            Account(
                email=a.email,
                imap_password=a.imap_password,
                imap_host=a.imap_host or IMAP_HOST_DEFAULT,
                imap_port=a.imap_port or IMAP_PORT_DEFAULT,
            )
            for a in req.accounts
        ]

        import concurrent.futures

        snaps: List[AccountSnapshot] = []
        snap_lock = threading.Lock()

        # 并发数：fetch 阶段全为 HTTP API 调用，无浏览器开销，可以比 browser 并发更激进。
        # 上限取 api_concurrency × 2（不超过 30），避免线程过多反而增加调度开销。
        # 浏览器登录依然由 _LOGIN_SEMAPHORE 控制，不受此值影响。
        api_workers = min(len(accounts), max(SETTINGS.api_concurrency * 2, 20), 30)
        workers = max(SETTINGS.browser_login_concurrency, api_workers)

        # 记录拉取阶段有 warn 的账号，export 完成后保留 warn 标志
        _warn_emails: set[str] = set()

        fetch_targets = _fetch_targets_for_run(with_summary=req.with_summary)

        def _fetch_one(acc: Account):
            _push(task_id, "progress", {"email": acc.email, "phase": "fetching"})
            try:
                snap = fetcher.fetch_one(
                    acc,
                    what=fetch_targets,
                    start_ts=start_ts,
                    end_ts=end_ts,
                )
                has_errors = bool(snap.errors)
                err_keys   = ",".join(snap.errors.keys()) if has_errors else ""
                with snap_lock:
                    snaps.append(snap)
                    task["done"] += 1
                    if has_errors:
                        _warn_emails.add(acc.email)
                        task["fail"].append({"email": acc.email, "error": err_keys})
                if has_errors:
                    for k, v in snap.errors.items():
                        log.warning(f"[{acc.email}] 拉取失败 {k}: {v}")
                    _push(task_id, "progress", {
                        "email": acc.email, "phase": "fetched_warn",
                        "msg": f"拉取部分失败: {err_keys}",
                    })
                else:
                    # 拉取完成，等待后续账单下载后再标 done
                    _push(task_id, "progress", {"email": acc.email, "phase": "fetched"})
            except Exception as e:
                with snap_lock:
                    task["done"] += 1
                    task["fail"].append({"email": acc.email, "error": str(e)})
                _push(task_id, "progress", {"email": acc.email, "phase": "error", "msg": str(e)})

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_fetch_one, acc) for acc in accounts]
            for f in concurrent.futures.as_completed(futs):
                try:
                    f.result()
                except Exception as e:
                    log.exception(f"并发任务异常: {e}")

        # 按账单月份过滤发票（month = "YYYY-MM"）
        # 优先用 period_start（账单周期开始），避免 created（发票生成时间＝周期末）导致月份偏移
        invoice_month = req.month or None
        log.info(f"发票月份过滤: req.month={req.month!r}")
        if invoice_month and snaps:
            for snap in snaps:
                if not snap.invoices:
                    continue
                log.info(f"[{snap.email}] 过滤前发票数={len(snap.invoices)}, "
                         f"示例字段={list(snap.invoices[0].keys())[:12] if snap.invoices else []}")
                filtered = []
                for inv in snap.invoices:
                    # period_start → periodStart → period_end → created → createdAt
                    ts_raw = (
                        inv.get("period_start") or inv.get("periodStart") or
                        inv.get("period_end")   or inv.get("periodEnd")   or
                        inv.get("created")      or inv.get("createdAt")   or 0
                    )
                    try:
                        ts = int(ts_raw)
                    except (TypeError, ValueError):
                        ts = 0
                    if ts:
                        # 兼容毫秒时间戳
                        if ts > 10 ** 12:
                            ts //= 1000
                        inv_month = datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m")
                        log.debug(f"  发票 ts={ts} → {inv_month}, 目标={invoice_month}")
                        if inv_month == invoice_month:
                            filtered.append(inv)
                    else:
                        # 无法识别日期时，仅在未指定月份时保留（不乱过滤）
                        log.debug(f"  发票无可识别日期字段: {list(inv.keys())}")
                log.info(f"[{snap.email}] 过滤后发票数={len(filtered)}")
                snap.invoices = filtered

        # 生成文件
        if snaps:
            label = req.month or time.strftime("%Y-%m")

            # 清理 1 小时前的旧临时目录（防止本次 startup 之后又有旧任务残留）
            _purge_old_tmp(max_age_sec=3600)

            out_dir = Path(tempfile.mkdtemp(prefix="cam_web_"))

            def _export_cb(email: str, phase: str, msg: str = "") -> None:
                """exporter 阶段回调 → 推送 SSE 进度事件（多线程安全）。"""
                if email:
                    if phase == "done":
                        # warn 账号完成后保持 warn_done，ok 账号才算 done
                        is_warn = email in _warn_emails   # set 读取（GIL 保护）
                        if is_warn:
                            _push(task_id, "progress", {
                                "email": email, "phase": "warn_done",
                                "msg": msg or "部分成功",
                            })
                        else:
                            with snap_lock:               # 与 fetch 阶段用同一把锁
                                task["ok"].append(email)
                            _push(task_id, "progress", {
                                "email": email, "phase": "done", "msg": msg,
                            })
                    else:
                        _push(task_id, "progress", {
                            "email": email, "phase": phase, "msg": msg,
                        })
                else:
                    # 全局阶段（生成汇总 / ZIP 就绪）
                    _push(task_id, "global_phase", {"phase": phase, "msg": msg})

            # 限制 export 阶段同时打开的浏览器数，使用账单下载独立并发。
            _browser_sem = threading.Semaphore(
                max(1, SETTINGS.invoice_download_concurrency)
            )
            try:
                exporter.export_per_account(
                    accounts, snaps, out_dir,
                    with_raw=req.with_raw,
                    with_invoices=req.with_invoices,
                    with_detail_xlsx=False,
                    with_full_summary_xlsx=False,
                    with_summary=req.with_summary,
                    invoice_month=req.month or "",
                    start_date=req.date_from or "",
                    end_date=req.date_to or "",
                    progress_cb=_export_cb,
                    browser_semaphore=_browser_sem,
                )
                if _should_mark_accounts_done_after_export(with_invoices=req.with_invoices):
                    for snap in snaps:
                        _export_cb(snap.email, "done", "")
                # export 完成后立即释放大字段内存，防止 300 账号 CSV 文本堆积
                for _s in snaps:
                    _s.usage_csv_text = None  # type: ignore[assignment]
                    _s.usage_events = None    # type: ignore[assignment]

                dl_token: Optional[str] = None
                if req.with_summary:
                    dl_token = secrets.token_hex(12)
                    summary_xlsx = out_dir / "汇总.xlsx"
                    with _task_lock:                      # _download_files 是全局共享 dict
                        _download_files[dl_token] = summary_xlsx
                has_zip = _has_zip_output_requested(
                    with_summary=req.with_summary,
                    with_invoices=req.with_invoices,
                    with_raw=req.with_raw,
                )
                with snap_lock:                           # task 字段写入用 worker 内部锁
                    task["download_token"] = dl_token
                    task["out_dir"] = str(out_dir)
                    task["has_zip"] = has_zip
                _push(task_id, "ready", {
                    "download_token": dl_token,
                    "has_zip": has_zip,
                    "label": label,
                })
            except Exception as e:
                log.exception("生成文件失败")
                _push(task_id, "error", {"msg": f"生成 Excel 失败: {e}"})

        task["status"] = "finished"
        task["finished_at"] = time.time()
        _push(task_id, "finished", {
            "ok": len(task["ok"]),
            "fail": len(task["fail"]),
        })

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return {"task_id": task_id}


_MAX_EVENTS = 2000  # 滑动窗口上限：超出后裁掉最旧的一半，防止内存无限增长


def _push(task_id: str, event_type: str, data: dict):
    with _task_lock:
        task = _tasks.get(task_id)
        if task is None:
            return
        task["events"].append({"type": event_type, "data": data})
        # 超过上限时裁掉最旧的一半，同时更新偏移量
        if len(task["events"]) > _MAX_EVENTS:
            trim = _MAX_EVENTS // 2
            task["events"] = task["events"][trim:]
            task["events_offset"] = task.get("events_offset", 0) + trim


@app.get("/api/stream/{task_id}")
async def stream_task(task_id: str):
    """SSE 端点，实时推送任务进度。"""
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def event_generator():
        abs_cursor = 0  # 绝对游标（与 events_offset 配合，不受裁剪影响）
        while True:
            with _task_lock:
                task = _tasks.get(task_id, {})
                events = task.get("events", [])
                offset = task.get("events_offset", 0)
                # 转换为 events 列表内的相对下标，防止越界
                rel = max(0, abs_cursor - offset)
                new_events = events[rel:]
                abs_cursor = offset + len(events)  # 推进到当前末尾
                is_done = task.get("status") == "finished"

            for ev in new_events:
                payload = json.dumps(ev["data"], ensure_ascii=False)
                yield f"event: {ev['type']}\ndata: {payload}\n\n"

            if is_done and not new_events:
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/download/{token}")
async def download_file(token: str):
    path = _download_files.get(token)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="文件不存在或已过期")
    fname = f"cursor_accounts_{time.strftime('%Y%m%d_%H%M')}.xlsx"
    # 下载后 2 小时清理整个任务目录（Excel 的父目录即 out_dir）
    _schedule_cleanup(path.parent.parent, delay_sec=7200)
    return FileResponse(path, filename=fname, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/task/{task_id}")
async def get_task(task_id: str):
    """返回任务快照，前端刷新后用于恢复进度界面。"""
    with _task_lock:
        task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    # 重连时只返回最新 500 条 events，避免大账号批次传输 MB 级 JSON
    all_events = task.get("events", [])
    offset = task.get("events_offset", 0)
    tail_count = min(500, len(all_events))
    tail_events = all_events[-tail_count:] if tail_count else []
    tail_offset = offset + (len(all_events) - tail_count)
    return {
        "status":         task.get("status"),
        "total":          task.get("total", 0),
        "done":           task.get("done", 0),
        "ok":             task.get("ok", []),
        "fail":           task.get("fail", []),
        "download_token": task.get("download_token"),
        "has_zip":        bool(task.get("has_zip") and task.get("out_dir") and Path(task["out_dir"]).exists()),
        "events":         tail_events,
        "events_offset":  tail_offset,  # 前端重连时以此作为初始 abs_cursor
    }


@app.get("/api/download_zip/{task_id}")
async def download_zip(task_id: str):
    """将任务输出目录打包成 ZIP 供下载（汇总.xlsx + 平铺 PDF）。"""
    with _task_lock:
        task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    out_dir = task.get("out_dir")
    if not out_dir or not Path(out_dir).exists():
        raise HTTPException(status_code=404, detail="输出文件不存在或已清理")

    out_path = Path(out_dir)

    def iter_zip():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            # 根目录：汇总.xlsx（各账号 Token 汇总）
            summary_file = out_path / "汇总.xlsx"
            if summary_file.exists():
                zf.write(summary_file, "汇总.xlsx")

            # 根目录平铺 PDF（不再打包账号明细 Excel / 账号子目录）
            used_names: set[str] = set()
            for acc_dir in sorted(out_path.iterdir()):
                if not acc_dir.is_dir():
                    continue
                invoices_dir = acc_dir / "invoices"
                pdf_src = invoices_dir if invoices_dir.exists() else acc_dir
                for pdf in sorted(pdf_src.glob("*.pdf")):
                    target_name = pdf.name
                    if target_name in used_names:
                        stem, suf = pdf.stem, pdf.suffix
                        n = 2
                        while f"{stem}_{n}{suf}" in used_names:
                            n += 1
                        target_name = f"{stem}_{n}{suf}"
                    used_names.add(target_name)
                    zf.write(pdf, target_name)

                raw_json = acc_dir / "raw.json"
                if raw_json.exists():
                    zf.write(raw_json, f"{acc_dir.name}/raw.json")

        buf.seek(0)
        yield buf.read()
        # ZIP 已写入内存，安排 2 小时后清理磁盘文件
        _schedule_cleanup(out_path, delay_sec=7200)

    fname = f"cursor_account_spending_{time.strftime('%Y%m%d_%H%M')}.zip"
    return StreamingResponse(
        iter_zip(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ─── 账号库 CRUD ───────────────────────────────────────────────────

@app.get("/api/accounts")
async def list_accounts_api():
    """返回账号库中所有账号（含 token 状态）。"""
    from .token_store import get_default_store
    store = get_default_store()
    accounts = store.list_accounts()
    token_rows = {r.email: r for r in store.list_all()}
    for acc in accounts:
        rec = token_rows.get(acc["email"])
        acc["token_status"] = rec.status if rec else "unknown"
        acc["token_failures"] = rec.consecutive_failures if rec else 0
    return {"accounts": accounts}


class SaveAccountsRequest(BaseModel):
    accounts: List[AccountRow]
    overwrite: bool = False
    source: str = "upload"


@app.post("/api/accounts/save")
async def save_accounts_api(req: SaveAccountsRequest):
    """将账号持久化到数据库。
    overwrite=False 时跳过已存在的邮箱；overwrite=True 时强制更新。
    返回 saved / skipped 数量及列表。
    """
    from .token_store import get_default_store
    store = get_default_store()
    existing_rows = store.list_accounts()
    # lower(email) -> stored_email（保留原值用于 overwrite 时清理历史大小写记录）
    existing_ci = {_normalize_email(a["email"]): a["email"] for a in existing_rows}

    # 批次内去重：同邮箱（大小写不同）只保留最后一条，行为与 upsert 一致
    latest_by_email: Dict[str, AccountRow] = {}
    for acc in req.accounts:
        norm = _normalize_email(acc.email)
        if not norm:
            continue
        latest_by_email[norm] = AccountRow(
            email=norm,
            imap_password=acc.imap_password,
            imap_host=acc.imap_host,
            imap_port=acc.imap_port,
        )

    saved: List[str] = []
    skipped: List[str] = []
    for norm_email, acc in latest_by_email.items():
        existing_email = existing_ci.get(norm_email)
        if existing_email and not req.overwrite:
            skipped.append(norm_email)
            continue
        # 兼容历史脏数据：若仅大小写不同，先删旧记录再写标准化邮箱
        if existing_email and existing_email != norm_email:
            store.delete_account(existing_email)
        store.upsert_account(
            email=norm_email,
            imap_password=acc.imap_password,
            imap_host=acc.imap_host or IMAP_HOST_DEFAULT,
            imap_port=acc.imap_port or IMAP_PORT_DEFAULT,
            source=req.source,
        )
        saved.append(norm_email)
        existing_ci[norm_email] = norm_email

    log.info(f"账号库更新：保存 {len(saved)} 个，跳过 {len(skipped)} 个")
    return {"saved": len(saved), "skipped": len(skipped),
            "saved_emails": saved, "skipped_emails": skipped}


@app.delete("/api/accounts/{email:path}")
async def delete_account_api(email: str):
    """从账号库删除指定账号（不影响 tokens 记录）。"""
    from .token_store import get_default_store
    store = get_default_store()
    store.delete_account(email)
    log.info(f"账号库删除：{email}")
    return {"ok": True, "email": email}


class ResetRequest(BaseModel):
    emails: List[str]


@app.post("/api/reset")
async def reset_accounts(req: ResetRequest):
    """重置账号状态：清除 disabled 标记和失败计数，下次运行时重新尝试登录。"""
    from .token_manager import get_default_manager
    mgr = get_default_manager()
    results = []
    for email in req.emails:
        try:
            mgr.store.reset(email)
            results.append({"email": email, "ok": True})
            log.info(f"[{email}] 账号状态已重置")
        except Exception as e:
            results.append({"email": email, "ok": False, "error": str(e)})
    return {"results": results}


@app.get("/api/account_status")
async def account_status(emails: str = ""):
    """查询账号在 tokens.db 里的状态（disabled/active/无记录）。"""
    from .token_manager import get_default_manager
    mgr = get_default_manager()
    email_list = [e.strip() for e in emails.split(",") if e.strip()]
    out = {}
    for email in email_list:
        rec = mgr.store.get(email)
        if rec is None:
            out[email] = {"status": "unknown", "failures": 0}
        else:
            out[email] = {"status": rec.status, "failures": rec.consecutive_failures}
    return out


@app.get("/api/status")
async def api_status():
    with _task_lock:
        running = sum(1 for t in _tasks.values() if t["status"] == "running")
        finished = sum(1 for t in _tasks.values() if t["status"] == "finished")
    return {"running": running, "finished": finished, "total_tasks": len(_tasks)}


def serve(host: str = "0.0.0.0", port: int = 8765, reload: bool = False):
    uvicorn.run("cam.web_server:app", host=host, port=port, reload=reload)
