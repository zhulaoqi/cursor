"""BI 每日同步任务编排。"""

from __future__ import annotations

import csv
import io
import json
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import fetcher
from .account_store import load_accounts
from .alerting import send_alert
from .config import SETTINGS
from .logger import get
from .models import Account
from .starrocks_loader import StarRocksLoader
from .sync_log_store import SyncLogStore, get_default_sync_log_store
from .token_store import get_default_store

log = get("bi_sync")

BJ_TZ = timezone(timedelta(hours=8))


def _kv_message(**kwargs: Any) -> str:
    parts: list[str] = []
    for k, v in kwargs.items():
        if v is None:
            continue
        text = str(v).replace("\n", " ").strip()
        parts.append(f"{k}={text}")
    return " ".join(parts)


def _err(code: str, detail: str) -> str:
    clean = str(detail).replace("\n", " ").strip()
    return f"{code}: {clean}"


@dataclass(frozen=True)
class SnapshotAccount:
    account: Account
    source: str
    is_new: bool


def _resolve_biz_date(biz_date: Optional[str]) -> str:
    if biz_date:
        return biz_date
    now_bj = datetime.now(BJ_TZ)
    return (now_bj - timedelta(days=1)).strftime("%Y-%m-%d")


def _biz_date_range_utc_seconds(biz_date: str) -> tuple[int, int]:
    day = datetime.strptime(biz_date, "%Y-%m-%d").replace(tzinfo=BJ_TZ)
    start_ts = int(day.timestamp())
    end_ts = int((day + timedelta(days=1, seconds=-1)).timestamp())
    return start_ts, end_ts


def _parse_datetime_like(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        if n > 10**12:
            return datetime.fromtimestamp(n / 1000, tz=timezone.utc)
        if n > 10**9:
            return datetime.fromtimestamp(n, tz=timezone.utc)
        return None
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _pick_value(record: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    if not record:
        return None
    lower = {str(k).strip().lower(): v for k, v in record.items()}
    for key in candidates:
        if key in lower:
            return lower[key]
    return None


def _to_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _rows_from_usage_csv(
    *,
    run_id: str,
    biz_date: str,
    email: str,
    csv_text: str,
) -> list[dict]:
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    out: list[dict] = []
    for row in reader:
        event_time = _parse_datetime_like(
            _pick_value(row, ("timestamp", "time", "event_time", "created_at", "date"))
        )
        if event_time is None:
            continue
        out.append(
            {
                "dt": biz_date,
                "run_id": run_id,
                "account_email": email,
                "event_time": event_time.replace(tzinfo=None),
                "first_api_request_id": _pick_value(
                    row,
                    ("first api request id", "first api req id", "request id", "request_id", "requestid", "id"),
                ),
                "environment": _pick_value(row, ("environment",)),
                "kind": _pick_value(row, ("kind",)),
                "model_name": _pick_value(row, ("model", "model_name")),
                "max_mode": _pick_value(row, ("max mode", "max_mode")),
                "input_tokens_wo_cache_write": _to_int(
                    _pick_value(
                        row,
                        ("input (w/o cache write)", "input_wo_cache_write"),
                    )
                ),
                "input_tokens_w_cache_write": _to_int(
                    _pick_value(
                        row,
                        ("input (w/ cache write)", "input_w_cache_write"),
                    )
                ),
                "output_tokens": _to_int(
                    _pick_value(
                        row,
                        ("output_tokens", "outputtokens", "output tokens", "output"),
                    )
                ),
                "total_tokens": _to_int(
                    _pick_value(row, ("total_tokens", "totaltokens", "total tokens"))
                ),
                "cost_usd": _to_float(
                    _pick_value(row, ("cost_usd", "cost", "total_cost_usd"))
                ),
                "raw_event_json": json.dumps(row, ensure_ascii=False),
            }
        )
    return out


def _rows_from_usage_events(
    *,
    run_id: str,
    biz_date: str,
    email: str,
    events: list[dict[str, Any]],
) -> list[dict]:
    rows: list[dict] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        ts = _to_int(ev.get("timestamp"))
        if not ts:
            continue
        event_time = _parse_datetime_like(ts)
        if event_time is None:
            continue
        token_usage = ev.get("tokenUsage") or {}
        rows.append(
            {
                "dt": biz_date,
                "run_id": run_id,
                "account_email": email,
                "event_time": event_time.replace(tzinfo=None),
                "first_api_request_id": ev.get("requestId") or ev.get("id"),
                "environment": ev.get("environment"),
                "kind": ev.get("kind"),
                "model_name": ev.get("model"),
                "max_mode": ev.get("maxMode"),
                "input_tokens_wo_cache_write": _to_int(
                    token_usage.get("inputTokensWithoutCacheWrite")
                    or token_usage.get("inputTokensWOCacheWrite")
                ),
                "input_tokens_w_cache_write": _to_int(
                    token_usage.get("inputTokensWithCacheWrite")
                    or token_usage.get("inputTokensWCacheWrite")
                    or token_usage.get("inputTokens")
                ),
                "output_tokens": _to_int(token_usage.get("outputTokens")),
                "total_tokens": _to_int(token_usage.get("totalTokens")),
                "cost_usd": _to_float(token_usage.get("totalCents")) / 100 if token_usage.get("totalCents") is not None else None,
                "raw_event_json": json.dumps(ev, ensure_ascii=False),
            }
        )
    return rows


def _snapshot_accounts(emails: Optional[tuple[str, ...]] = None, *, biz_date: str) -> list[SnapshotAccount]:
    store = get_default_store()
    rows = store.list_accounts()
    wanted = {e.strip().lower() for e in (emails or ()) if e.strip()}
    snap: list[SnapshotAccount] = []
    day_start = datetime.strptime(biz_date, "%Y-%m-%d").replace(tzinfo=BJ_TZ)
    day_start_ts = int(day_start.timestamp())
    for row in rows:
        email = str(row.get("email") or "").strip()
        if not email:
            continue
        if wanted and email.lower() not in wanted:
            continue
        acc = Account(
            email=email,
            imap_password=str(row.get("imap_password") or ""),
            imap_host=str(row.get("imap_host") or SETTINGS.default_imap_host),
            imap_port=int(row.get("imap_port") or SETTINGS.default_imap_port),
        )
        added_at = int(row.get("added_at") or 0)
        snap.append(
            SnapshotAccount(
                account=acc,
                source=str(row.get("source") or "db"),
                is_new=added_at >= day_start_ts if added_at else False,
            )
        )
    if snap:
        return snap

    # 兼容：账号库为空时回退 CSV
    fallback = load_accounts()
    out = []
    for acc in fallback:
        if wanted and acc.email.lower() not in wanted:
            continue
        out.append(SnapshotAccount(account=acc, source="csv", is_new=False))
    return out


def run_daily_sync(
    *,
    biz_date: Optional[str] = None,
    trigger_type: str = "manual",
    emails: Optional[tuple[str, ...]] = None,
    run_id: Optional[str] = None,
    log_store: Optional[SyncLogStore] = None,
) -> dict[str, Any]:
    if not SETTINGS.bi_sync_enable:
        raise RuntimeError("BI_SYNC_ENABLE=false，未开启 BI 同步")

    target_date = _resolve_biz_date(biz_date)
    start_ts, end_ts = _biz_date_range_utc_seconds(target_date)
    run_id = run_id or f"{target_date.replace('-', '')}_{secrets.token_hex(4)}"
    log_store = log_store or get_default_sync_log_store()
    loader = StarRocksLoader()
    snapshot = _snapshot_accounts(emails, biz_date=target_date)
    snapshot_total = len(snapshot)
    new_account_count = sum(1 for s in snapshot if s.is_new)
    log_store.create_run(
        run_id=run_id,
        biz_date=target_date,
        trigger_type=trigger_type,
        account_total=snapshot_total,
        account_snapshot_total=snapshot_total,
        new_account_count=new_account_count,
    )
    log_store.add_stage(run_id=run_id, stage="init", status="start")
    if snapshot_total == 0:
        msg = _err("E_SNAPSHOT_EMPTY", "account snapshot is empty")
        log_store.add_stage(run_id=run_id, stage="init", status="failed", message=msg)
        log_store.finish_run(
            run_id=run_id,
            status="failed",
            account_success=0,
            account_failed=0,
            event_total=0,
            ods_rows=0,
            dwd_rows=0,
            error_summary=msg,
        )
        result = {"run_id": run_id, "biz_date": target_date, "status": "failed", "message": msg}
        send_alert(
            "BI 日同步失败",
            f"run_id={run_id}\nbiz_date={target_date}\nreason={msg}",
            level="error",
        )
        return result
    log_store.add_stage(
        run_id=run_id,
        stage="init",
        status="success",
        message=_kv_message(snapshot_total=snapshot_total, new_account_count=new_account_count),
    )

    all_rows: list[dict] = []
    ok_count = 0
    fail_count = 0
    error_messages: list[str] = []
    fetch_fail_count = 0
    load_fail_count = 0
    load_ok_count = 0
    ods_rows = 0
    dwd_rows = 0
    log_store.add_stage(run_id=run_id, stage="prepare_partition", status="start")
    try:
        loader.ensure_tables()
        loader.ensure_biz_date_partitions_ready(biz_date=target_date)
        log_store.add_stage(
            run_id=run_id,
            stage="prepare_partition",
            status="success",
            message=_kv_message(biz_date=target_date, tables="ods,dwd"),
        )
    except Exception as e:
        msg = _err("E_PARTITION_PREPARE", f"{type(e).__name__}: {e}")
        log_store.add_stage(run_id=run_id, stage="prepare_partition", status="failed", message=msg)
        log_store.finish_run(
            run_id=run_id,
            status="failed",
            account_success=0,
            account_failed=snapshot_total,
            event_total=0,
            ods_rows=0,
            dwd_rows=0,
            error_summary=msg[:2000],
        )
        raise

    log_store.add_stage(run_id=run_id, stage="fetch", status="start")
    for item in snapshot:
        acc = item.account
        acc_start = int(time.time())
        attempt = 0
        last_err = ""
        account_rows: list[dict] = []
        while attempt < max(1, SETTINGS.bi_sync_retry_times):
            attempt += 1
            try:
                snap = fetcher.fetch_one(
                    acc,
                    what=("usage_events",),
                    start_ts=start_ts,
                    end_ts=end_ts,
                )
                if snap.errors:
                    last_err = _err(
                        "E_FETCH_ACCOUNT",
                        "; ".join(f"{k}={v}" for k, v in snap.errors.items()),
                    )
                    continue
                if snap.usage_csv_text:
                    account_rows = _rows_from_usage_csv(
                        run_id=run_id,
                        biz_date=target_date,
                        email=acc.email,
                        csv_text=snap.usage_csv_text,
                    )
                else:
                    account_rows = _rows_from_usage_events(
                        run_id=run_id,
                        biz_date=target_date,
                        email=acc.email,
                        events=snap.usage_events or [],
                    )
                break
            except Exception as e:
                last_err = _err("E_FETCH_EXCEPTION", f"{type(e).__name__}: {e}")
        acc_end = int(time.time())
        if account_rows or last_err == "":
            # 按账号+日期增量落库：账号拉完即入 ODS/DWD，避免整天全量在末尾一次性写入。
            try:
                normalized = [loader.normalize_decimal_fields(r) for r in account_rows]
                loaded_ods = loader.replace_ods_rows_for_account(
                    biz_date=target_date,
                    account_email=acc.email,
                    rows=normalized,
                )
                loaded_dwd = loader.rebuild_dwd_for_account_date(
                    biz_date=target_date,
                    account_email=acc.email,
                )
                ods_rows += loaded_ods
                dwd_rows += loaded_dwd
                load_ok_count += 1
                ok_count += 1
                all_rows.extend(account_rows)
                log_store.add_account_log(
                    run_id=run_id,
                    account_email=acc.email,
                    account_source=item.source,
                    is_new_account=item.is_new,
                    status="success",
                    started_at=acc_start,
                    ended_at=acc_end,
                    fetch_rows=len(account_rows),
                    load_rows=loaded_dwd,
                )
            except Exception as e:
                fail_count += 1
                load_fail_count += 1
                err = _err("E_LOAD_ACCOUNT", f"{type(e).__name__}: {e}")
                error_messages.append(err)
                log_store.add_account_log(
                    run_id=run_id,
                    account_email=acc.email,
                    account_source=item.source,
                    is_new_account=item.is_new,
                    status="failed",
                    started_at=acc_start,
                    ended_at=acc_end,
                    fetch_rows=len(account_rows),
                    load_rows=0,
                    error_message=err,
                )
                log.warning(
                    _kv_message(
                        event="account_load_failed",
                        run_id=run_id,
                        account_email=acc.email,
                        error=err,
                    )
                )
        else:
            fail_count += 1
            fetch_fail_count += 1
            err = last_err
            error_messages.append(err)
            log_store.add_account_log(
                run_id=run_id,
                account_email=acc.email,
                account_source=item.source,
                is_new_account=item.is_new,
                status="failed",
                started_at=acc_start,
                ended_at=acc_end,
                fetch_rows=0,
                load_rows=0,
                error_message=last_err,
            )
            log.warning(
                _kv_message(
                    event="account_fetch_failed",
                    run_id=run_id,
                    account_email=acc.email,
                    error=err,
                )
            )

    log_store.add_stage(
        run_id=run_id,
        stage="fetch",
        status="success" if fetch_fail_count == 0 else "failed",
        message=_kv_message(
            snapshot_total=snapshot_total,
            fetch_ok=ok_count,
            fetch_fail=fetch_fail_count,
            fetched_rows=len(all_rows),
        ),
    )
    log_store.add_stage(
        run_id=run_id,
        stage="load_ods",
        status="success" if load_fail_count == 0 else "failed",
        message=_kv_message(
            load_ok=load_ok_count,
            load_fail=load_fail_count,
            ods_rows=ods_rows,
        ),
    )
    log_store.add_stage(
        run_id=run_id,
        stage="load_dwd",
        status="success" if load_fail_count == 0 else "failed",
        message=_kv_message(
            load_ok=load_ok_count,
            load_fail=load_fail_count,
            dwd_rows=dwd_rows,
        ),
    )

    if fail_count == 0:
        status = "success"
    elif ok_count > 0:
        status = "partial_failed"
    else:
        status = "failed"
    log_store.add_stage(
        run_id=run_id,
        stage="finalize",
        status="success",
        message=_kv_message(
            final_status=status,
            account_success=ok_count,
            account_failed=fail_count,
            event_total=len(all_rows),
            ods_rows=ods_rows,
            dwd_rows=dwd_rows,
        ),
    )
    log_store.finish_run(
        run_id=run_id,
        status=status,
        account_success=ok_count,
        account_failed=fail_count,
        event_total=len(all_rows),
        ods_rows=ods_rows,
        dwd_rows=dwd_rows,
        error_summary="; ".join(error_messages)[:2000],
    )
    result = {
        "run_id": run_id,
        "biz_date": target_date,
        "status": status,
        "account_total": snapshot_total,
        "account_success": ok_count,
        "account_failed": fail_count,
        "event_total": len(all_rows),
        "ods_rows": ods_rows,
        "dwd_rows": dwd_rows,
    }
    if status == "success":
        send_alert(
            "BI 日同步成功",
            (
                f"run_id={run_id}\nbiz_date={target_date}\n"
                f"account_success={ok_count}\naccount_failed={fail_count}\n"
                f"ods_rows={ods_rows}\ndwd_rows={dwd_rows}"
            ),
            level="info",
        )
    else:
        send_alert(
            "BI 日同步异常",
            (
                f"run_id={run_id}\nbiz_date={target_date}\nstatus={status}\n"
                f"account_success={ok_count}\naccount_failed={fail_count}\n"
                f"errors={'; '.join(error_messages)[:1000]}"
            ),
            level="error",
        )
    return result


def retry_failed_accounts(*, run_id: str, biz_date: Optional[str] = None) -> dict[str, Any]:
    store = get_default_sync_log_store()
    run = store.get_run(run_id)
    if not run:
        raise ValueError(f"run_id 不存在: {run_id}")
    failed_emails = tuple(store.list_failed_accounts(run_id))
    if not failed_emails:
        return {"run_id": run_id, "status": "skipped", "message": "无失败账号"}
    target_date = biz_date or str(run.get("biz_date"))
    return run_daily_sync(
        biz_date=target_date,
        trigger_type="retry",
        emails=failed_emails,
    )

