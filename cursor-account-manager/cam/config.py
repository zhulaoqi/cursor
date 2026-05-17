"""全局配置：.env 读取 + 常量。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)


_load_env()


# Cursor 固定常量
CURSOR_CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"
CURSOR_API2_BASE = "https://api2.cursor.sh"
CURSOR_WEB_BASE = "https://cursor.com"
# cursor.com/login 会自动 302 到 authenticator.cursor.sh/?client_id=...&redirect_uri=...&state=...
# 这是目前（2026）有效的登录入口；authenticator.cursor.sh/sign-in 已 404
CURSOR_SIGN_IN_URL = "https://cursor.com/login"
CURSOR_LOGIN_DEEP_CONTROL = "https://cursor.com/cn/loginDeepControl"
CURSOR_OAUTH_TOKEN_URL = f"{CURSOR_API2_BASE}/oauth/token"
CURSOR_AUTH_POLL_URL = f"{CURSOR_API2_BASE}/auth/poll"
CURSOR_VERIFICATION_SENDER = "no-reply@cursor.com"

EXPIRY_SAFETY_MARGIN_SEC = 5 * 60  # token 提前 5 分钟认定失效
MAX_CONSECUTIVE_FAILURES = 5       # 达到即 disable


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    default_imap_host: str
    default_imap_port: int
    proxy: str
    browser_login_concurrency: int
    invoice_download_concurrency: int
    invoice_active_context_limit: int
    api_concurrency: int
    capsolver_api_key: str
    twocaptcha_api_key: str
    accounts_csv: Path
    tokens_db: Path
    exports_dir: Path
    headless: bool
    verification_code_timeout: int
    bi_sync_enable: bool
    bi_sync_db_url: str
    bi_sync_db_username: str
    bi_sync_db_password: str
    bi_sync_batch_size: int
    bi_sync_retry_times: int
    bi_sync_account_timeout_sec: int
    bi_sync_db_connect_timeout_sec: int
    bi_sync_db_read_timeout_sec: int
    bi_sync_db_write_timeout_sec: int
    bi_sync_db_query_timeout_sec: int
    bi_sync_db_pool_min_cached: int
    bi_sync_db_pool_max_cached: int
    bi_sync_db_pool_max_connections: int
    bi_sync_db_pool_blocking: bool
    bi_sync_db_pool_ping: int
    bi_sync_db_connect_retry_times: int
    bi_sync_db_connect_retry_backoff_sec: int
    bi_sync_biz_tz: str
    bi_sync_cron: str
    bi_sync_lock_file: str
    spending_refresh_enable: bool
    spending_refresh_cron: str
    spending_refresh_lock_file: str
    spending_refresh_concurrency: int
    spending_refresh_alert_enable: bool
    alert_bot_client_id: str
    alert_bot_secret: str
    alert_bot_provider: str
    alert_bot_enable: bool
    alert_to_emails: str


def load_settings() -> Settings:
    def _path(key: str, default: str) -> Path:
        raw = os.environ.get(key, default)
        p = Path(raw)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p

    return Settings(
        default_imap_host=os.environ.get("DEFAULT_IMAP_HOST", "imap.feishu.cn"),
        default_imap_port=_env_int("DEFAULT_IMAP_PORT", 993),
        proxy=os.environ.get("PROXY", "").strip(),
        browser_login_concurrency=_env_int("BROWSER_LOGIN_CONCURRENCY", 5),
        invoice_download_concurrency=_env_int("INVOICE_DOWNLOAD_CONCURRENCY", 4),
        invoice_active_context_limit=_env_int("INVOICE_ACTIVE_CONTEXT_LIMIT", 3),
        api_concurrency=_env_int("API_CONCURRENCY", 30),
        capsolver_api_key=os.environ.get("CAPSOLVER_API_KEY", "").strip(),
        twocaptcha_api_key=os.environ.get("TWOCAPTCHA_API_KEY", "").strip(),
        accounts_csv=_path("ACCOUNTS_CSV", "data/accounts.csv"),
        tokens_db=_path("TOKENS_DB", "data/tokens.db"),
        exports_dir=_path("EXPORTS_DIR", "data/exports"),
        headless=_env_bool("HEADLESS", True),
        verification_code_timeout=_env_int("VERIFICATION_CODE_TIMEOUT", 120),
        bi_sync_enable=_env_bool("BI_SYNC_ENABLE", False),
        bi_sync_db_url=os.environ.get("BI_SYNC_DB_URL", "").strip(),
        bi_sync_db_username=os.environ.get("BI_SYNC_DB_USERNAME", "").strip(),
        bi_sync_db_password=os.environ.get("BI_SYNC_DB_PASSWORD", "").strip(),
        bi_sync_batch_size=_env_int("BI_SYNC_BATCH_SIZE", 5000),
        bi_sync_retry_times=_env_int("BI_SYNC_RETRY_TIMES", 3),
        bi_sync_account_timeout_sec=_env_int("BI_SYNC_ACCOUNT_TIMEOUT_SEC", 600),
        bi_sync_db_connect_timeout_sec=_env_int("BI_SYNC_DB_CONNECT_TIMEOUT_SEC", 10),
        bi_sync_db_read_timeout_sec=_env_int("BI_SYNC_DB_READ_TIMEOUT_SEC", 120),
        bi_sync_db_write_timeout_sec=_env_int("BI_SYNC_DB_WRITE_TIMEOUT_SEC", 120),
        bi_sync_db_query_timeout_sec=_env_int("BI_SYNC_DB_QUERY_TIMEOUT_SEC", 120),
        bi_sync_db_pool_min_cached=_env_int("BI_SYNC_DB_POOL_MIN_CACHED", 1),
        bi_sync_db_pool_max_cached=_env_int("BI_SYNC_DB_POOL_MAX_CACHED", 4),
        bi_sync_db_pool_max_connections=_env_int("BI_SYNC_DB_POOL_MAX_CONNECTIONS", 8),
        bi_sync_db_pool_blocking=_env_bool("BI_SYNC_DB_POOL_BLOCKING", True),
        bi_sync_db_pool_ping=_env_int("BI_SYNC_DB_POOL_PING", 1),
        bi_sync_db_connect_retry_times=_env_int("BI_SYNC_DB_CONNECT_RETRY_TIMES", 3),
        bi_sync_db_connect_retry_backoff_sec=_env_int("BI_SYNC_DB_CONNECT_RETRY_BACKOFF_SEC", 2),
        bi_sync_biz_tz=os.environ.get("BI_SYNC_BIZ_TZ", "Asia/Shanghai").strip() or "Asia/Shanghai",
        bi_sync_cron=os.environ.get("BI_SYNC_CRON", "30 1 * * *").strip() or "30 1 * * *",
        bi_sync_lock_file=os.environ.get("BI_SYNC_LOCK_FILE", "/tmp/cam_bi_sync.lock").strip() or "/tmp/cam_bi_sync.lock",
        spending_refresh_enable=_env_bool("SPENDING_REFRESH_ENABLE", True),
        spending_refresh_cron=os.environ.get("SPENDING_REFRESH_CRON", "0 3 * * *").strip() or "0 3 * * *",
        spending_refresh_lock_file=(
            os.environ.get("SPENDING_REFRESH_LOCK_FILE", "/tmp/cam_spending_refresh.lock").strip()
            or "/tmp/cam_spending_refresh.lock"
        ),
        spending_refresh_concurrency=max(
            1,
            _env_int(
                "SPENDING_REFRESH_CONCURRENCY",
                _env_int("INVOICE_ACTIVE_CONTEXT_LIMIT", 3),
            ),
        ),
        spending_refresh_alert_enable=_env_bool("SPENDING_REFRESH_ALERT_ENABLE", True),
        alert_bot_client_id=os.environ.get("ALERT_BOT_CLIENT_ID", "").strip(),
        alert_bot_secret=os.environ.get("ALERT_BOT_SECRET", "").strip(),
        alert_bot_provider=os.environ.get("ALERT_BOT_PROVIDER", "feishu").strip() or "feishu",
        alert_bot_enable=_env_bool("ALERT_BOT_ENABLE", False),
        alert_to_emails=os.environ.get("ALERT_TO_EMAILS", "").strip(),
    )


SETTINGS = load_settings()
