"""BI 每日同步任务编排。"""

from __future__ import annotations

import concurrent.futures
import csv
import io
import json
import queue
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from . import fetcher
from .alerting import send_alert
from .config import SETTINGS
from .logger import get
from .models import Account
from .plan_scraper import PlanInfo, fetch_plan_info_from_dashboard
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


def _sync_alert_title(trigger_type: str, status: str) -> str:
    trigger = str(trigger_type or "").lower()
    state = str(status or "").lower()
    if trigger == "scheduler":
        return "BI 调度同步成功" if state == "success" else "BI 调度同步失败"
    return "BI 日同步成功" if state == "success" else "BI 日同步失败"


@dataclass(frozen=True)
class SnapshotAccount:
    account: Account
    source: str
    is_new: bool
    feishu_email: str = ""


@dataclass(frozen=True)
class AccountFetchResult:
    item: SnapshotAccount
    started_at: int
    ended_at: int
    rows: list[dict]
    error: str
    plan_amount: Optional[Decimal] = None
    plan_status: str = "unknown"
    skip_reason: str = ""


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


def _extract_plan_amount(value: Any) -> Optional[Decimal]:
    text = str(value or "").strip()
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None


def _extract_plan_text(plan: Any) -> str:
    if not plan:
        return ""
    if isinstance(plan, str):
        return plan
    if isinstance(plan, dict):
        current = _pick_value(
            plan,
            ("currentplan", "current_plan", "plan", "name", "planname", "subscription"),
        )
        if isinstance(current, dict):
            nested = _pick_value(current, ("name", "planname", "displayname"))
            return str(nested or "")
        if current is not None:
            return str(current)
    return str(plan)


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
    feishu_email: str,
    plan_amount: Optional[Decimal],
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
                "feishu_email": feishu_email,
                "plan_amount": plan_amount,
                "event_time": event_time.replace(tzinfo=None),
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
                "cost": _pick_value(row, ("cost", "cost_usd", "total_cost_usd")),
                "raw_event_json": json.dumps(row, ensure_ascii=False),
            }
        )
    return out


def _rows_from_usage_events(
    *,
    run_id: str,
    biz_date: str,
    email: str,
    feishu_email: str,
    plan_amount: Optional[Decimal],
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
                "feishu_email": feishu_email,
                "plan_amount": plan_amount,
                "event_time": event_time.replace(tzinfo=None),
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
                "cost": (
                    str(_to_float(token_usage.get("totalCents")) / 100)
                    if token_usage.get("totalCents") is not None
                    else None
                ),
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
            feishu_email=str(row.get("feishu_email") or "").strip().lower(),
        )
        added_at = int(row.get("added_at") or 0)
        snap.append(
            SnapshotAccount(
                account=acc,
                source=str(row.get("source") or "db"),
                is_new=added_at >= day_start_ts if added_at else False,
                feishu_email=acc.feishu_email,
            )
        )
    if snap:
        return snap
    return []


def _update_account_plan_status(*, email: str, info: PlanInfo) -> None:
    try:
        get_default_store().update_account_plan(
            email=email,
            plan_status=info.status,
            plan_amount=info.amount,
            plan_error=info.error,
        )
    except Exception as e:
        log.warning(
            _kv_message(
                event="account_plan_status_update_failed",
                account_email=email,
                plan_status=info.status,
                error=f"{type(e).__name__}: {e}",
            )
        )


def _fetch_account_usage_rows(
    item: SnapshotAccount,
    *,
    run_id: str,
    biz_date: str,
    start_ts: int,
    end_ts: int,
) -> AccountFetchResult:
    acc = item.account
    acc_start = int(time.time())
    feishu_email = str(item.feishu_email or getattr(acc, "feishu_email", "") or "").strip().lower()
    if not feishu_email:
        return AccountFetchResult(
            item=item,
            started_at=acc_start,
            ended_at=int(time.time()),
            rows=[],
            error=_err("E_ACCOUNT_METADATA", "feishu_email is required"),
        )
    retry_times = max(1, SETTINGS.bi_sync_retry_times)
    last_err = ""
    account_rows: list[dict] = []
    plan_info: Optional[PlanInfo] = None
    plan_amount: Optional[Decimal] = None
    plan_errors: list[str] = []
    for attempt in range(1, retry_times + 1):
        try:
            plan_info = fetch_plan_info_from_dashboard(acc)
            if plan_info.status == "not_enabled":
                _update_account_plan_status(email=acc.email, info=plan_info)
                return AccountFetchResult(
                    item=item,
                    started_at=acc_start,
                    ended_at=int(time.time()),
                    rows=[],
                    error="",
                    plan_amount=None,
                    plan_status="not_enabled",
                    skip_reason=plan_info.error or "plan not enabled",
                )
            if plan_info.status != "active" or plan_info.amount is None:
                raise RuntimeError(plan_info.error or f"套餐状态异常: {plan_info.status}")
            plan_amount = plan_info.amount
            _update_account_plan_status(email=acc.email, info=plan_info)
            last_err = ""
            break
        except Exception as e:
            err_detail = f"attempt={attempt}/{retry_times} {type(e).__name__}: {e}"
            plan_errors.append(err_detail)
            last_err = _err("E_PLAN_AMOUNT", " | ".join(plan_errors))
            log.warning(
                _kv_message(
                    event="plan_amount_fetch_failed",
                    run_id=run_id,
                    account_email=acc.email,
                    attempt=attempt,
                    retry_times=retry_times,
                    error=f"{type(e).__name__}: {e}",
                )
            )
    if plan_amount is None:
        _update_account_plan_status(
            email=acc.email,
            info=PlanInfo(status="error", amount=None, error=last_err),
        )
        return AccountFetchResult(
            item=item,
            started_at=acc_start,
            ended_at=int(time.time()),
            rows=[],
            error=last_err,
            plan_amount=None,
            plan_status="error",
        )
    for _ in range(retry_times):
        try:
            snap = fetcher.fetch_one(
                acc,
                what=("usage_events",),
                start_ts=start_ts,
                end_ts=end_ts,
            )
            fatal_errors = dict(snap.errors)
            if fatal_errors:
                last_err = _err(
                    "E_FETCH_ACCOUNT",
                    "; ".join(f"{k}={v}" for k, v in fatal_errors.items()),
                )
                continue
            if snap.usage_csv_text:
                account_rows = _rows_from_usage_csv(
                    run_id=run_id,
                    biz_date=biz_date,
                    email=acc.email,
                    feishu_email=feishu_email,
                    plan_amount=plan_amount,
                    csv_text=snap.usage_csv_text,
                )
            else:
                account_rows = _rows_from_usage_events(
                    run_id=run_id,
                    biz_date=biz_date,
                    email=acc.email,
                    feishu_email=feishu_email,
                    plan_amount=plan_amount,
                    events=snap.usage_events or [],
                )
            last_err = ""
            break
        except Exception as e:
            last_err = _err("E_FETCH_EXCEPTION", f"{type(e).__name__}: {e}")
    return AccountFetchResult(
        item=item,
        started_at=acc_start,
        ended_at=int(time.time()),
        rows=account_rows,
        error=last_err,
        plan_amount=plan_amount,
        plan_status=plan_info.status if plan_info else "unknown",
    )


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
            error_summary=msg,
        )
        result = {"run_id": run_id, "biz_date": target_date, "status": "failed", "message": msg}
        send_alert(
            _sync_alert_title(trigger_type, "failed"),
            f"trigger_type={trigger_type}\nrun_id={run_id}\nbiz_date={target_date}\nreason={msg}",
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
    fetch_ok_count = 0
    fetch_fail_count = 0
    load_fail_count = 0
    load_ok_count = 0
    ods_rows = 0
    log_store.add_stage(run_id=run_id, stage="prepare_partition", status="start")
    try:
        loader = StarRocksLoader()
        loader.check_connection()
        loader.ensure_tables()
        loader.ensure_biz_date_partitions_ready(biz_date=target_date)
        log_store.add_stage(
            run_id=run_id,
            stage="prepare_partition",
            status="success",
            message=_kv_message(biz_date=target_date, tables="ods", starrocks_connection="ok"),
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
            error_summary=msg[:2000],
        )
        send_alert(
            _sync_alert_title(trigger_type, "failed"),
            f"trigger_type={trigger_type}\nrun_id={run_id}\nbiz_date={target_date}\nstage=prepare_partition\nerror={msg}",
            level="error",
        )
        raise

    fetch_workers = min(snapshot_total, max(1, int(SETTINGS.api_concurrency or 1)))
    log_store.add_stage(
        run_id=run_id,
        stage="fetch",
        status="start",
        message=_kv_message(
            fetch_workers=fetch_workers,
            load_queue="single_writer",
            writer_workers=1,
        ),
    )

    def _handle_fetch_result(fetch_result: AccountFetchResult) -> None:
        nonlocal ok_count, fail_count, fetch_ok_count, fetch_fail_count, load_fail_count
        nonlocal load_ok_count, ods_rows
        item = fetch_result.item
        acc = item.account
        account_rows = fetch_result.rows
        last_err = fetch_result.error
        if fetch_result.plan_status == "not_enabled":
            fetch_ok_count += 1
            ok_count += 1
            msg = _kv_message(
                event="account_plan_not_enabled",
                run_id=run_id,
                account_email=acc.email,
                plan_status=fetch_result.plan_status,
                reason=fetch_result.skip_reason,
            )
            log.info(msg)
            log_store.add_account_log(
                run_id=run_id,
                account_email=acc.email,
                account_source=item.source,
                is_new_account=item.is_new,
                status="skipped",
                started_at=fetch_result.started_at,
                ended_at=fetch_result.ended_at,
                fetch_rows=0,
                load_rows=0,
                error_message=_kv_message(
                    plan_status=fetch_result.plan_status,
                    reason=fetch_result.skip_reason,
                ),
            )
            return
        if account_rows or last_err == "":
            fetch_ok_count += 1
            # 按账号+日期覆盖写入 ODS；保留原始明细，避免额外派生表写入成本。
            load_start = int(time.time())
            log.info(
                _kv_message(
                    event="account_load_start",
                    run_id=run_id,
                    account_email=acc.email,
                    fetch_rows=len(account_rows),
                    queue="single_writer",
                )
            )
            try:
                normalized = [loader.normalize_decimal_fields(r) for r in account_rows]
                loaded_ods = loader.replace_ods_rows_for_account(
                    biz_date=target_date,
                    account_email=acc.email,
                    rows=normalized,
                )
                ods_rows += loaded_ods
                load_ok_count += 1
                ok_count += 1
                all_rows.extend(account_rows)
                load_end = int(time.time())
                log.info(
                    _kv_message(
                        event="account_load_success",
                        run_id=run_id,
                        account_email=acc.email,
                        ods_rows=loaded_ods,
                        duration_sec=max(0, load_end - load_start),
                    )
                )
                log_store.add_account_log(
                    run_id=run_id,
                    account_email=acc.email,
                    account_source=item.source,
                    is_new_account=item.is_new,
                    status="success",
                    started_at=fetch_result.started_at,
                    ended_at=int(time.time()),
                    fetch_rows=len(account_rows),
                    load_rows=loaded_ods,
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
                    started_at=fetch_result.started_at,
                    ended_at=int(time.time()),
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
                started_at=fetch_result.started_at,
                ended_at=fetch_result.ended_at,
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

    result_queue: queue.Queue[
        tuple[SnapshotAccount, Optional[AccountFetchResult], Optional[BaseException]]
    ] = queue.Queue(maxsize=max(1, fetch_workers * 2))

    def _enqueue_fetch_result(
        future: concurrent.futures.Future[AccountFetchResult],
        item: SnapshotAccount,
    ) -> None:
        try:
            result_queue.put((item, future.result(), None))
        except BaseException as e:
            result_queue.put((item, None, e))

    with concurrent.futures.ThreadPoolExecutor(max_workers=fetch_workers) as pool:
        for item in snapshot:
            future = pool.submit(
                _fetch_account_usage_rows,
                item,
                run_id=run_id,
                biz_date=target_date,
                start_ts=start_ts,
                end_ts=end_ts,
            )
            future.add_done_callback(
                lambda done_future, item=item: _enqueue_fetch_result(done_future, item)
            )

        for _ in range(snapshot_total):
            item, fetch_result, fetch_exception = result_queue.get()
            try:
                if fetch_exception is not None:
                    raise fetch_exception
                if fetch_result is None:
                    raise RuntimeError("fetch worker returned empty result")
                _handle_fetch_result(fetch_result)
            except Exception as e:
                fail_count += 1
                fetch_fail_count += 1
                err = _err("E_FETCH_WORKER", f"{type(e).__name__}: {e}")
                error_messages.append(err)
                now = int(time.time())
                log_store.add_account_log(
                    run_id=run_id,
                    account_email=item.account.email,
                    account_source=item.source,
                    is_new_account=item.is_new,
                    status="failed",
                    started_at=now,
                    ended_at=now,
                    fetch_rows=0,
                    load_rows=0,
                    error_message=err,
                )
                log.warning(
                    _kv_message(
                        event="account_fetch_worker_failed",
                        run_id=run_id,
                        account_email=item.account.email,
                        error=err,
                    )
                )
            finally:
                result_queue.task_done()

    log_store.add_stage(
        run_id=run_id,
        stage="fetch",
        status="success" if fetch_fail_count == 0 else "failed",
        message=_kv_message(
            snapshot_total=snapshot_total,
            fetch_workers=fetch_workers,
            load_queue="single_writer",
            fetch_ok=fetch_ok_count,
            fetch_fail=fetch_fail_count,
            fetched_rows=len(all_rows),
        ),
    )
    log_store.add_stage(
        run_id=run_id,
        stage="load_ods",
        status="success" if load_fail_count == 0 else "failed",
        message=_kv_message(
            load_mode="serial_queue",
            load_ok=load_ok_count,
            load_fail=load_fail_count,
            ods_rows=ods_rows,
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
        ),
    )
    log_store.finish_run(
        run_id=run_id,
        status=status,
        account_success=ok_count,
        account_failed=fail_count,
        event_total=len(all_rows),
        ods_rows=ods_rows,
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
    }
    if status == "success":
        send_alert(
            _sync_alert_title(trigger_type, status),
            (
                f"trigger_type={trigger_type}\nrun_id={run_id}\nbiz_date={target_date}\n"
                f"account_success={ok_count}\naccount_failed={fail_count}\n"
                f"ods_rows={ods_rows}"
            ),
            level="success",
        )
    else:
        send_alert(
            _sync_alert_title(trigger_type, status),
            (
                f"trigger_type={trigger_type}\nrun_id={run_id}\nbiz_date={target_date}\nstatus={status}\n"
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

