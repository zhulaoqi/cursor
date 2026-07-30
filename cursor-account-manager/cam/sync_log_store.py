"""每日同步任务日志存储（SQLite）。"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from .config import SETTINGS


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_job_run (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT NOT NULL UNIQUE,
    biz_date                TEXT NOT NULL,
    trigger_type            TEXT NOT NULL,
    status                  TEXT NOT NULL,
    started_at              INTEGER NOT NULL,
    ended_at                INTEGER,
    duration_sec            INTEGER,
    account_total           INTEGER DEFAULT 0,
    account_snapshot_total  INTEGER DEFAULT 0,
    new_account_count       INTEGER DEFAULT 0,
    account_success         INTEGER DEFAULT 0,
    account_failed          INTEGER DEFAULT 0,
    event_total             INTEGER DEFAULT 0,
    ods_rows                INTEGER DEFAULT 0,
    error_summary           TEXT DEFAULT '',
    created_at              INTEGER NOT NULL,
    updated_at              INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sync_job_run_biz_date ON sync_job_run(biz_date);
CREATE INDEX IF NOT EXISTS idx_sync_job_run_status ON sync_job_run(status);
CREATE INDEX IF NOT EXISTS idx_sync_job_run_trigger
ON sync_job_run(trigger_type, run_id);

CREATE TABLE IF NOT EXISTS sync_job_account_log (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT NOT NULL,
    account_email           TEXT NOT NULL,
    account_source          TEXT DEFAULT '',
    is_new_account          INTEGER DEFAULT 0,
    status                  TEXT NOT NULL,
    started_at              INTEGER NOT NULL,
    ended_at                INTEGER,
    duration_sec            INTEGER,
    fetch_rows              INTEGER DEFAULT 0,
    load_rows               INTEGER DEFAULT 0,
    error_message           TEXT DEFAULT '',
    created_at              INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sync_job_account_run ON sync_job_account_log(run_id);
CREATE INDEX IF NOT EXISTS idx_sync_job_account_email ON sync_job_account_log(account_email);
CREATE INDEX IF NOT EXISTS idx_sync_job_account_email_run
ON sync_job_account_log(account_email, run_id, ended_at);

CREATE TABLE IF NOT EXISTS sync_job_stage_log (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  TEXT NOT NULL,
    stage                   TEXT NOT NULL,
    status                  TEXT NOT NULL,
    message                 TEXT DEFAULT '',
    ts                      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sync_job_stage_run ON sync_job_stage_log(run_id);
"""


@dataclass(frozen=True)
class AccountAttemptState:
    """账号在一个触发时间槽内的持久尝试状态。"""

    attempts: int
    last_failed_at: int | None
    succeeded: bool


@dataclass(frozen=True)
class UsageCollectOutcome:
    """账号最近一次用量采集终态（供看板数据状态悬停）。"""

    email: str
    status: str
    error_message: str
    ended_at: int


class SyncLogStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or SETTINGS.tokens_db
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

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

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def create_run(
        self,
        *,
        run_id: str,
        biz_date: str,
        trigger_type: str,
        account_total: int,
        account_snapshot_total: int,
        new_account_count: int,
    ) -> None:
        now = int(time.time())
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO sync_job_run (
                    run_id, biz_date, trigger_type, status, started_at,
                    account_total, account_snapshot_total, new_account_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    biz_date = excluded.biz_date,
                    trigger_type = excluded.trigger_type,
                    status = 'running',
                    started_at = excluded.started_at,
                    ended_at = NULL,
                    duration_sec = NULL,
                    account_total = excluded.account_total,
                    account_snapshot_total = excluded.account_snapshot_total,
                    new_account_count = excluded.new_account_count,
                    account_success = 0,
                    account_failed = 0,
                    event_total = 0,
                    ods_rows = 0,
                    error_summary = '',
                    updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    biz_date,
                    trigger_type,
                    now,
                    account_total,
                    account_snapshot_total,
                    new_account_count,
                    now,
                    now,
                ),
            )

    def finish_run(
        self,
        *,
        run_id: str,
        status: str,
        account_success: int,
        account_failed: int,
        event_total: int,
        ods_rows: int,
        error_summary: str = "",
    ) -> None:
        now = int(time.time())
        with self._conn() as conn:
            row = conn.execute(
                "SELECT started_at FROM sync_job_run WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            started_at = int(row["started_at"]) if row else now
            duration_sec = max(0, now - started_at)
            conn.execute(
                """
                UPDATE sync_job_run
                SET status = ?, ended_at = ?, duration_sec = ?,
                    account_success = ?, account_failed = ?,
                    event_total = ?, ods_rows = ?,
                    error_summary = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    status,
                    now,
                    duration_sec,
                    account_success,
                    account_failed,
                    event_total,
                    ods_rows,
                    error_summary[:2000],
                    now,
                    run_id,
                ),
            )

    def add_stage(self, *, run_id: str, stage: str, status: str, message: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO sync_job_stage_log (run_id, stage, status, message, ts)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, stage, status, message[:2000], int(time.time())),
            )

    def add_account_log(
        self,
        *,
        run_id: str,
        account_email: str,
        account_source: str,
        is_new_account: bool,
        status: str,
        started_at: int,
        ended_at: int,
        fetch_rows: int,
        load_rows: int,
        error_message: str = "",
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO sync_job_account_log (
                    run_id, account_email, account_source, is_new_account, status,
                    started_at, ended_at, duration_sec, fetch_rows, load_rows,
                    error_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    account_email,
                    account_source,
                    1 if is_new_account else 0,
                    status,
                    started_at,
                    ended_at,
                    max(0, ended_at - started_at),
                    fetch_rows,
                    load_rows,
                    error_message[:2000],
                    int(time.time()),
                ),
            )

    def get_run(self, run_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM sync_job_run WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_failed_accounts(self, run_id: str) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT account_email FROM sync_job_account_log
                WHERE run_id = ? AND status = 'failed'
                ORDER BY id ASC
                """,
                (run_id,),
            ).fetchall()
            return [str(r["account_email"]) for r in rows]

    def get_latest_run(self, biz_date: Optional[str] = None) -> Optional[dict]:
        with self._conn() as conn:
            if biz_date:
                row = conn.execute(
                    """
                    SELECT * FROM sync_job_run
                    WHERE biz_date = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (biz_date,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM sync_job_run ORDER BY id DESC LIMIT 1"
                ).fetchone()
            return dict(row) if row else None

    def list_runs(self, limit: int = 30) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sync_job_run
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def has_run_for_trigger(self, *, biz_date: str, trigger_type: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM sync_job_run
                WHERE biz_date = ? AND trigger_type = ?
                LIMIT 1
                """,
                (biz_date, trigger_type),
            ).fetchone()
            return row is not None

    def get_latest_usage_collect_outcome(
        self,
        account_email: str,
    ) -> UsageCollectOutcome | None:
        """读取账号最近一条用量快照采集终态（failed/skipped/success）。"""
        if not isinstance(account_email, str) or not account_email.strip():
            raise ValueError("account_email 去除首尾空白后不能为空")
        normalized = account_email.strip().lower()
        mapping = self.map_latest_usage_collect_outcomes([normalized])
        return mapping.get(normalized)

    def map_latest_usage_collect_outcomes(
        self,
        emails: list[str],
    ) -> dict[str, UsageCollectOutcome]:
        """批量读取若干账号最近一次用量快照采集终态。"""
        normalized = sorted(
            {
                email.strip().lower()
                for email in emails
                if isinstance(email, str) and email.strip()
            }
        )
        if not normalized:
            return {}

        placeholders = ",".join("?" for _ in normalized)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT account_email, status, error_message, ended_at
                FROM (
                    SELECT
                        lower(trim(account_email)) AS account_email,
                        status,
                        COALESCE(error_message, '') AS error_message,
                        ended_at,
                        ROW_NUMBER() OVER (
                            PARTITION BY lower(trim(account_email))
                            ORDER BY ended_at DESC, id DESC
                        ) AS rn
                    FROM sync_job_account_log
                    WHERE account_source = 'usage_snapshot'
                      AND status IN ('success', 'failed', 'skipped')
                      AND lower(trim(account_email)) IN ({placeholders})
                )
                WHERE rn = 1
                """,
                normalized,
            ).fetchall()

        return {
            str(row["account_email"]): UsageCollectOutcome(
                email=str(row["account_email"]),
                status=str(row["status"]),
                error_message=str(row["error_message"] or ""),
                ended_at=int(row["ended_at"]),
            )
            for row in rows
        }

    def get_account_attempt_state(
        self,
        *,
        account_email: str,
        trigger_type: str,
    ) -> AccountAttemptState:
        """规范化账号后读取精确触发时间槽内的终态日志汇总。"""
        if not isinstance(account_email, str) or not account_email.strip():
            raise ValueError("account_email 去除首尾空白后不能为空")
        normalized_email = account_email.strip().lower()
        if not isinstance(trigger_type, str) or not trigger_type.strip():
            raise ValueError("trigger_type 不能为空")

        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS attempts,
                    MAX(CASE WHEN a.status = 'failed' THEN a.ended_at END)
                        AS last_failed_at,
                    MAX(CASE WHEN a.status = 'success' THEN 1 ELSE 0 END)
                        AS succeeded
                FROM sync_job_account_log AS a
                JOIN sync_job_run AS r ON r.run_id = a.run_id
                WHERE a.account_email = ?
                  AND r.trigger_type = ?
                  AND a.status IN ('success', 'failed', 'skipped')
                """,
                (normalized_email, trigger_type),
            ).fetchone()

        return AccountAttemptState(
            attempts=int(row["attempts"] or 0),
            last_failed_at=(
                int(row["last_failed_at"])
                if row["last_failed_at"] is not None
                else None
            ),
            succeeded=bool(row["succeeded"]),
        )

    def list_stage_logs(self, run_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sync_job_stage_log
                WHERE run_id = ?
                ORDER BY id ASC
                """,
                (run_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_account_logs(self, run_id: str, *, status: Optional[str] = None) -> list[dict]:
        with self._conn() as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT * FROM sync_job_account_log
                    WHERE run_id = ? AND status = ?
                    ORDER BY id ASC
                    """,
                    (run_id, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM sync_job_account_log
                    WHERE run_id = ?
                    ORDER BY id ASC
                    """,
                    (run_id,),
                ).fetchall()
            return [dict(r) for r in rows]

    def delete_run(self, run_id: str) -> bool:
        """删除单次 run 及其阶段/账号明细日志。"""
        with self._conn() as conn:
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM sync_job_stage_log WHERE run_id = ?", (run_id,))
                conn.execute("DELETE FROM sync_job_account_log WHERE run_id = ?", (run_id,))
                cur = conn.execute("DELETE FROM sync_job_run WHERE run_id = ?", (run_id,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return int(cur.rowcount or 0) > 0


_default_sync_log_store: SyncLogStore | None = None


def get_default_sync_log_store() -> SyncLogStore:
    global _default_sync_log_store
    if _default_sync_log_store is None:
        _default_sync_log_store = SyncLogStore()
    return _default_sync_log_store

