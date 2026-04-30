"""批量拉取 → AccountSnapshot。"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from typing import Iterable, Optional

from .api_client import CursorClient
from .config import SETTINGS
from .logger import get
from .models import Account, AccountSnapshot, TokenAcquisitionError, TokenExpiredError
from .token_manager import TokenManager, get_default_manager

log = get("fetcher")


DEFAULT_WHAT = ("usage", "plan", "usage_limit", "usage_events", "stripe")


def fetch_one(
    account: Account,
    *,
    manager: Optional[TokenManager] = None,
    what: Iterable[str] = DEFAULT_WHAT,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> AccountSnapshot:
    """拉单个账号的所有数据，单项失败不影响整体。"""
    mgr = manager or get_default_manager()
    snap = AccountSnapshot(email=account.email, fetched_at=int(time.time()))
    what_set = set(what)

    try:
        token = mgr.get_valid_token(account)
    except TokenAcquisitionError as e:
        snap.errors["token"] = str(e)
        return snap

    client = CursorClient(token)
    auth_relogin_attempts = 0
    max_auth_relogin_attempts = 3

    def _refresh_client_after_401(reason: str) -> bool:
        """API 明确返回未登录时，强制重登并重建 client。"""
        nonlocal client, auth_relogin_attempts
        if auth_relogin_attempts >= max_auth_relogin_attempts:
            return False
        auth_relogin_attempts += 1
        log.warning(
            f"[{account.email}] {reason} 返回 401/403，判定当前登录态不可用，"
            f"准备强制重登（{auth_relogin_attempts}/{max_auth_relogin_attempts}）"
        )
        mgr.mark_access_token_expired(account.email)
        try:
            client.close()
        except Exception:
            pass
        new_token = mgr.force_relogin(account)
        client = CursorClient(new_token)
        log.info(
            f"[{account.email}] {reason} 已强制重登完成，继续重试当前接口"
        )
        return True

    def _call(name: str, fn, *, max_transient_attempts: int = 3):
        """统一调用包装：
        - TokenExpiredError（401/403）走强制重登流程
        - 其他异常（代理断连/超时/5xx 等瞬时错误）自动重试 max_transient_attempts 次
          采用指数退避（2s / 4s / ...），最后一次仍失败则记录错误。
        """
        last_auth_error: TokenExpiredError | None = None
        transient_attempt = 0
        while True:
            try:
                return fn()
            except TokenExpiredError as e:
                last_auth_error = e
                try:
                    if _refresh_client_after_401(name):
                        continue
                except Exception as ee:
                    snap.errors[name] = f"重登后仍失败: {ee}"
                    return None
                snap.errors[name] = f"401/403，重登 {max_auth_relogin_attempts} 次后仍失败: {last_auth_error}"
                return None
            except Exception as e:
                transient_attempt += 1
                if transient_attempt >= max_transient_attempts:
                    snap.errors[name] = str(e)
                    return None
                backoff = 2 ** transient_attempt
                log.warning(
                    f"[{account.email}] {name} 瞬时错误，{backoff}s 后重试"
                    f"（{transient_attempt}/{max_transient_attempts - 1}）：{e}"
                )
                time.sleep(backoff)
                continue

    try:
        if "usage" in what_set:
            _last_usage_err: Exception | None = None
            _attempt = 1
            while _attempt <= 3:  # 非认证类错误最多重试 3 次
                try:
                    snap.usage = client.get_current_period_usage() or {}
                    _last_usage_err = None
                    break
                except TokenExpiredError as e:
                    try:
                        if _refresh_client_after_401("usage"):
                            continue
                    except Exception as ee:
                        snap.errors["usage"] = f"重登后仍失败: {ee}"
                        _last_usage_err = None
                        break
                    snap.errors["usage"] = f"401/403，重登 {max_auth_relogin_attempts} 次后仍失败: {e}"
                    _last_usage_err = None
                    break
                except Exception as e:
                    _last_usage_err = e
                    if _attempt < 3:
                        time.sleep(2 ** _attempt)  # 2s / 4s 指数退避
                    _attempt += 1
            if _last_usage_err is not None:
                snap.errors["usage"] = str(_last_usage_err)

        if "plan" in what_set:
            snap.plan = _call("plan", lambda: client.get_plan_info() or {}) or {}

        if "usage_limit" in what_set:
            snap.usage_limit = _call("usage_limit", lambda: client.get_usage_limit_status() or {}) or {}

        if "usage_events" in what_set:
            # ── 优先：CSV 端点，精度最高（含完整 token 用量） ──
            csv_ok = False
            try:
                csv_text = client.export_usage_events_csv(
                    start_ts=start_ts, end_ts=end_ts
                )
                # 校验：非空且是真正的 CSV（首行应有逗号，不是 HTML 错误页）
                if csv_text and "," in csv_text[:500] and not csv_text.lstrip().startswith("<"):
                    snap.usage_csv_text = csv_text
                    lines = csv_text.strip().splitlines()
                    log.info(
                        f"[{account.email}] 使用明细 CSV: {max(0, len(lines)-1)} 行"
                        f"（含表头 {lines[0][:80] if lines else ''}）"
                    )
                    csv_ok = True
                else:
                    log.warning(
                        f"[{account.email}] CSV 端点返回非 CSV 内容，降级 API: "
                        f"{csv_text[:100]!r}"
                    )
            except Exception as csv_err:
                log.info(f"[{account.email}] CSV 端点失败({csv_err})，尝试 API 端点")

            # ── 降级：分页 API 端点 ──
            if not csv_ok:
                evs_result = _call(
                    "usage_events",
                    lambda: client.iter_all_usage_events(
                        page_size=100,
                        start_ts=start_ts,
                        end_ts=end_ts,
                    ),
                )
                if evs_result is not None:
                    snap.usage_events = evs_result
                    evs = snap.usage_events or []
                    if evs:
                        ts_vals = [int(e.get("timestamp") or 0) for e in evs if e.get("timestamp")]
                        if ts_vals:
                            import datetime as _dt
                            fmt = lambda ms: _dt.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d")
                            log.info(
                                f"[{account.email}] 使用明细(API): {len(evs)} 条, "
                                f"范围 {fmt(min(ts_vals))} ~ {fmt(max(ts_vals))}"
                            )
                        else:
                            log.info(f"[{account.email}] 使用明细(API): {len(evs)} 条")
                    else:
                        log.info(f"[{account.email}] 使用明细: 0 条（CSV 和 API 均无数据）")

        if "stripe" in what_set:
            snap.stripe = _call("stripe", lambda: client.get_stripe_info() or {}) or {}
    finally:
        client.close()

    return snap


def fetch_many(
    accounts: list[Account],
    *,
    manager: Optional[TokenManager] = None,
    what: Iterable[str] = DEFAULT_WHAT,
    concurrency: Optional[int] = None,
) -> list[AccountSnapshot]:
    """并发拉取，返回与 accounts 同序的 snapshots。"""
    mgr = manager or get_default_manager()
    workers = concurrency or SETTINGS.api_concurrency

    results: list[Optional[AccountSnapshot]] = [None] * len(accounts)
    log_lock = threading.Lock()

    def _task(idx: int, acc: Account) -> None:
        snap = fetch_one(acc, manager=mgr, what=what)
        with log_lock:
            if snap.errors:
                log.warning(
                    f"[{acc.email}] 完成（{len(snap.errors)} 项失败: "
                    f"{','.join(snap.errors.keys())}）"
                )
            else:
                log.info(f"[{acc.email}] 完成")
        results[idx] = snap

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_task, i, a) for i, a in enumerate(accounts)]
        for f in concurrent.futures.as_completed(futures):
            try:
                f.result()
            except Exception as e:
                log.exception(f"任务异常: {e}")

    return [r for r in results if r is not None]
