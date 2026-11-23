"""Devin 数据存储：与 Cursor 账号完全独立的三张 SQLite 表（同库 tokens.db）。

- ``devin_accounts``  账号库（邮箱 + IMAP 授权码），独立于 Cursor 的 ``accounts``
- ``devin_snapshots`` 每次采集一条快照（成功/失败都落，保留原始 JSON 便于回溯与调解析）
- ``devin_cycles``    账期（每账号每个计费周期一行，随成功采集自动刷新）
- ``devin_runs``      每次批量采集（定时/手动）的汇总，用于看板与飞书播报
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import SETTINGS
from .devin_parser import cycle_start_from_end, missing_fields

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devin_accounts (
    email         TEXT PRIMARY KEY,
    imap_password TEXT NOT NULL DEFAULT '',
    imap_host     TEXT NOT NULL DEFAULT 'imap.feishu.cn',
    imap_port     INTEGER NOT NULL DEFAULT 993,
    owner_email   TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'active',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_login_at INTEGER NOT NULL DEFAULT 0,
    added_at      INTEGER NOT NULL DEFAULT 0,
    updated_at    INTEGER NOT NULL DEFAULT 0,
    source        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS devin_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL,
    run_id        TEXT NOT NULL DEFAULT '',
    collected_at  INTEGER NOT NULL,
    ok            INTEGER NOT NULL DEFAULT 0,
    error_code    TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    login_used    INTEGER NOT NULL DEFAULT 0,
    plan_name     TEXT NOT NULL DEFAULT '',
    plan_price_usd REAL,
    billing_period TEXT NOT NULL DEFAULT '',
    renews_at     TEXT NOT NULL DEFAULT '',
    weekly_used_pct REAL,
    weekly_reset_at INTEGER,
    daily_used_pct  REAL,
    daily_reset_at  INTEGER,
    on_demand_used_usd    REAL,
    on_demand_balance_usd REAL,
    message_limit_usd     REAL,
    source        TEXT NOT NULL DEFAULT '',
    raw_json      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_devin_snap_email ON devin_snapshots(email, id DESC);
CREATE INDEX IF NOT EXISTS idx_devin_snap_run ON devin_snapshots(run_id);

-- 账期：每个账号每个计费周期一行（cycle_end = 续费日）。每次成功采集都会刷新该行的
-- “最新值 / 峰值”，周期结束后最后一行数据即为该账期的最终用量。
CREATE TABLE IF NOT EXISTS devin_cycles (
    email         TEXT NOT NULL,
    cycle_end     TEXT NOT NULL,
    cycle_start   TEXT NOT NULL DEFAULT '',
    billing_period TEXT NOT NULL DEFAULT 'month',
    plan_name     TEXT NOT NULL DEFAULT '',
    plan_price_usd REAL,
    first_seen_at INTEGER NOT NULL,
    last_seen_at  INTEGER NOT NULL,
    samples       INTEGER NOT NULL DEFAULT 1,
    last_snapshot_id INTEGER NOT NULL DEFAULT 0,
    weekly_used_pct      REAL,
    peak_weekly_used_pct REAL,
    on_demand_used_usd      REAL,
    peak_on_demand_used_usd REAL,
    on_demand_balance_usd   REAL,
    PRIMARY KEY (email, cycle_end)
);
CREATE INDEX IF NOT EXISTS idx_devin_cycles_end ON devin_cycles(cycle_end DESC);

CREATE TABLE IF NOT EXISTS devin_runs (
    run_id      TEXT PRIMARY KEY,
    trigger     TEXT NOT NULL DEFAULT 'manual',
    status      TEXT NOT NULL DEFAULT 'running',
    started_at  INTEGER NOT NULL,
    finished_at INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    success     INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    failure_breakdown TEXT NOT NULL DEFAULT '{}',
    failures_json     TEXT NOT NULL DEFAULT '[]',
    message     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_devin_runs_started ON devin_runs(started_at DESC);
"""

# 快照里可作为"数据列"落库/返回的字段
SNAPSHOT_DATA_FIELDS = (
    "plan_name",
    "plan_price_usd",
    "billing_period",
    "renews_at",
    "weekly_used_pct",
    "weekly_reset_at",
    "daily_used_pct",
    "daily_reset_at",
    "on_demand_used_usd",
    "on_demand_balance_usd",
    "message_limit_usd",
)


class DevinStore:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or SETTINGS.tokens_db
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self.db_path, timeout=30, isolation_level=None, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            with self._lock:
                yield conn
        finally:
            conn.close()

    # ─── 账号 ─────────────────────────────────────────────────────

    def list_accounts(self, keyword: str = "", limit: int = 0) -> list[dict]:
        kw = (keyword or "").strip().lower()
        sql = "SELECT * FROM devin_accounts"
        args: list[Any] = []
        if kw:
            sql += " WHERE lower(email) LIKE ? OR lower(owner_email) LIKE ? OR lower(note) LIKE ?"
            args += [f"%{kw}%"] * 3
        sql += " ORDER BY email"
        if limit and limit > 0:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def list_active_accounts(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM devin_accounts WHERE status = 'active' ORDER BY email"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_account(self, email: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM devin_accounts WHERE email = ?", (email,)
            ).fetchone()
            return dict(row) if row else None

    def upsert_account(
        self,
        email: str,
        imap_password: str,
        imap_host: str,
        imap_port: int,
        owner_email: str = "",
        note: str = "",
        source: str = "",
    ) -> None:
        now = int(time.time())
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO devin_accounts
                    (email, imap_password, imap_host, imap_port, owner_email, note,
                     status, added_at, updated_at, source)
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    imap_password = excluded.imap_password,
                    imap_host     = excluded.imap_host,
                    imap_port     = excluded.imap_port,
                    owner_email   = excluded.owner_email,
                    note          = CASE WHEN excluded.note = '' THEN devin_accounts.note ELSE excluded.note END,
                    updated_at    = excluded.updated_at,
                    source        = excluded.source
                """,
                (email, imap_password, imap_host, int(imap_port), owner_email, note, now, now, source),
            )

    def delete_account(self, email: str, *, purge_snapshots: bool = False) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM devin_accounts WHERE email = ?", (email,))
            if purge_snapshots:
                conn.execute("DELETE FROM devin_snapshots WHERE email = ?", (email,))
                conn.execute("DELETE FROM devin_cycles WHERE email = ?", (email,))

    def set_status(self, email: str, status: str) -> None:
        if status not in ("active", "disabled"):
            raise ValueError("status 只能是 active / disabled")
        with self._conn() as conn:
            conn.execute(
                "UPDATE devin_accounts SET status = ?, consecutive_failures = CASE WHEN ? = 'active' THEN 0 ELSE consecutive_failures END, updated_at = ? WHERE email = ?",
                (status, status, int(time.time()), email),
            )

    def update_note(self, email: str, note: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE devin_accounts SET note = ?, updated_at = ? WHERE email = ?",
                (note[:500], int(time.time()), email),
            )

    def mark_success(self, email: str, *, login_used: bool) -> None:
        with self._conn() as conn:
            if login_used:
                conn.execute(
                    "UPDATE devin_accounts SET consecutive_failures = 0, last_login_at = ? WHERE email = ?",
                    (int(time.time()), email),
                )
            else:
                conn.execute(
                    "UPDATE devin_accounts SET consecutive_failures = 0 WHERE email = ?",
                    (email,),
                )

    def bump_failure(self, email: str) -> int:
        with self._conn() as conn:
            conn.execute(
                "UPDATE devin_accounts SET consecutive_failures = consecutive_failures + 1 WHERE email = ?",
                (email,),
            )
            row = conn.execute(
                "SELECT consecutive_failures FROM devin_accounts WHERE email = ?", (email,)
            ).fetchone()
            return int(row["consecutive_failures"]) if row else 0

    # ─── 快照 ─────────────────────────────────────────────────────

    def add_snapshot(
        self,
        *,
        email: str,
        ok: bool,
        run_id: str = "",
        error_code: str = "",
        error_message: str = "",
        duration_ms: int = 0,
        login_used: bool = False,
        data: dict[str, Any] | None = None,
        source: str = "",
        raw: Any = None,
        collected_at: int | None = None,
    ) -> int:
        data = data or {}
        fields = {k: data.get(k) for k in SNAPSHOT_DATA_FIELDS}
        for text_key in ("plan_name", "billing_period", "renews_at"):
            fields[text_key] = fields[text_key] or ""
        raw_text = ""
        if raw not in (None, "", {}, []):
            raw_text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        cols = [
            "email", "run_id", "collected_at", "ok", "error_code", "error_message",
            "duration_ms", "login_used", "source", "raw_json", *SNAPSHOT_DATA_FIELDS,
        ]
        values = [
            email, run_id, int(collected_at or time.time()), 1 if ok else 0,
            error_code, (error_message or "")[:2000], int(duration_ms), 1 if login_used else 0,
            source, raw_text, *[fields[k] for k in SNAPSHOT_DATA_FIELDS],
        ]
        with self._conn() as conn:
            cur = conn.execute(
                f"INSERT INTO devin_snapshots ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                values,
            )
            snapshot_id = int(cur.lastrowid)
            if ok:
                self._upsert_cycle(
                    conn, email, snapshot_id, int(collected_at or time.time()),
                    {**fields, "cycle_start": data.get("cycle_start")},
                )
            return snapshot_id

    # ─── 账期 ─────────────────────────────────────────────────────

    @staticmethod
    def _upsert_cycle(conn: sqlite3.Connection, email: str, snapshot_id: int, seen_at: int, f: dict[str, Any]) -> None:
        """成功快照带续费日时，刷新对应账期行（没有续费日，如 Free 套餐，不记账期）。"""
        cycle_end = (f.get("renews_at") or "").strip()
        if not cycle_end:
            return
        period = (f.get("billing_period") or "month").strip() or "month"
        derived = cycle_start_from_end(cycle_end, period)
        if not derived:
            return
        # 页面/API 直接给了起点就用真实值，否则按计费周期往前推
        given = (f.get("cycle_start") or "").strip() if isinstance(f.get("cycle_start"), str) else ""
        cycle_start = given if re.fullmatch(r"\d{4}-\d{2}-\d{2}", given) and given < cycle_end else derived
        old = conn.execute(
            "SELECT * FROM devin_cycles WHERE email = ? AND cycle_end = ?", (email, cycle_end)
        ).fetchone()

        def peak(prev: Any, cur: Any) -> Any:
            if cur is None:
                return prev
            return cur if prev is None else max(prev, cur)

        if old is None:
            conn.execute(
                """
                INSERT INTO devin_cycles (email, cycle_end, cycle_start, billing_period, plan_name, plan_price_usd,
                    first_seen_at, last_seen_at, samples, last_snapshot_id, weekly_used_pct, peak_weekly_used_pct,
                    on_demand_used_usd, peak_on_demand_used_usd, on_demand_balance_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    email, cycle_end, cycle_start, period, f.get("plan_name") or "", f.get("plan_price_usd"),
                    seen_at, seen_at, snapshot_id, f.get("weekly_used_pct"), f.get("weekly_used_pct"),
                    f.get("on_demand_used_usd"), f.get("on_demand_used_usd"), f.get("on_demand_balance_usd"),
                ),
            )
            return
        conn.execute(
            """
            UPDATE devin_cycles SET cycle_start = ?, billing_period = ?, plan_name = ?, plan_price_usd = ?,
                last_seen_at = ?, samples = samples + 1, last_snapshot_id = ?,
                weekly_used_pct = COALESCE(?, weekly_used_pct), peak_weekly_used_pct = ?,
                on_demand_used_usd = COALESCE(?, on_demand_used_usd), peak_on_demand_used_usd = ?,
                on_demand_balance_usd = COALESCE(?, on_demand_balance_usd)
            WHERE email = ? AND cycle_end = ?
            """,
            (
                cycle_start, period, f.get("plan_name") or old["plan_name"],
                f.get("plan_price_usd") if f.get("plan_price_usd") is not None else old["plan_price_usd"],
                seen_at, snapshot_id,
                f.get("weekly_used_pct"), peak(old["peak_weekly_used_pct"], f.get("weekly_used_pct")),
                f.get("on_demand_used_usd"), peak(old["peak_on_demand_used_usd"], f.get("on_demand_used_usd")),
                f.get("on_demand_balance_usd"),
                email, cycle_end,
            ),
        )

    def list_cycles(self, email: str = "", limit: int = 500) -> list[dict]:
        """账期明细（按账期终点倒序）。email 为空 = 全部账号。"""
        sql = "SELECT * FROM devin_cycles"
        args: list[Any] = []
        if email:
            sql += " WHERE email = ?"
            args.append(email)
        sql += " ORDER BY cycle_end DESC, email LIMIT ?"
        args.append(max(1, min(int(limit), 5000)))
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def get_snapshot(self, snapshot_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM devin_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_snapshots(self, email: str, limit: int = 30) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, email, run_id, collected_at, ok, error_code, error_message, duration_ms,
                       login_used, source, plan_name, plan_price_usd, billing_period, renews_at,
                       weekly_used_pct, weekly_reset_at, daily_used_pct, daily_reset_at,
                       on_demand_used_usd, on_demand_balance_usd, message_limit_usd
                FROM devin_snapshots WHERE email = ? ORDER BY id DESC LIMIT ?
                """,
                (email, max(1, min(int(limit), 200))),
            ).fetchall()
            return [dict(r) for r in rows]

    def dashboard_rows(self) -> list[dict]:
        """每个账号：账号信息 + 最近一次成功快照的数据 + 最近一次尝试的结果。"""
        with self._conn() as conn:
            accounts = [dict(r) for r in conn.execute(
                "SELECT * FROM devin_accounts ORDER BY email"
            ).fetchall()]
            last_ok = {
                r["email"]: dict(r)
                for r in conn.execute(
                    """
                    SELECT s.* FROM devin_snapshots s
                    JOIN (SELECT email, MAX(id) AS mid FROM devin_snapshots WHERE ok = 1 GROUP BY email) m
                      ON s.id = m.mid
                    """
                ).fetchall()
            }
            last_try = {
                r["email"]: dict(r)
                for r in conn.execute(
                    """
                    SELECT s.id, s.email, s.collected_at, s.ok, s.error_code, s.error_message
                    FROM devin_snapshots s
                    JOIN (SELECT email, MAX(id) AS mid FROM devin_snapshots GROUP BY email) m
                      ON s.id = m.mid
                    """
                ).fetchall()
            }
            real_start = {
                (r["email"], r["cycle_end"]): r["cycle_start"]
                for r in conn.execute("SELECT email, cycle_end, cycle_start FROM devin_cycles").fetchall()
            }
        rows: list[dict] = []
        for acc in accounts:
            email = acc["email"]
            ok = last_ok.get(email) or {}
            attempt = last_try.get(email) or {}
            row = {
                "email": email,
                "owner_email": acc["owner_email"],
                "note": acc["note"],
                "status": acc["status"],
                "consecutive_failures": acc["consecutive_failures"],
                "imap_host": acc["imap_host"],
                "last_login_at": acc["last_login_at"],
                "snapshot_id": ok.get("id"),
                "data_collected_at": ok.get("collected_at"),
                "source": ok.get("source", ""),
                "last_attempt_at": attempt.get("collected_at"),
                "last_attempt_ok": bool(attempt.get("ok")) if attempt else None,
                "last_error_code": attempt.get("error_code", "") if attempt and not attempt.get("ok") else "",
                "last_error_message": attempt.get("error_message", "") if attempt and not attempt.get("ok") else "",
            }
            for key in SNAPSHOT_DATA_FIELDS:
                row[key] = ok.get(key)
            row["cycle_end"] = row.get("renews_at") or ""
            row["cycle_start"] = (
                real_start.get((email, row["cycle_end"]))
                or cycle_start_from_end(row["cycle_end"], row.get("billing_period") or "month")
                if row["cycle_end"] else ""
            )
            row["missing"] = missing_fields(row) if ok else []
            rows.append(row)
        return rows

    # ─── 批次 ─────────────────────────────────────────────────────

    def start_run(self, trigger: str, total: int) -> str:
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO devin_runs (run_id, trigger, status, started_at, total) VALUES (?, ?, 'running', ?, ?)",
                (run_id, trigger, int(time.time()), int(total)),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        success: int,
        failed: int,
        skipped: int,
        failure_breakdown: dict[str, int],
        failures: list[dict],
        message: str = "",
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE devin_runs SET status = ?, finished_at = ?, success = ?, failed = ?, skipped = ?,
                    failure_breakdown = ?, failures_json = ?, message = ? WHERE run_id = ?
                """,
                (
                    status, int(time.time()), success, failed, skipped,
                    json.dumps(failure_breakdown, ensure_ascii=False),
                    json.dumps(failures[:200], ensure_ascii=False),
                    message[:500], run_id,
                ),
            )

    def fail_stale_runs(self) -> int:
        """进程重启后把遗留的 running 批次标记为 interrupted。"""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE devin_runs SET status = 'interrupted', finished_at = ?, message = '进程重启，批次中断' WHERE status = 'running'",
                (int(time.time()),),
            )
            return int(cur.rowcount or 0)

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict:
        d = dict(row)
        for key in ("failure_breakdown", "failures_json"):
            try:
                d[key] = json.loads(d.get(key) or ("{}" if key == "failure_breakdown" else "[]"))
            except ValueError:
                d[key] = {} if key == "failure_breakdown" else []
        return d

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM devin_runs ORDER BY started_at DESC, run_id DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
            return [self._run_row(r) for r in rows]

    def get_run(self, run_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM devin_runs WHERE run_id = ?", (run_id,)).fetchone()
            return self._run_row(row) if row else None


_default_store: DevinStore | None = None
_default_lock = threading.Lock()


def get_default_devin_store() -> DevinStore:
    global _default_store
    with _default_lock:
        if _default_store is None:
            _default_store = DevinStore()
        return _default_store
