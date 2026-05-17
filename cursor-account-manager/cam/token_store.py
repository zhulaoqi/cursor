"""SQLite 持久化：tokens + audit_log。"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import SETTINGS
from .models import TokenRecord


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    email                TEXT PRIMARY KEY,
    access_token         TEXT,
    refresh_token        TEXT,
    expires_at           INTEGER DEFAULT 0,
    last_refreshed_at    INTEGER DEFAULT 0,
    last_login_at        INTEGER DEFAULT 0,
    consecutive_failures INTEGER DEFAULT 0,
    status               TEXT DEFAULT 'active',
    note                 TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    email  TEXT,
    ts     INTEGER,
    action TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    email         TEXT PRIMARY KEY,
    imap_password TEXT NOT NULL DEFAULT '',
    imap_host     TEXT NOT NULL DEFAULT 'imap.feishu.cn',
    imap_port     INTEGER NOT NULL DEFAULT 993,
    feishu_email  TEXT NOT NULL DEFAULT '',
    plan_amount   TEXT DEFAULT '',
    plan_status   TEXT NOT NULL DEFAULT 'unknown',
    plan_checked_at INTEGER DEFAULT 0,
    plan_error    TEXT DEFAULT '',
    plan_name     TEXT DEFAULT '',
    on_demand_enabled INTEGER,
    on_demand_historical INTEGER,
    spending_checked_at INTEGER DEFAULT 0,
    spending_error TEXT DEFAULT '',
    added_at      INTEGER DEFAULT 0,
    updated_at    INTEGER DEFAULT 0,
    source        TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_email_ts ON audit_log(email, ts);
"""


class TokenStore:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or SETTINGS.tokens_db
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            self._ensure_account_columns(conn)

    def _ensure_account_columns(self, conn: sqlite3.Connection) -> None:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        if "feishu_email" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN feishu_email TEXT NOT NULL DEFAULT ''")
        if "plan_amount" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN plan_amount TEXT DEFAULT ''")
        if "plan_status" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN plan_status TEXT NOT NULL DEFAULT 'unknown'")
        if "plan_checked_at" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN plan_checked_at INTEGER DEFAULT 0")
        if "plan_error" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN plan_error TEXT DEFAULT ''")
        if "plan_name" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN plan_name TEXT DEFAULT ''")
        if "on_demand_enabled" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN on_demand_enabled INTEGER")
        if "on_demand_historical" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN on_demand_historical INTEGER")
        if "spending_checked_at" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN spending_checked_at INTEGER DEFAULT 0")
        if "spending_error" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN spending_error TEXT DEFAULT ''")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            with self._lock:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> TokenRecord:
        return TokenRecord(
            email=row["email"],
            access_token=row["access_token"] or "",
            refresh_token=row["refresh_token"] or "",
            expires_at=row["expires_at"] or 0,
            last_refreshed_at=row["last_refreshed_at"] or 0,
            last_login_at=row["last_login_at"] or 0,
            consecutive_failures=row["consecutive_failures"] or 0,
            status=row["status"] or "active",
            note=row["note"] or "",
        )

    def get(self, email: str) -> TokenRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM tokens WHERE email = ?", (email,),
            ).fetchone()
            return self._row_to_record(row) if row else None

    def list_all(self) -> list[TokenRecord]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM tokens ORDER BY email").fetchall()
            return [self._row_to_record(r) for r in rows]

    def upsert(self, record: TokenRecord) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO tokens (
                    email, access_token, refresh_token, expires_at,
                    last_refreshed_at, last_login_at,
                    consecutive_failures, status, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    access_token         = excluded.access_token,
                    refresh_token        = excluded.refresh_token,
                    expires_at           = excluded.expires_at,
                    last_refreshed_at    = excluded.last_refreshed_at,
                    last_login_at        = excluded.last_login_at,
                    consecutive_failures = excluded.consecutive_failures,
                    status               = excluded.status,
                    note                 = excluded.note
                """,
                (
                    record.email, record.access_token, record.refresh_token,
                    record.expires_at, record.last_refreshed_at, record.last_login_at,
                    record.consecutive_failures, record.status, record.note,
                ),
            )

    def update_tokens(
        self, email: str,
        access_token: str, refresh_token: str, expires_at: int,
        *, from_refresh: bool = False, from_login: bool = False,
    ) -> None:
        now = int(time.time())
        rec = self.get(email) or TokenRecord(email=email)
        rec.access_token = access_token
        rec.refresh_token = refresh_token
        rec.expires_at = expires_at
        if from_refresh:
            rec.last_refreshed_at = now
        if from_login:
            rec.last_login_at = now
        rec.consecutive_failures = 0
        rec.status = "active"
        self.upsert(rec)

    def invalidate_access_token(self, email: str) -> None:
        rec = self.get(email)
        if rec is None:
            return
        rec.access_token = ""
        rec.expires_at = 0
        self.upsert(rec)

    def invalidate_refresh_token(self, email: str) -> None:
        rec = self.get(email)
        if rec is None:
            return
        rec.access_token = ""
        rec.refresh_token = ""
        rec.expires_at = 0
        self.upsert(rec)

    def bump_failure(self, email: str, max_failures: int) -> int:
        rec = self.get(email) or TokenRecord(email=email)
        rec.consecutive_failures += 1
        if rec.consecutive_failures >= max_failures:
            rec.status = "disabled"
        self.upsert(rec)
        return rec.consecutive_failures

    def reset(self, email: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM tokens WHERE email = ?", (email,))

    def log(self, email: str, action: str, detail: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (email, ts, action, detail) VALUES (?, ?, ?, ?)",
                (email, int(time.time()), action, detail[:2000]),
            )

    def get_latest_error_detail(self, email: str) -> str:
        """返回账号最近一次失败原因，优先登录/刷新错误日志。"""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT detail
                FROM audit_log
                WHERE email = ?
                  AND action IN ('browser_login_fail', 'browser_login_error', 'refresh_fail', 'refresh_error')
                  AND COALESCE(detail, '') <> ''
                ORDER BY ts DESC, id DESC
                LIMIT 1
                """,
                (email,),
            ).fetchone()
            return (row["detail"] or "").strip() if row else ""

    # ─── Accounts CRUD ───────────────────────────────────────────────

    def list_accounts(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY email").fetchall()
            return [dict(r) for r in rows]

    def search_accounts(self, keyword: str = "", limit: int = 500) -> list[dict]:
        keyword = (keyword or "").strip().lower()
        limit = max(1, min(int(limit or 500), 1000))
        with self._conn() as conn:
            if keyword:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM accounts
                    WHERE lower(email) LIKE ?
                    ORDER BY email
                    LIMIT ?
                    """,
                    (f"%{keyword}%", limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM accounts ORDER BY email LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_account(self, email: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE email = ?", (email,)
            ).fetchone()
            return dict(row) if row else None

    def upsert_account(
        self,
        email: str,
        imap_password: str,
        imap_host: str,
        imap_port: int,
        feishu_email: str = "",
        source: str = "",
    ) -> None:
        now = int(time.time())
        existing = self.get_account(email)
        added_at = existing["added_at"] if existing else now
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO accounts (email, imap_password, imap_host, imap_port, feishu_email,
                                      added_at, updated_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    imap_password = excluded.imap_password,
                    imap_host     = excluded.imap_host,
                    imap_port     = excluded.imap_port,
                    feishu_email  = excluded.feishu_email,
                    updated_at    = excluded.updated_at,
                    source        = excluded.source
                """,
                (
                    email,
                    imap_password,
                    imap_host,
                    imap_port,
                    feishu_email,
                    added_at,
                    now,
                    source,
                ),
            )

    def update_account_plan(
        self,
        *,
        email: str,
        plan_status: str,
        plan_amount: object = "",
        plan_error: str = "",
        checked_at: int | None = None,
    ) -> None:
        now = int(time.time()) if checked_at is None else int(checked_at)
        amount = "" if plan_amount in (None, "") else str(plan_amount)
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET plan_amount = ?,
                    plan_status = ?,
                    plan_checked_at = ?,
                    plan_error = ?,
                    updated_at = ?
                WHERE email = ?
                """,
                (
                    amount,
                    plan_status or "unknown",
                    now,
                    (plan_error or "")[:2000],
                    now,
                    email,
                ),
            )

    def update_account_spending_snapshot(
        self,
        *,
        email: str,
        plan_name: str = "",
        on_demand_enabled: bool | int | None = None,
        on_demand_historical: bool | int | None = None,
        spending_error: str = "",
        checked_at: int | None = None,
    ) -> None:
        """更新消费页解析结果：套餐档位名、按需开关、曾按需消费标记。未知字段传 None 写入 NULL。"""
        now = int(time.time()) if checked_at is None else int(checked_at)
        name = (plan_name or "").strip()[:128]
        err = (spending_error or "")[:2000]
        if on_demand_enabled is True or on_demand_enabled == 1:
            od_sql: int | None = 1
        elif on_demand_enabled is False or on_demand_enabled == 0:
            od_sql = 0
        else:
            od_sql = None
        if on_demand_historical is True or on_demand_historical == 1:
            hist_sql: int | None = 1
        elif on_demand_historical is False or on_demand_historical == 0:
            hist_sql = 0
        else:
            hist_sql = None
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET plan_name = ?,
                    on_demand_enabled = ?,
                    on_demand_historical = ?,
                    spending_checked_at = ?,
                    spending_error = ?,
                    updated_at = ?
                WHERE email = ?
                """,
                (
                    name,
                    od_sql,
                    hist_sql,
                    now,
                    err,
                    now,
                    email,
                ),
            )

    def delete_account(self, email: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM accounts WHERE email = ?", (email,))


_default_store: TokenStore | None = None


def get_default_store() -> TokenStore:
    global _default_store
    if _default_store is None:
        _default_store = TokenStore()
    return _default_store
