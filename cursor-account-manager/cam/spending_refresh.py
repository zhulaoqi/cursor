"""按量付费 / 消费页全量刷新（定时调度 + 静默实现）。"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .alerting import send_alert
from .config import SETTINGS
from .models import Account
from .plan_scraper import SpendingPanelBatchItem, fetch_spending_panels_batch
from .logger import get
from .sync_log_store import SyncLogStore, get_default_sync_log_store
from .token_store import TokenStore, get_default_store

log = get("spending")
BJ_TZ = timezone(timedelta(hours=8))


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _on_demand_open_flag(value: object) -> bool:
    if value is True or value == 1:
        return True
    if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes"):
        return True
    return False


def persist_spending_panel(store: TokenStore, email: str, info) -> None:
    """写入消费页解析结果；若同页解析出明确套餐状态则一并更新 plan_* 字段。"""
    now = int(time.time())
    store.update_account_spending_snapshot(
        email=email,
        plan_name=info.plan_name,
        on_demand_enabled=info.on_demand_enabled,
        on_demand_historical=bool(info.on_demand_historical),
        spending_error=info.error or "",
        checked_at=now,
    )
    if info.plan_snapshot is not None:
        store.update_account_plan(
            email=email,
            plan_status=info.plan_snapshot.status,
            plan_amount=info.plan_snapshot.amount,
            plan_error=info.plan_snapshot.error,
            checked_at=now,
        )


def _apply_batch_item(
    store: TokenStore,
    row: dict,
    item: SpendingPanelBatchItem,
) -> tuple[bool, bool, bool, str, str]:
    """持久化单条批量结果。返回 (ok, on_demand_open, on_demand_historical, email, error)。"""
    email = _normalize_email(item.email or str(row.get("email") or ""))
    if not email:
        return False, False, False, "", "empty_email"
    if item.error or item.info is None:
        err = item.error or "消费页解析失败"
        store.update_account_spending_snapshot(
            email=email,
            plan_name="",
            on_demand_enabled=None,
            on_demand_historical=None,
            spending_error=err,
        )
        return False, False, False, email, err

    info = item.info
    persist_spending_panel(store, email, info)
    on_open = _on_demand_open_flag(info.on_demand_enabled)
    on_hist = (not on_open) and bool(info.on_demand_historical)
    return True, on_open, on_hist, email, ""


def _summarize_errors(errors: list[str]) -> str:
    if not errors:
        return ""
    from collections import Counter

    counter = Counter(err.strip() for err in errors if str(err).strip())
    if not counter:
        return ""
    parts = [f"{msg} x{cnt}" for msg, cnt in counter.most_common(5)]
    return "；".join(parts)


def _run_spending_refresh_core(
    *,
    run_id: Optional[str] = None,
    log_store: Optional[SyncLogStore] = None,
) -> dict[str, Any]:
    store = get_default_store()
    rows = store.list_accounts()
    accounts: list[Account] = []
    row_by_email: dict[str, dict] = {}
    for row in rows:
        email = _normalize_email(str(row.get("email") or ""))
        if not email:
            continue
        row_by_email[email] = row
        accounts.append(
            Account(
                email=email,
                imap_password=str(row.get("imap_password") or ""),
                imap_host=str(row.get("imap_host") or SETTINGS.default_imap_host),
                imap_port=int(row.get("imap_port") or SETTINGS.default_imap_port),
                feishu_email=str(row.get("feishu_email") or "").strip().lower(),
            )
        )

    if run_id and log_store:
        log_store.add_stage(
            run_id=run_id,
            stage="fetch",
            status="start",
            message=f"spending_refresh accounts={len(accounts)}",
        )

    batch_items = fetch_spending_panels_batch(accounts, silent=True)

    ok = failed = on_demand_open = on_demand_historical = 0
    error_messages: list[str] = []
    for item in batch_items:
        started_at = int(time.time())
        row = row_by_email.get(_normalize_email(item.email), {})
        row_ok, on_open, on_hist, email, error_message = _apply_batch_item(store, row, item)
        ended_at = int(time.time())
        if row_ok:
            ok += 1
            if on_open:
                on_demand_open += 1
            elif on_hist:
                on_demand_historical += 1
        else:
            failed += 1
            if error_message:
                error_messages.append(error_message)

        if run_id and log_store and email:
            log_store.add_account_log(
                run_id=run_id,
                account_email=email,
                account_source=str(row.get("source") or "db"),
                is_new_account=False,
                status="success" if row_ok else "failed",
                started_at=started_at,
                ended_at=ended_at,
                fetch_rows=0,
                load_rows=0,
                error_message=error_message,
            )

    error_summary = _summarize_errors(error_messages)
    if run_id and log_store:
        log_store.add_stage(
            run_id=run_id,
            stage="fetch",
            status="success" if failed == 0 else "failed",
            message=(
                f"ok={ok} failed={failed} "
                f"on_demand_open={on_demand_open} on_demand_historical={on_demand_historical}"
                + (f" errors={error_summary}" if error_summary else "")
            )[:2000],
        )
        log_store.add_stage(
            run_id=run_id,
            stage="finalize",
            status="success",
            message=f"status={'success' if failed == 0 else ('partial_failed' if ok > 0 else 'failed')}",
        )

    return {
        "ok": ok,
        "failed": failed,
        "total": ok + failed,
        "on_demand_open": on_demand_open,
        "on_demand_historical": on_demand_historical,
        "error_summary": error_summary,
    }


def run_daily_spending_refresh_silent() -> dict[str, Any]:
    """遍历账号库刷新消费页（无飞书、仅写库）。供兼容调用。"""
    return _run_spending_refresh_core()


def _spending_refresh_alert_title(status: str) -> str:
    if status == "success":
        return "套餐/按量付费 调度刷新成功"
    if status == "partial_failed":
        return "套餐/按量付费 调度刷新部分失败"
    return "套餐/按量付费 调度刷新失败"


def run_daily_spending_refresh_scheduled(
    *,
    trigger_type: str = "spending_scheduler",
    trigger_date: str | None = None,
) -> dict[str, Any]:
    """定时调度：刷新全库套餐档位与按需开关，写库并可选发送飞书汇总。"""
    now = datetime.now(BJ_TZ)
    date_key = trigger_date or now.strftime("%Y-%m-%d")
    run_id = f"spending_{date_key.replace('-', '')}_{secrets.token_hex(4)}"
    log_store = get_default_sync_log_store()
    store = get_default_store()
    account_total = len(store.list_accounts())

    log_store.create_run(
        run_id=run_id,
        biz_date=date_key,
        trigger_type=trigger_type,
        account_total=account_total,
        account_snapshot_total=account_total,
        new_account_count=0,
    )
    log_store.add_stage(run_id=run_id, stage="init", status="start")
    log_store.add_stage(
        run_id=run_id,
        stage="init",
        status="success",
        message=f"account_total={account_total}",
    )

    try:
        result = _run_spending_refresh_core(run_id=run_id, log_store=log_store)
        ok = int(result.get("ok") or 0)
        failed = int(result.get("failed") or 0)
        on_open = int(result.get("on_demand_open") or 0)
        on_hist = int(result.get("on_demand_historical") or 0)
        err_summary = str(result.get("error_summary") or "")
        if failed == 0:
            status = "success"
        elif ok > 0:
            status = "partial_failed"
        else:
            status = "failed"

        log_store.finish_run(
            run_id=run_id,
            status=status,
            account_success=ok,
            account_failed=failed,
            event_total=0,
            ods_rows=0,
            error_summary="" if failed == 0 else (err_summary or f"failed={failed}"),
        )
        log.info(
            "消费页定时刷新完成 date=%s ok=%s failed=%s on_demand_open=%s on_demand_hist=%s",
            date_key,
            ok,
            failed,
            on_open,
            on_hist,
        )

        if SETTINGS.spending_refresh_alert_enable and SETTINGS.alert_bot_enable:
            level = "success" if status == "success" else "error"
            send_alert(
                _spending_refresh_alert_title(status),
                (
                    f"trigger_type={trigger_type}\n"
                    f"date={date_key}\n"
                    f"run_id={run_id}\n"
                    f"status={status}\n"
                    f"account_success={ok}\n"
                    f"account_failed={failed}\n"
                    f"on_demand_open={on_open}\n"
                    f"on_demand_historical={on_hist}"
                ),
                level=level,
            )
        return result
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        log.exception("消费页定时刷新失败: %s", err)
        log_store.finish_run(
            run_id=run_id,
            status="failed",
            account_success=0,
            account_failed=account_total,
            event_total=0,
            ods_rows=0,
            error_summary=err[:2000],
        )
        if SETTINGS.spending_refresh_alert_enable and SETTINGS.alert_bot_enable:
            send_alert(
                _spending_refresh_alert_title("failed"),
                (
                    f"trigger_type={trigger_type}\n"
                    f"date={date_key}\n"
                    f"run_id={run_id}\n"
                    f"status=failed\n"
                    f"error={err}"
                ),
                level="error",
            )
        raise
