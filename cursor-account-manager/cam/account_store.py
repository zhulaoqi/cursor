"""读 accounts.csv。"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

from .config import SETTINGS
from .models import Account


def load_accounts(csv_path: Path | None = None) -> list[Account]:
    """从 CSV 读所有账号。缺失的 imap_host/imap_port 用 .env 默认值填充。"""
    path = csv_path or SETTINGS.accounts_csv
    if not path.exists():
        raise FileNotFoundError(
            f"账号 CSV 不存在: {path}（可从 data/accounts.csv.example 复制）"
        )

    accounts: list[Account] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"email", "imap_password"}
        missing = required - set((reader.fieldnames or []))
        if missing:
            raise ValueError(f"CSV 缺少必需列: {missing}")

        for i, row in enumerate(reader, start=2):
            email = (row.get("email") or "").strip()
            pwd = (row.get("imap_password") or "").strip()
            if not email or not pwd:
                continue
            host = (row.get("imap_host") or "").strip() or SETTINGS.default_imap_host
            port_raw = (row.get("imap_port") or "").strip()
            try:
                port = int(port_raw) if port_raw else SETTINGS.default_imap_port
            except ValueError:
                port = SETTINGS.default_imap_port

            accounts.append(Account(
                email=email,
                imap_password=pwd,
                imap_host=host,
                imap_port=port,
            ))

    return accounts


def filter_accounts(accounts: Iterable[Account], emails: Iterable[str]) -> list[Account]:
    """按邮箱列表过滤，保留顺序。"""
    wanted = {e.strip().lower() for e in emails if e and e.strip()}
    return [a for a in accounts if a.email.lower() in wanted]
