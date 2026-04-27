"""数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Account:
    """来自 accounts.csv 的账号条目。"""
    email: str
    imap_password: str
    imap_host: str
    imap_port: int


@dataclass
class TokenRecord:
    """SQLite 中一条 token 记录。"""
    email: str
    access_token: str = ""
    refresh_token: str = ""
    expires_at: int = 0
    last_refreshed_at: int = 0
    last_login_at: int = 0
    consecutive_failures: int = 0
    status: str = "active"
    note: str = ""


@dataclass
class AccountSnapshot:
    """单账号一次完整数据采集结果。"""
    email: str
    fetched_at: int
    usage: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] = field(default_factory=dict)
    usage_limit: dict[str, Any] = field(default_factory=dict)
    usage_events: list[dict[str, Any]] = field(default_factory=list)
    # CSV 格式的使用明细原始文本（来自 export-usage-events-csv 端点，优先于 usage_events）
    usage_csv_text: str = ""
    stripe: dict[str, Any] = field(default_factory=dict)
    invoices: list[dict[str, Any]] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


class TokenAcquisitionError(Exception):
    """所有获取 token 路径都失败。"""


class TokenExpiredError(Exception):
    """API 返回 401，当前 access_token 已失效。"""


class RefreshTokenInvalidError(Exception):
    """refresh_token 失效（shouldLogout / 400 等）。"""


class BrowserLoginError(Exception):
    """浏览器登录失败（Turnstile / 邮件码超时 / PKCE 轮询失败）。"""
