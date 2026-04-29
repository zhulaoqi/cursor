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
    api_concurrency: int
    capsolver_api_key: str
    twocaptcha_api_key: str
    accounts_csv: Path
    tokens_db: Path
    exports_dir: Path
    headless: bool
    verification_code_timeout: int


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
        invoice_download_concurrency=_env_int("INVOICE_DOWNLOAD_CONCURRENCY", 8),
        api_concurrency=_env_int("API_CONCURRENCY", 30),
        capsolver_api_key=os.environ.get("CAPSOLVER_API_KEY", "").strip(),
        twocaptcha_api_key=os.environ.get("TWOCAPTCHA_API_KEY", "").strip(),
        accounts_csv=_path("ACCOUNTS_CSV", "data/accounts.csv"),
        tokens_db=_path("TOKENS_DB", "data/tokens.db"),
        exports_dir=_path("EXPORTS_DIR", "data/exports"),
        headless=_env_bool("HEADLESS", True),
        verification_code_timeout=_env_int("VERIFICATION_CODE_TIMEOUT", 120),
    )


SETTINGS = load_settings()
