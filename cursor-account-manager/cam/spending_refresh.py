"""按量付费 / 消费页全量刷新（定时调度 + 静默实现）。"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta, timezone

from .alerting import send_alert
from .config import SETTINGS
from .models import Account
from .plan_scraper import SpendingPanelBatchItem, fetch_spending_panels_batch
from .logger import get
from .sync_log_store import get_default_sync_log_store
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
) -> tuple[bool, bool, bool]:
    """持久化单条批量结果。返回 (ok, on_demand_open, on_demand_historical)。"""
    email = _normalize_email(item.email or str(row.get("email") or ""))
    if not email:
        return False, False, False
    if item.error or item.info is None:
        err = item.error or "消费页解析失败"
        store.update_account_spending_snapshot(
            email=email,
            plan_name="",
            on_demand_enabled=None,
            on_demand_historical=None,
            spending_error=err,
        )
        return False, False, False

    info = item.info
    persist_spending_panel(store, email, info)
    on_open = _on_demand_open_flag(info.on_demand_enabled)
    on_hist = (not on_open) and bool(info.on_demand_historical)
    return True, on_open, on_hist


def _run_spending_refresh_core() -> dict[str, int]:
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

    batch_items = fetch_spending_panels_batch(accounts, silent=True)

    ok = failed = on_demand_open = on_demand_historical = 0
    for item in batch_items:
        row = row_by_email.get(_normalize_email(item.email), {})
        row_ok, on_open, on_hist = _apply_batch_item(store, row, item)
        if row_ok:
            ok += 1
            if on_open:
                on_demand_open += 1
            elif on_hist:
                on_demand_historical += 1
        else:
            failed += 1

    return {
        "ok": ok,
        "failed": failed,
        "total": ok + failed,
        "on_demand_open": on_demand_open,
        "on_demand_historical": on_demand_historical,
    }


def run_daily_spending_refresh_silent() -> dict[str, int]:
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
) -> dict[str, int]:
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

    try:
        result = _run_spending_refresh_core()
        ok = int(result.get("ok") or 0)
        failed = int(result.get("failed") or 0)
        on_open = int(result.get("on_demand_open") or 0)
        on_hist = int(result.get("on_demand_historical") or 0)
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
            error_summary="" if failed == 0 else f"failed={failed}",
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
