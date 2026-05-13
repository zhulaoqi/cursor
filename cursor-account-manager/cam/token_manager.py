"""Token 编排：SQLite 缓存 → refresh_token 刷新 → 浏览器登录兜底。"""

from __future__ import annotations

import base64
import json
import threading
import time
from typing import Optional
from urllib.parse import unquote

import requests

from . import browser_login
from .config import (
    CURSOR_CLIENT_ID,
    CURSOR_OAUTH_TOKEN_URL,
    EXPIRY_SAFETY_MARGIN_SEC,
    MAX_CONSECUTIVE_FAILURES,
    SETTINGS,
)
from .logger import get
from .models import (
    Account,
    BrowserLoginError,
    RefreshTokenInvalidError,
    TokenAcquisitionError,
    TokenRecord,
)
from .token_store import TokenStore, get_default_store

log = get("token")


_email_locks: dict[str, threading.Lock] = {}
_email_locks_guard = threading.Lock()


def _lock_for(email: str) -> threading.Lock:
    with _email_locks_guard:
        lk = _email_locks.get(email)
        if lk is None:
            lk = threading.Lock()
            _email_locks[email] = lk
        return lk


def _decode_jwt_exp(token: str) -> int:
    """解析 JWT payload 取 exp（秒）；失败返回 0。

    兼容 WorkosCursorSessionToken 形式的 token（user_id%3A%3A<jwt> / user_id::<jwt>）：
    会先 URL-decode，再剥掉 `::` 前面的 user_id，再按 JWT 解析。
    """
    try:
        t = unquote(token or "")
        if "::" in t:
            t = t.split("::", 1)[1]
        parts = t.split(".")
        if len(parts) < 2:
            return 0
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        data = base64.urlsafe_b64decode(payload + padding)
        obj = json.loads(data)
        return int(obj.get("exp") or 0)
    except Exception:
        return 0


def _is_token_valid(rec: TokenRecord) -> bool:
    if not rec.access_token:
        return False
    # expires_at 未填时尝试从 JWT 解析
    exp = rec.expires_at or _decode_jwt_exp(rec.access_token)
    if exp <= 0:
        return False
    return (exp - EXPIRY_SAFETY_MARGIN_SEC) > int(time.time())


def _refresh_via_api(refresh_token: str, proxy: str = "") -> tuple[str, str, int]:
    """调 oauth/token 换新 access_token。返回 (access, refresh, expires_at)。"""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    resp = requests.post(
        CURSOR_OAUTH_TOKEN_URL,
        json={
            "grant_type": "refresh_token",
            "client_id": CURSOR_CLIENT_ID,
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/json"},
        timeout=20,
        proxies=proxies,
    )
    try:
        data = resp.json()
    except Exception:
        raise RefreshTokenInvalidError(
            f"refresh 非 JSON 响应: status={resp.status_code}, body={resp.text[:200]}"
        )

    if resp.status_code != 200:
        if data.get("shouldLogout") is True:
            raise RefreshTokenInvalidError(f"shouldLogout=true: {data}")
        raise RefreshTokenInvalidError(
            f"refresh 失败 status={resp.status_code}: {data}"
        )

    if data.get("shouldLogout") is True:
        raise RefreshTokenInvalidError(f"200 但 shouldLogout=true: {data}")

    access = data.get("accessToken") or data.get("access_token") or ""
    new_refresh = data.get("refreshToken") or data.get("refresh_token") or refresh_token
    if not access:
        raise RefreshTokenInvalidError(f"refresh 响应缺 accessToken: {data}")

    expires_at = _decode_jwt_exp(access)
    if expires_at == 0:
        expires_at = int(time.time()) + 3600
    return access, new_refresh, expires_at


class TokenManager:
    def __init__(self, store: Optional[TokenStore] = None):
        self.store = store or get_default_store()

    def get_valid_token(self, account: Account, *, force_refresh: bool = False) -> str:
        """
        返回当前有效的 access_token。策略：
          1. 缓存命中且未过期 → 直接返回
          2. 过期/缺失 → 用 refresh_token 刷新
          3. 刷新失败或无 refresh_token → 浏览器重登
          4. 浏览器登录失败 → 失败计数 +1，达上限则 disable
        """
        lock = _lock_for(account.email)
        with lock:
            rec = self.store.get(account.email) or TokenRecord(email=account.email)

            if rec.status == "disabled":
                last_err = self.store.get_latest_error_detail(account.email)
                suffix = f"；最近错误：{last_err}" if last_err else ""
                raise TokenAcquisitionError(
                    f"账号 {account.email} 已被标记 disabled（连续失败 {rec.consecutive_failures} 次）{suffix}"
                )

            if not force_refresh and _is_token_valid(rec):
                return rec.access_token

            if rec.refresh_token:
                try:
                    access, refresh, exp = _refresh_via_api(
                        rec.refresh_token, proxy=SETTINGS.proxy,
                    )
                    self.store.update_tokens(
                        account.email, access, refresh, exp, from_refresh=True,
                    )
                    self.store.log(account.email, "refresh_ok", f"exp={exp}")
                    log.info(f"[{account.email}] refresh_token 刷新成功")
                    return access
                except RefreshTokenInvalidError as e:
                    log.warning(f"[{account.email}] refresh_token 失效: {e}")
                    self.store.log(account.email, "refresh_fail", str(e))
                    self.store.invalidate_refresh_token(account.email)
                except Exception as e:
                    log.warning(f"[{account.email}] refresh 异常（非失效）: {e}")
                    self.store.log(account.email, "refresh_error", str(e))

            return self._browser_login_and_save(account)

    # 这些关键词出现在异常信息里，视为临时网络抖动，重试不计入失败次数
    _TRANSIENT_ERRORS = (
        "ERR_TUNNEL_CONNECTION_FAILED",
        "ERR_CONNECTION_RESET",
        "ERR_CONNECTION_REFUSED",
        "ERR_CONNECTION_TIMED_OUT",
        "ERR_NETWORK_CHANGED",
        "ERR_INTERNET_DISCONNECTED",
        "net::ERR_",
        "Timeout",
        "timeout",
    )
    _NETWORK_RETRY = 3        # 网络抖动最多重试次数
    _NETWORK_RETRY_DELAY = 5  # 每次重试前等待秒数

    def _is_transient(self, e: Exception) -> bool:
        msg = str(e)
        return any(k in msg for k in self._TRANSIENT_ERRORS)

    def _browser_login_and_save(self, account: Account, *, force_fresh: bool = False) -> str:
        log.info(f"[{account.email}] 启动浏览器登录兜底")

        for attempt in range(1, self._NETWORK_RETRY + 1):
            try:
                access, refresh = browser_login.login(
                    account.email, account.imap_password,
                    imap_host=account.imap_host, imap_port=account.imap_port,
                    force_fresh=force_fresh,
                )
                break  # 成功跳出重试循环
            except BrowserLoginError as e:
                # 真正的登录业务失败（验证码错误/账号异常等），计入失败次数，不重试
                n = self.store.bump_failure(account.email, MAX_CONSECUTIVE_FAILURES)
                self.store.log(account.email, "browser_login_fail", str(e))
                log.error(f"[{account.email}] 浏览器登录失败（第 {n} 次）: {e}")
                raise TokenAcquisitionError(f"{account.email} 浏览器登录失败: {e}") from e
            except Exception as e:
                if self._is_transient(e) and attempt < self._NETWORK_RETRY:
                    # 临时网络错误，等待后重试，不计入失败次数
                    log.warning(
                        f"[{account.email}] 网络抖动（{attempt}/{self._NETWORK_RETRY}），"
                        f"{self._NETWORK_RETRY_DELAY}s 后重试: {e}"
                    )
                    time.sleep(self._NETWORK_RETRY_DELAY * attempt)
                    continue
                # 非网络错误，或已达重试上限，才计入失败
                n = self.store.bump_failure(account.email, MAX_CONSECUTIVE_FAILURES)
                self.store.log(account.email, "browser_login_error", str(e))
                log.exception(f"[{account.email}] 浏览器登录异常（第 {n} 次）")
                raise TokenAcquisitionError(f"{account.email} 浏览器登录异常: {e}") from e

        exp = _decode_jwt_exp(access)
        if exp == 0:
            exp = int(time.time()) + 3600
        self.store.update_tokens(
            account.email, access, refresh, exp,
            from_refresh=True, from_login=True,
        )
        self.store.log(account.email, "browser_login_ok", f"exp={exp}")
        return access

    def force_relogin(self, account: Account) -> str:
        """不走缓存 / 不走 refresh，直接浏览器登录。"""
        with _lock_for(account.email):
            return self._browser_login_and_save(account, force_fresh=True)

    def mark_access_token_expired(self, email: str) -> None:
        """API 返回 401 时调用，让下次 get_valid_token 触发 refresh。"""
        self.store.invalidate_access_token(email)
        self.store.log(email, "api_401", "access_token invalidated")


_default_manager: Optional[TokenManager] = None
_default_manager_lock = threading.Lock()


def get_default_manager() -> TokenManager:
    global _default_manager
    if _default_manager is None:
        with _default_manager_lock:
            if _default_manager is None:   # double-checked locking
                _default_manager = TokenManager()
    return _default_manager
