"""全局配置：.env 读取 + 常量。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from math import isfinite
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


def _strict_env_value(key: str) -> str | None:
    """读取严格配置原始值，未设置或空字符串返回空值。"""
    value = os.environ.get(key)
    if value is None or not value.strip():
        return None
    return value.strip()


def _strict_env_int(key: str, default: int) -> int:
    """严格读取整数，显式非法值直接报错。"""
    value = _strict_env_value(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{key} 必须是整数") from exc


def _strict_env_float(key: str, default: float) -> float:
    """严格读取浮点数，显式非法值直接报错。"""
    value = _strict_env_value(key)
    if value is None:
        return default
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError(f"{key} 必须是浮点数") from exc
    if not isfinite(result):
        raise ValueError(f"{key} 必须是有限浮点数")
    return result


def _strict_env_decimal(key: str, default: Decimal) -> Decimal:
    """严格读取十进制数，显式非法值直接报错。"""
    value = _strict_env_value(key)
    if value is None:
        return default
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{key} 必须是十进制数") from exc
    if not result.is_finite():
        raise ValueError(f"{key} 必须是有限十进制数")
    return result


def _strict_env_bool(key: str, default: bool) -> bool:
    """严格读取布尔值，显式非法值直接报错。"""
    value = _strict_env_value(key)
    if value is None:
        return default
    normalized = value.lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{key} 必须是布尔值")


def _strict_env_str(key: str, default: str) -> str:
    """读取字符串，未设置或空字符串使用默认值。"""
    value = _strict_env_value(key)
    return default if value is None else value


@dataclass(frozen=True)
class Settings:
    default_imap_host: str
    default_imap_port: int
    proxy: str
    browser_login_concurrency: int
    invoice_download_concurrency: int
    invoice_active_context_limit: int
    api_concurrency: int
    web_fetch_max_workers: int
    capsolver_api_key: str
    twocaptcha_api_key: str
    accounts_csv: Path
    tokens_db: Path
    exports_dir: Path
    chrome_profiles_dir: Path
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
    bi_sync_db_pool_max_usage: int
    bi_sync_db_connect_retry_times: int
    bi_sync_db_connect_retry_backoff_sec: int
    bi_sync_db_execute_retry_times: int
    bi_sync_db_execute_retry_backoff_sec: int
    bi_sync_biz_tz: str
    bi_sync_cron: str
    bi_sync_lock_file: str
    spending_refresh_enable: bool
    spending_refresh_cron: str
    spending_refresh_lock_file: str
    spending_refresh_concurrency: int
    billing_ledger_retry_times: int
    billing_ledger_retry_backoff_sec: int
    billing_ledger_concurrency: int
    billing_ledger_refresh_enable: bool
    billing_ledger_refresh_cron: str
    billing_ledger_refresh_lock_file: str
    spending_refresh_alert_enable: bool
    alert_bot_client_id: str
    alert_bot_secret: str
    alert_bot_provider: str
    alert_bot_enable: bool
    alert_to_emails: str
    ledger_db_host: str
    ledger_db_port: int
    ledger_db_user: str
    ledger_db_password: str
    ledger_db_name: str
    ledger_db_pool_max_connections: int
    ledger_db_pool_min_cached: int
    ledger_db_pool_max_cached: int
    ledger_db_connect_timeout_sec: int
    ledger_db_read_timeout_sec: int
    ledger_db_write_timeout_sec: int
    ledger_db_connect_retry_times: int
    ledger_db_connect_retry_backoff_sec: int
    usage_snapshot_enable: bool
    usage_periodic_interval_hours: int
    usage_periodic_daily_at: str
    usage_snapshot_concurrency: int
    usage_bootstrap_stale_hours: int
    usage_periodic_retry_minutes: int
    usage_periodic_max_attempts_per_slot: int
    usage_periodic_alert_enable: bool
    usage_pre_reset_scan_interval_min: int
    usage_pre_reset_window_start_min: int
    usage_pre_reset_target_offset_min: int
    usage_pre_reset_window_end_min: int
    usage_low_threshold_pct: Decimal
    usage_cycle_continuity_tolerance_hours: int
    usage_periodic_lock_file: str
    usage_pre_reset_lock_file: str
    usage_account_lock_dir: Path
    usage_account_lock_timeout_sec: int
    usage_auth_breaker_min_samples: int
    usage_auth_breaker_failure_ratio: float
    usage_auth_breaker_cooldown_min: int
    usage_auth_breaker_window_size: int
    usage_auth_breaker_window_min: int
    usage_auth_quarantine_failures: int


def validate_settings(settings: Settings) -> None:
    """校验用量快照配置之间的约束。"""
    if not (
        settings.usage_pre_reset_window_start_min
        > settings.usage_pre_reset_target_offset_min
        > settings.usage_pre_reset_window_end_min
        >= 0
    ):
        raise ValueError(
            "必须满足 USAGE_PRE_RESET_WINDOW_START_MIN > "
            "USAGE_PRE_RESET_TARGET_OFFSET_MIN > "
            "USAGE_PRE_RESET_WINDOW_END_MIN >= 0"
        )
    if settings.usage_pre_reset_scan_interval_min <= 0:
        raise ValueError("USAGE_PRE_RESET_SCAN_INTERVAL_MIN 必须大于 0")
    if settings.usage_periodic_interval_hours <= 0:
        raise ValueError("USAGE_PERIODIC_INTERVAL_HOURS 必须大于 0")
    daily_at = (settings.usage_periodic_daily_at or "").strip()
    if not re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", daily_at):
        raise ValueError("USAGE_PERIODIC_DAILY_AT 必须是 HH:MM（00:00-23:59）")
    if settings.usage_periodic_retry_minutes <= 0:
        raise ValueError("USAGE_PERIODIC_RETRY_MINUTES 必须大于 0")
    if settings.usage_auth_breaker_cooldown_min <= 0:
        raise ValueError("USAGE_AUTH_BREAKER_COOLDOWN_MIN 必须大于 0")
    if settings.usage_auth_breaker_window_min <= 0:
        raise ValueError("USAGE_AUTH_BREAKER_WINDOW_MIN 必须大于 0")
    if (
        settings.usage_bootstrap_stale_hours
        < settings.usage_periodic_interval_hours
    ):
        raise ValueError(
            "USAGE_BOOTSTRAP_STALE_HOURS 不能小于 "
            "USAGE_PERIODIC_INTERVAL_HOURS"
        )
    if settings.usage_periodic_max_attempts_per_slot < 1:
        raise ValueError("USAGE_PERIODIC_MAX_ATTEMPTS_PER_SLOT 必须至少为 1")
    if settings.usage_snapshot_concurrency < 1:
        raise ValueError("USAGE_SNAPSHOT_CONCURRENCY 必须至少为 1")
    if settings.usage_auth_breaker_min_samples < 1:
        raise ValueError("USAGE_AUTH_BREAKER_MIN_SAMPLES 必须至少为 1")
    if settings.usage_auth_breaker_window_size < 1:
        raise ValueError("USAGE_AUTH_BREAKER_WINDOW_SIZE 必须至少为 1")
    if (
        settings.usage_auth_breaker_window_size
        < settings.usage_auth_breaker_min_samples
    ):
        raise ValueError(
            "USAGE_AUTH_BREAKER_WINDOW_SIZE 不能小于 "
            "USAGE_AUTH_BREAKER_MIN_SAMPLES"
        )
    if not Decimal("0") <= settings.usage_low_threshold_pct <= Decimal("100"):
        raise ValueError("USAGE_LOW_THRESHOLD_PCT 必须在 0 到 100 之间")
    if settings.usage_cycle_continuity_tolerance_hours < 0:
        raise ValueError(
            "USAGE_CYCLE_CONTINUITY_TOLERANCE_HOURS 不能小于 0"
        )
    if not 0 < settings.usage_auth_breaker_failure_ratio <= 1:
        raise ValueError(
            "USAGE_AUTH_BREAKER_FAILURE_RATIO 必须大于 0 且不大于 1"
        )
    if settings.usage_account_lock_timeout_sec < 0:
        raise ValueError("USAGE_ACCOUNT_LOCK_TIMEOUT_SEC 不能小于 0")
    if settings.usage_auth_quarantine_failures < 1:
        raise ValueError("USAGE_AUTH_QUARANTINE_FAILURES 必须至少为 1")


def validate_database_credentials(settings: Settings) -> None:
    """预检共享数据库凭据。

    由数据库任务启动入口调用，不在 load_settings/import 时调用。
    """
    if not (
        settings.billing_ledger_refresh_enable
        or settings.usage_snapshot_enable
    ):
        return
    if not settings.ledger_db_password.strip():
        raise ValueError(
            "启用 Ledger 或 Usage Snapshot 数据库任务时必须设置 "
            "LEDGER_DB_PASSWORD"
        )


# Windows 账号库和浏览器 profile 都固定在 D:\cam-data，不跟部署目录走
WINDOWS_DATA_DIR = Path(r"D:\cam-data")
WINDOWS_CHROME_PROFILES_DIR = WINDOWS_DATA_DIR / "chrome-profiles"

_WIN_ABS_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def default_chrome_profiles_dir() -> Path:
    if os.name == "nt":
        return WINDOWS_CHROME_PROFILES_DIR
    return Path.home() / ".cam" / "chrome-profiles"


def _chrome_profiles_dir() -> Path:
    if os.name == "nt":
        return WINDOWS_CHROME_PROFILES_DIR
    raw = (os.environ.get("CAM_CHROME_PROFILES_DIR") or "").strip()
    if not raw:
        return default_chrome_profiles_dir()
    if _WIN_ABS_PATH.match(raw):
        return Path(raw)
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def load_settings() -> Settings:
    def _path(key: str, default: str) -> Path:
        raw = os.environ.get(key, default)
        p = Path(raw)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p

    def _strict_path(key: str, default: str) -> Path:
        raw = _strict_env_str(key, default)
        p = Path(raw)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p

    settings = Settings(
        default_imap_host=os.environ.get("DEFAULT_IMAP_HOST", "imap.feishu.cn"),
        default_imap_port=_env_int("DEFAULT_IMAP_PORT", 993),
        proxy=os.environ.get("PROXY", "").strip(),
        browser_login_concurrency=_env_int("BROWSER_LOGIN_CONCURRENCY", 8),
        invoice_download_concurrency=_env_int("INVOICE_DOWNLOAD_CONCURRENCY", 10),
        invoice_active_context_limit=_env_int("INVOICE_ACTIVE_CONTEXT_LIMIT", 6),
        api_concurrency=_env_int("API_CONCURRENCY", 30),
        web_fetch_max_workers=_env_int("WEB_FETCH_MAX_WORKERS", 0),
        capsolver_api_key=os.environ.get("CAPSOLVER_API_KEY", "").strip(),
        twocaptcha_api_key=os.environ.get("TWOCAPTCHA_API_KEY", "").strip(),
        accounts_csv=(
            WINDOWS_DATA_DIR / "accounts.csv"
            if os.name == "nt"
            else _path("ACCOUNTS_CSV", "data/accounts.csv")
        ),
        tokens_db=(
            WINDOWS_DATA_DIR / "tokens.db"
            if os.name == "nt"
            else _path("TOKENS_DB", "data/tokens.db")
        ),
        exports_dir=_path("EXPORTS_DIR", "data/exports"),
        chrome_profiles_dir=_chrome_profiles_dir(),
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
        bi_sync_db_pool_ping=_env_int("BI_SYNC_DB_POOL_PING", 7),
        bi_sync_db_pool_max_usage=_env_int("BI_SYNC_DB_POOL_MAX_USAGE", 200),
        bi_sync_db_connect_retry_times=_env_int("BI_SYNC_DB_CONNECT_RETRY_TIMES", 3),
        bi_sync_db_connect_retry_backoff_sec=_env_int("BI_SYNC_DB_CONNECT_RETRY_BACKOFF_SEC", 2),
        bi_sync_db_execute_retry_times=_env_int("BI_SYNC_DB_EXECUTE_RETRY_TIMES", 3),
        bi_sync_db_execute_retry_backoff_sec=_env_int("BI_SYNC_DB_EXECUTE_RETRY_BACKOFF_SEC", 2),
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
            _env_int("SPENDING_REFRESH_CONCURRENCY", 4),
        ),
        billing_ledger_retry_times=_env_int("BILLING_LEDGER_RETRY_TIMES", 2),
        billing_ledger_retry_backoff_sec=_env_int("BILLING_LEDGER_RETRY_BACKOFF_SEC", 3),
        billing_ledger_concurrency=max(1, _env_int("BILLING_LEDGER_CONCURRENCY", 3)),
        billing_ledger_refresh_enable=_env_bool("BILLING_LEDGER_REFRESH_ENABLE", True),
        billing_ledger_refresh_cron=(
            os.environ.get("BILLING_LEDGER_REFRESH_CRON", "0 5 * * *").strip()
            or "0 5 * * *"
        ),
        billing_ledger_refresh_lock_file=(
            os.environ.get("BILLING_LEDGER_REFRESH_LOCK_FILE", "/tmp/cam_billing_ledger_refresh.lock").strip()
            or "/tmp/cam_billing_ledger_refresh.lock"
        ),
        spending_refresh_alert_enable=_env_bool("SPENDING_REFRESH_ALERT_ENABLE", True),
        alert_bot_client_id=os.environ.get("ALERT_BOT_CLIENT_ID", "").strip(),
        alert_bot_secret=os.environ.get("ALERT_BOT_SECRET", "").strip(),
        alert_bot_provider=os.environ.get("ALERT_BOT_PROVIDER", "feishu").strip() or "feishu",
        alert_bot_enable=_env_bool("ALERT_BOT_ENABLE", False),
        alert_to_emails=os.environ.get("ALERT_TO_EMAILS", "").strip(),
        ledger_db_host=os.environ.get("LEDGER_DB_HOST", "172.30.11.103").strip(),
        ledger_db_port=_env_int("LEDGER_DB_PORT", 3306),
        ledger_db_user=os.environ.get("LEDGER_DB_USER", "aicoding_admin").strip(),
        ledger_db_password=os.environ.get("LEDGER_DB_PASSWORD", "").strip(),
        ledger_db_name=os.environ.get("LEDGER_DB_NAME", "aicoding").strip(),
        ledger_db_pool_max_connections=_env_int("LEDGER_DB_POOL_MAX_CONNECTIONS", 8),
        ledger_db_pool_min_cached=_env_int("LEDGER_DB_POOL_MIN_CACHED", 1),
        ledger_db_pool_max_cached=_env_int("LEDGER_DB_POOL_MAX_CACHED", 4),
        ledger_db_connect_timeout_sec=_env_int("LEDGER_DB_CONNECT_TIMEOUT_SEC", 10),
        ledger_db_read_timeout_sec=_env_int("LEDGER_DB_READ_TIMEOUT_SEC", 30),
        ledger_db_write_timeout_sec=_env_int("LEDGER_DB_WRITE_TIMEOUT_SEC", 30),
        ledger_db_connect_retry_times=_env_int("LEDGER_DB_CONNECT_RETRY_TIMES", 3),
        ledger_db_connect_retry_backoff_sec=_env_int("LEDGER_DB_CONNECT_RETRY_BACKOFF_SEC", 2),
        usage_snapshot_enable=_strict_env_bool("USAGE_SNAPSHOT_ENABLE", True),
        usage_periodic_interval_hours=_strict_env_int("USAGE_PERIODIC_INTERVAL_HOURS", 24),
        usage_periodic_daily_at=_strict_env_str("USAGE_PERIODIC_DAILY_AT", "06:00"),
        usage_snapshot_concurrency=_strict_env_int("USAGE_SNAPSHOT_CONCURRENCY", 10),
        usage_bootstrap_stale_hours=_strict_env_int("USAGE_BOOTSTRAP_STALE_HOURS", 36),
        usage_periodic_retry_minutes=_strict_env_int("USAGE_PERIODIC_RETRY_MINUTES", 30),
        usage_periodic_max_attempts_per_slot=_strict_env_int(
            "USAGE_PERIODIC_MAX_ATTEMPTS_PER_SLOT",
            3,
        ),
        usage_periodic_alert_enable=_env_bool("USAGE_PERIODIC_ALERT_ENABLE", True),
        usage_pre_reset_scan_interval_min=_strict_env_int(
            "USAGE_PRE_RESET_SCAN_INTERVAL_MIN",
            15,
        ),
        usage_pre_reset_window_start_min=_strict_env_int(
            "USAGE_PRE_RESET_WINDOW_START_MIN",
            360,
        ),
        usage_pre_reset_target_offset_min=_strict_env_int(
            "USAGE_PRE_RESET_TARGET_OFFSET_MIN",
            180,
        ),
        usage_pre_reset_window_end_min=_strict_env_int(
            "USAGE_PRE_RESET_WINDOW_END_MIN",
            30,
        ),
        usage_low_threshold_pct=_strict_env_decimal(
            "USAGE_LOW_THRESHOLD_PCT",
            Decimal("30"),
        ),
        usage_cycle_continuity_tolerance_hours=_strict_env_int(
            "USAGE_CYCLE_CONTINUITY_TOLERANCE_HOURS",
            48,
        ),
        usage_periodic_lock_file=_strict_env_str(
            "USAGE_PERIODIC_LOCK_FILE",
            "data/cam_usage_periodic.lock",
        ),
        usage_pre_reset_lock_file=_strict_env_str(
            "USAGE_PRE_RESET_LOCK_FILE",
            "data/cam_usage_pre_reset.lock",
        ),
        usage_account_lock_dir=_strict_path(
            "USAGE_ACCOUNT_LOCK_DIR",
            "data/usage-account-locks",
        ),
        usage_account_lock_timeout_sec=_strict_env_int(
            "USAGE_ACCOUNT_LOCK_TIMEOUT_SEC",
            5,
        ),
        usage_auth_breaker_min_samples=_strict_env_int(
            "USAGE_AUTH_BREAKER_MIN_SAMPLES",
            20,
        ),
        usage_auth_breaker_failure_ratio=_strict_env_float(
            "USAGE_AUTH_BREAKER_FAILURE_RATIO",
            0.50,
        ),
        usage_auth_breaker_cooldown_min=_strict_env_int(
            "USAGE_AUTH_BREAKER_COOLDOWN_MIN",
            30,
        ),
        usage_auth_breaker_window_size=_strict_env_int(
            "USAGE_AUTH_BREAKER_WINDOW_SIZE",
            50,
        ),
        usage_auth_breaker_window_min=_strict_env_int(
            "USAGE_AUTH_BREAKER_WINDOW_MIN",
            10,
        ),
        usage_auth_quarantine_failures=_strict_env_int(
            "USAGE_AUTH_QUARANTINE_FAILURES",
            3,
        ),
    )
    validate_settings(settings)
    return settings


SETTINGS = load_settings()


def web_fetch_thread_workers(account_count: int) -> int:
    """Web 批量拉取 fetch 阶段线程数（纯 HTTP，无浏览器）。

    ``WEB_FETCH_MAX_WORKERS=0`` 表示仅按 ``API_CONCURRENCY`` 推算，不设硬顶 30。
    """
    n = max(1, int(account_count or 1))
    boosted = max(SETTINGS.api_concurrency * 2, 20)
    cap = int(SETTINGS.web_fetch_max_workers or 0)
    if cap > 0:
        boosted = min(boosted, cap)
    return min(n, max(SETTINGS.browser_login_concurrency, boosted))
