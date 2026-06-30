"""账期净支出定时刷新：抓取当月 Billing Invoices 并写入数据库。"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .config import SETTINGS
from .logger import get
from .models import Account
from .sync_log_store import SyncLogStore, get_default_sync_log_store
from .token_store import TokenStore, get_default_store

log = get("billing_ledger_refresh")
BJ_TZ = timezone(timedelta(hours=8))
BILLING_LEDGER_TRIGGER_TYPE = "billing_ledger_scheduler"


def scrape_billing_ledger_batch(*args, **kwargs):
    from .billing_ledger import scrape_billing_ledger_batch as _impl

    return _impl(*args, **kwargs)


def get_ledger_store():
    from .billing_ledger_store import get_ledger_store as _impl

    return _impl()


def get_default_manager():
    from .token_manager import get_default_manager as _impl

    return _impl()


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _date_key(trigger_date: str | None = None) -> str:
    return trigger_date or datetime.now(BJ_TZ).strftime("%Y-%m-%d")


def _month_key_from_date(date_key: str) -> str:
    return (date_key or datetime.now(BJ_TZ).strftime("%Y-%m-%d"))[:7]


def _accounts_from_store(store: TokenStore) -> tuple[list[Account], dict[str, dict]]:
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
    return accounts, row_by_email


def _summarize_errors(errors: list[str]) -> str:
    if not errors:
        return ""
    from collections import Counter

    counter = Counter(err.strip() for err in errors if str(err).strip())
    return "；".join(f"{msg} x{cnt}" for msg, cnt in counter.most_common(5))


def run_daily_billing_ledger_refresh_scheduled(
    *,
    trigger_type: str = BILLING_LEDGER_TRIGGER_TYPE,
    trigger_date: str | None = None,
    log_store: Optional[SyncLogStore] = None,
) -> dict[str, Any]:
    """定时调度：刷新当天所属月份的账期净支出，写库并记录最近运行。"""
    date_key = _date_key(trigger_date)
    billing_month = _month_key_from_date(date_key)
    run_id = f"ledger_{date_key.replace('-', '')}_{secrets.token_hex(4)}"
    log_store = log_store or get_default_sync_log_store()
    token_store = get_default_store()
    accounts, row_by_email = _accounts_from_store(token_store)

    log_store.create_run(
        run_id=run_id,
        biz_date=date_key,
        trigger_type=trigger_type,
        account_total=len(accounts),
        account_snapshot_total=len(accounts),
        new_account_count=0,
    )
    log_store.add_stage(run_id=run_id, stage="init", status="start")
    log_store.add_stage(
        run_id=run_id,
        stage="init",
        status="success",
        message=f"billing_month={billing_month} account_total={len(accounts)}",
    )

    account_started_at: dict[str, int] = {}
    account_status: dict[str, tuple[str, str]] = {}

    def _progress_cb(email: str, phase: str, msg: str = "") -> None:
        norm_email = _normalize_email(email)
        if not norm_email:
            return
        account_started_at.setdefault(norm_email, int(time.time()))
        if phase in ("done", "warn_done"):
            account_status[norm_email] = ("success", msg or "")
        elif phase == "error":
            account_status[norm_email] = ("failed", msg or "失败")

    try:
        log_store.add_stage(
            run_id=run_id,
            stage="fetch",
            status="start",
            message=f"billing_month={billing_month}",
        )
        summaries, _detail_rows = scrape_billing_ledger_batch(
            accounts,
            billing_month,
            manager=get_default_manager(),
            progress_cb=_progress_cb,
        )
        summary_by_email = {_normalize_email(getattr(s, "email", "")): s for s in summaries}

        errors: list[str] = []
        success = failed = 0
        for acc in accounts:
            email = _normalize_email(acc.email)
            if email in summary_by_email:
                status, error_message = "success", ""
                success += 1
            else:
                status, error_message = account_status.get(email, ("failed", "未返回账期净支出结果"))
                if status == "success":
                    success += 1
                else:
                    failed += 1
                    errors.append(error_message)

            row = row_by_email.get(email, {})
            started_at = account_started_at.get(email, int(time.time()))
            summary = summary_by_email.get(email)
            log_store.add_account_log(
                run_id=run_id,
                account_email=email,
                account_source=str(row.get("source") or "db"),
                is_new_account=False,
                status=status,
                started_at=started_at,
                ended_at=int(time.time()),
                fetch_rows=int(getattr(summary, "row_count", 0) or 0),
                load_rows=1 if status == "success" else 0,
                error_message=error_message,
            )

        log_store.add_stage(
            run_id=run_id,
            stage="fetch",
            status="success" if failed == 0 else "failed",
            message=f"billing_month={billing_month} success={success} failed={failed}",
        )

        log_store.add_stage(
            run_id=run_id,
            stage="db_write",
            status="start",
            message=f"summaries={len(summaries)}",
        )
        ledger_store = get_ledger_store()
        ledger_store.ensure_tables()
        written = ledger_store.upsert_summaries(summaries) if summaries else 0
        log_store.add_stage(
            run_id=run_id,
            stage="db_write",
            status="success",
            message=f"written={written}",
        )

        if failed == 0:
            status = "success"
        elif success > 0:
            status = "partial_failed"
        else:
            status = "failed"
        error_summary = _summarize_errors(errors)
        log_store.add_stage(
            run_id=run_id,
            stage="finalize",
            status="success",
            message=f"status={status}",
        )
        log_store.finish_run(
            run_id=run_id,
            status=status,
            account_success=success,
            account_failed=failed,
            event_total=0,
            ods_rows=written,
            error_summary="" if failed == 0 else (error_summary or f"failed={failed}"),
        )
        log.info(
            "账期净支出定时刷新完成 date=%s month=%s success=%s failed=%s written=%s",
            date_key,
            billing_month,
            success,
            failed,
            written,
        )
        return {
            "run_id": run_id,
            "biz_date": date_key,
            "billing_month": billing_month,
            "trigger_type": trigger_type,
            "status": status,
            "account_total": len(accounts),
            "account_success": success,
            "account_failed": failed,
            "written": written,
            "error_summary": error_summary,
        }
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        log.exception("账期净支出定时刷新失败")
        log_store.add_stage(run_id=run_id, stage="finalize", status="failed", message=err)
        log_store.finish_run(
            run_id=run_id,
            status="failed",
            account_success=0,
            account_failed=len(accounts),
            event_total=0,
            ods_rows=0,
            error_summary=err,
        )
        raise
