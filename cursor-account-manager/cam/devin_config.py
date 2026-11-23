"""Devin 平台配置：全部从环境变量读取，不污染 Cursor 的 Settings。

所有键都以 ``DEVIN_`` 开头，缺省值可直接运行；.env.example 里有完整说明。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config import PROJECT_ROOT, SETTINGS

DEVIN_APP_BASE = "https://app.devin.ai"
DEVIN_USAGE_URL = f"{DEVIN_APP_BASE}/settings/usage"
DEVIN_PLANS_URL = f"{DEVIN_APP_BASE}/settings/plans"
DEVIN_INVOICES_URL = f"{DEVIN_APP_BASE}/settings/invoices"
DEVIN_API_HOST = "api.devin.ai"

_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _env_str(key: str, default: str) -> str:
    raw = os.environ.get(key)
    return default if raw is None or not raw.strip() else raw.strip()


def _env_int(key: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(key, default)))
    except (TypeError, ValueError):
        return max(minimum, default)


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class DevinSettings:
    schedule_enable: bool
    daily_at: str
    concurrency: int
    max_attempts: int
    headless: bool
    proxy: str
    lock_file: str
    alert_enable: bool
    sender_keywords: tuple[str, ...]
    code_timeout_sec: int
    artifacts_dir: Path
    export_dir: Path


def load_devin_settings() -> DevinSettings:
    daily_at = _env_str("DEVIN_DAILY_AT", "02:30")
    if not _HHMM_RE.fullmatch(daily_at):
        raise ValueError("DEVIN_DAILY_AT 必须是 HH:MM（00:00-23:59）")
    artifacts = Path(_env_str("DEVIN_ARTIFACTS_DIR", "data/artifacts/devin"))
    if not artifacts.is_absolute():
        artifacts = PROJECT_ROOT / artifacts
    keywords = tuple(
        x.strip().lower()
        for x in _env_str("DEVIN_CODE_SENDER_KEYWORDS", "devin,cognition,windsurf").split(",")
        if x.strip()
    )
    export_dir = Path(_env_str("DEVIN_EXPORT_DIR", "data/exports/devin"))
    if not export_dir.is_absolute():
        export_dir = PROJECT_ROOT / export_dir
    return DevinSettings(
        schedule_enable=_env_bool("DEVIN_SCHEDULE_ENABLE", True),
        daily_at=daily_at,
        concurrency=_env_int("DEVIN_CONCURRENCY", 3, minimum=1),
        max_attempts=_env_int("DEVIN_MAX_ATTEMPTS", 2, minimum=1),
        headless=_env_bool("DEVIN_HEADLESS", SETTINGS.headless),
        proxy=_env_str("DEVIN_PROXY", SETTINGS.proxy),
        lock_file=_env_str("DEVIN_LOCK_FILE", "data/cam_devin_daily.lock"),
        alert_enable=_env_bool("DEVIN_ALERT_ENABLE", True),
        sender_keywords=keywords or ("devin", "cognition"),
        code_timeout_sec=_env_int("DEVIN_CODE_TIMEOUT_SEC", 180, minimum=30),
        artifacts_dir=artifacts,
        export_dir=export_dir,
    )


def devin_settings() -> DevinSettings:
    """每次读取最新环境变量，便于测试里 monkeypatch。"""
    return load_devin_settings()
