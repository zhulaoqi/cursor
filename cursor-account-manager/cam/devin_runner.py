"""Devin 批量采集：并发执行 + 落库 + 飞书卡片播报。

``run_devin_collection`` 是唯一入口：定时调度、网页手动、CLI 都走它。
同一时刻只允许一个批次（进程内锁 + 跨进程文件锁）。
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .alerting import send_alert
from .config import SETTINGS
from .devin_collector import CollectResult, collect_account
from .devin_config import devin_settings
from .devin_errors import error_label
from .devin_store import DevinStore, get_default_devin_store
from .logger import get
from .scheduler import _try_lock

log = get("devin_runner")

_run_lock = threading.Lock()
_progress_lock = threading.Lock()
_progress: dict[str, Any] = {
    "running": False,
    "run_id": "",
    "trigger": "",
    "total": 0,
    "done": 0,
    "ok": 0,
    "failed": 0,
    "current": [],
    "started_at": 0,
    "updated_at": 0,
}


def get_progress() -> dict[str, Any]:
    with _progress_lock:
        snap = dict(_progress)
        snap["current"] = list(_progress["current"])
        return snap


def is_running() -> bool:
    return bool(get_progress()["running"])


def _set_progress(**kwargs: Any) -> None:
    with _progress_lock:
        _progress.update(kwargs)
        _progress["updated_at"] = int(time.time())


@dataclass
class RunSummary:
    run_id: str = ""
    trigger: str = "manual"
    total: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    lock_busy: bool = False
    duration_sec: int = 0
    failure_breakdown: dict[str, int] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)
    plan_distribution: dict[str, int] = field(default_factory=dict)
    high_usage_accounts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.lock_busy:
            return "failed"
        if self.total == 0:
            return "success"
        if self.failed == 0:
            return "success"
        return "partial_failed" if self.success > 0 else "failed"


def _strip_artifact_hint(message: str) -> str:
    return message.split(" [现场:")[0].strip()


def collect_one(
    account: dict[str, Any],
    store: DevinStore,
    *,
    run_id: str = "",
    collector: Callable[[dict[str, Any]], CollectResult] | None = None,
    max_attempts: int | None = None,
) -> CollectResult:
    """采集一个账号（带重试）并落快照。"""
    collector = collector or collect_account
    attempts = max_attempts or devin_settings().max_attempts
    result: CollectResult | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = collector(account)
        except Exception as exc:  # noqa: BLE001 - collector 理论上不抛，兜底
            from .devin_errors import classify_exception

            code, message = classify_exception(exc)
            result = CollectResult(email=account["email"], ok=False, error_code=code, error_message=message)
        if result.ok:
            break
        # 账号层面的确定性失败不重试（重试只会重复发验证码/被限流）
        if result.error_code in ("imap_auth", "account_not_found"):
            break
        if attempt < attempts:
            log.info(f"[{account['email']}] 第 {attempt} 次失败({result.error_code})，稍后重试")
            time.sleep(5)
    assert result is not None
    store.add_snapshot(
        email=account["email"],
        ok=result.ok,
        run_id=run_id,
        error_code=result.error_code,
        error_message=result.error_message,
        duration_ms=result.duration_ms,
        login_used=result.login_used,
        data=result.data,
        source=result.source,
        raw=result.raw,
    )
    if result.ok:
        store.mark_success(account["email"], login_used=result.login_used)
    else:
        store.bump_failure(account["email"])
    return result


def build_alert(summary: RunSummary, *, zone: str | None = None) -> tuple[str, str, str]:
    """生成飞书卡片 (title, content, level)。content 用 key=value + 说明行，复用 alerting 的卡片渲染。"""
    tz = ZoneInfo(zone or getattr(SETTINGS, "bi_sync_biz_tz", "Asia/Shanghai") or "Asia/Shanghai")
    date_key = datetime.now(tz).strftime("%Y-%m-%d")
    status = summary.status
    title, level = {
        "success": ("Devin 每日采集完成", "success"),
        "partial_failed": ("Devin 每日采集部分失败", "warning"),
        "failed": ("Devin 每日采集失败", "error"),
    }[status]
    lines = [
        "platform=Devin",
        f"trigger_type={'devin_daily' if summary.trigger == 'scheduler' else 'devin_manual'}",
        f"date={date_key}",
        f"status={status}",
        f"run_id={summary.run_id}",
        f"account_total={summary.total}",
        f"account_success={summary.success}",
        f"account_failed={summary.failed}",
        f"account_skipped={summary.skipped}",
        f"duration={_fmt_duration(summary.duration_sec)}",
    ]
    if summary.lock_busy:
        lines.append("reason=已有 Devin 采集任务在执行（未拿到全局锁），本轮未采集")
    if summary.failure_breakdown:
        lines.append("**失败原因分布**")
        for code, count in sorted(summary.failure_breakdown.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {error_label(code)}：{count} 个")
    if summary.failures:
        lines.append("**失败账号（最多 15 个）**")
        for item in summary.failures[:15]:
            lines.append(f"- {item['email']}：{error_label(item['code'])}")
        if len(summary.failures) > 15:
            lines.append(f"- … 其余 {len(summary.failures) - 15} 个见看板")
    if summary.plan_distribution:
        dist = "、".join(f"{name or '未识别'}×{n}" for name, n in sorted(summary.plan_distribution.items(), key=lambda kv: -kv[1]))
        lines.append(f"**套餐分布：** {dist}")
    if summary.high_usage_accounts:
        lines.append(f"**周额度 ≥80% 账号：** {len(summary.high_usage_accounts)} 个")
        for item in summary.high_usage_accounts[:8]:
            lines.append(f"- {item['email']}：{item['weekly_used_pct']:g}%")
    return title, "\n".join(lines), level


def _fmt_duration(sec: int) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec} 秒"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m} 分 {s} 秒"
    h, m = divmod(m, 60)
    return f"{h} 小时 {m} 分"


def run_devin_collection(
    *,
    trigger: str = "manual",
    emails: list[str] | None = None,
    notify: bool | None = None,
    store: DevinStore | None = None,
    collector: Callable[[dict[str, Any]], CollectResult] | None = None,
    use_file_lock: bool = True,
) -> RunSummary:
    """执行一轮采集。notify=None 时：scheduler 触发默认通知，其余不通知。"""
    store = store or get_default_devin_store()
    cfg = devin_settings()
    do_notify = (trigger == "scheduler") if notify is None else notify
    do_notify = do_notify and cfg.alert_enable

    if not _run_lock.acquire(blocking=False):
        summary = RunSummary(trigger=trigger, lock_busy=True)
        log.warning("Devin 采集已在进程内运行，跳过")
        if do_notify:
            _notify(summary)
        return summary

    try:
        if use_file_lock:
            with _try_lock(cfg.lock_file) as locked:
                if not locked:
                    summary = RunSummary(trigger=trigger, lock_busy=True)
                    log.warning("Devin 采集文件锁被占用，跳过")
                    if do_notify:
                        _notify(summary)
                    return summary
                return _run_locked(store, cfg, trigger, emails, do_notify, collector)
        return _run_locked(store, cfg, trigger, emails, do_notify, collector)
    finally:
        _run_lock.release()


def _run_locked(store, cfg, trigger, emails, do_notify, collector) -> RunSummary:
    wanted = {e.strip().lower() for e in emails or [] if e and e.strip()}
    all_accounts = store.list_accounts()
    if wanted:
        picked = [a for a in all_accounts if a["email"].lower() in wanted]
        targets = [a for a in picked if a["status"] == "active"]
        skipped = len(picked) - len(targets)
    else:
        targets = [a for a in all_accounts if a["status"] == "active"]
        skipped = len(all_accounts) - len(targets)

    started = time.time()
    run_id = store.start_run(trigger, len(targets))
    summary = RunSummary(run_id=run_id, trigger=trigger, total=len(targets), skipped=skipped)
    _set_progress(
        running=True, run_id=run_id, trigger=trigger, total=len(targets),
        done=0, ok=0, failed=0, current=[], started_at=int(started),
    )
    log.info(f"Devin 采集开始 run_id={run_id} trigger={trigger} total={len(targets)} concurrency={cfg.concurrency}")

    failures: list[dict[str, Any]] = []
    breakdown: Counter[str] = Counter()
    plans: Counter[str] = Counter()
    high: list[dict[str, Any]] = []
    result_lock = threading.Lock()

    def work(account: dict[str, Any]) -> None:
        email = account["email"]
        with _progress_lock:
            _progress["current"].append(email)
        try:
            res = collect_one(account, store, run_id=run_id, collector=collector)
        finally:
            with _progress_lock:
                if email in _progress["current"]:
                    _progress["current"].remove(email)
        with result_lock:
            if res.ok:
                summary.success += 1
                plans[res.data.get("plan_name") or ""] += 1
                pct = res.data.get("weekly_used_pct")
                if pct is not None and pct >= 80:
                    high.append({"email": email, "weekly_used_pct": float(pct)})
            else:
                summary.failed += 1
                breakdown[res.error_code or "unknown"] += 1
                failures.append({
                    "email": email,
                    "code": res.error_code or "unknown",
                    "label": error_label(res.error_code),
                    "message": _strip_artifact_hint(res.error_message)[:300],
                })
            _set_progress(done=summary.success + summary.failed, ok=summary.success, failed=summary.failed)

    try:
        if targets:
            with ThreadPoolExecutor(max_workers=cfg.concurrency, thread_name_prefix="cam-devin") as pool:
                list(pool.map(work, targets))
    except Exception:
        log.exception("Devin 采集批次异常")
        raise
    finally:
        summary.duration_sec = int(time.time() - started)
        summary.failure_breakdown = dict(breakdown)
        summary.failures = failures
        summary.plan_distribution = dict(plans)
        summary.high_usage_accounts = sorted(high, key=lambda x: -x["weekly_used_pct"])
        store.finish_run(
            run_id,
            status=summary.status,
            success=summary.success,
            failed=summary.failed,
            skipped=summary.skipped,
            failure_breakdown=summary.failure_breakdown,
            failures=summary.failures,
        )
        _set_progress(running=False, current=[])
        log.info(
            f"Devin 采集结束 run_id={run_id} success={summary.success} failed={summary.failed} "
            f"skipped={summary.skipped} breakdown={summary.failure_breakdown}"
        )

    # 没有任何账号时不打扰（新环境/未导入账号）
    if do_notify and summary.total > 0:
        _notify(summary)
    return summary


def _notify(summary: RunSummary) -> None:
    try:
        title, content, level = build_alert(summary)
        send_alert(title, content, level=level)
    except Exception:
        log.exception("Devin 采集结果通知发送失败")
