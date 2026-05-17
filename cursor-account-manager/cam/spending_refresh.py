"""按量付费 / 消费页全量刷新（定时调度 + 静默实现）。"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta, timezone

from .alerting import send_alert
from .config import SETTINGS
from .models import Account
from .plan_scraper import (
    SpendingPanelInfo,
    _PLAN_BROWSER_SEM,
    _fetch_spending_panel_reuse_browser,
    _run_playwright_coroutine,
)
from .api_client import _split_session_token
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


def persist_spending_panel(store: TokenStore, email: str, info: SpendingPanelInfo) -> None:
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


async def _run_daily_spending_refresh_async(
    store: TokenStore,
    rows: list[dict],
) -> dict[str, int]:
    from patchright.async_api import async_playwright
    from .token_manager import get_default_manager

    mgr = get_default_manager()
    ok = 0
    failed = 0
    on_demand_open = 0
    on_demand_historical = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            for row in rows:
                email = _normalize_email(str(row.get("email") or ""))
                if not email:
                    continue
                acc = Account(
                    email=email,
                    imap_password=str(row.get("imap_password") or ""),
                    imap_host=str(row.get("imap_host") or SETTINGS.default_imap_host),
                    imap_port=int(row.get("imap_port") or SETTINGS.default_imap_port),
                    feishu_email=str(row.get("feishu_email") or "").strip().lower(),
                )
                try:
                    token = mgr.get_valid_token(acc)
                    cookie_val, _ = _split_session_token(token)
                    if not cookie_val:
                        raise RuntimeError("WorkosCursorSessionToken 为空")
                    info = await _fetch_spending_panel_reuse_browser(
                        browser, cookie_val, silent=True
                    )
                    persist_spending_panel(store, email, info)
                    if _on_demand_open_flag(info.on_demand_enabled):
                        on_demand_open += 1
                    elif bool(info.on_demand_historical):
                        on_demand_historical += 1
                    ok += 1
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    store.update_account_spending_snapshot(
                        email=email,
                        plan_name="",
                        on_demand_enabled=None,
                        on_demand_historical=None,
                        spending_error=err,
                    )
                    failed += 1
        finally:
            await browser.close()
    return {
        "ok": ok,
        "failed": failed,
        "total": ok + failed,
        "on_demand_open": on_demand_open,
        "on_demand_historical": on_demand_historical,
    }


def _run_spending_refresh_core() -> dict[str, int]:
    store = get_default_store()
    rows = store.list_accounts()
    with _PLAN_BROWSER_SEM:
        return _run_playwright_coroutine(  # type: ignore[return-value]
            _run_daily_spending_refresh_async(store, rows)
        )


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
