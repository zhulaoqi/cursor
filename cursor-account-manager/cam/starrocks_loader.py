"""StarRocks 装载器（MySQL 协议）。"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterator, Optional

import pymysql

from .config import SETTINGS


_JDBC_RE = re.compile(r"^jdbc:mysql://(?P<host>[^:/]+)(?::(?P<port>\d+))?/(?P<db>[^?]+)")
_JOB_ID_RE = re.compile(r"job_id\s*=\s*(\d+)")


def _to_decimal(value: object) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        dec = Decimal(str(value))
        if not dec.is_finite():
            return None
        return dec
    except Exception:
        return None


def _fit_decimal(value: object, *, precision: int, scale: int) -> Optional[Decimal]:
    dec = _to_decimal(value)
    if dec is None:
        return None
    try:
        q = Decimal(1).scaleb(-scale)  # scale=6 -> Decimal("0.000001")
        dec = dec.quantize(q, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    # DECIMAL(p,s) 的整数位上限为 p-s
    int_digits = precision - scale
    max_abs = Decimal(10) ** int_digits
    if dec >= max_abs or dec <= -max_abs:
        return None
    return dec


def _to_int_or_none(value: object) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except Exception:
        return None


class StarRocksLoader:
    ODS_TABLE = "ods_cursor_usage_events_di"
    DWD_TABLE = "dwd_cursor_usage_detail_di"

    def __init__(self) -> None:
        jdbc_url = SETTINGS.bi_sync_db_url.strip()
        if not jdbc_url:
            raise ValueError("BI_SYNC_DB_URL 未配置")
        m = _JDBC_RE.match(jdbc_url)
        if not m:
            raise ValueError(f"BI_SYNC_DB_URL 非法: {jdbc_url}")
        self.host = m.group("host")
        self.port = int(m.group("port") or 9030)
        self.db = m.group("db")
        self.username = SETTINGS.bi_sync_db_username.strip()
        self.password = SETTINGS.bi_sync_db_password
        if not self.username:
            raise ValueError("BI_SYNC_DB_USERNAME 未配置")

    @contextmanager
    def _conn(self) -> Iterator[pymysql.connections.Connection]:
        conn = pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.username,
            password=self.password,
            database=self.db,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=pymysql.cursors.Cursor,
        )
        try:
            yield conn
        finally:
            conn.close()

    def ensure_tables(self) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.db}.{self.ODS_TABLE} (
                    dt                      DATE            NOT NULL,
                    account_email           VARCHAR(320)    NOT NULL,
                    event_time              DATETIME        NOT NULL,
                    model_name              VARCHAR(65533)  NULL,
                    run_id                  VARCHAR(128)    NOT NULL,
                    source_file             VARCHAR(65533)  NULL,
                    event_time_bj           DATETIME        NOT NULL,
                    request_id              VARCHAR(65533)  NULL,
                    project_name            VARCHAR(65533)  NULL,
                    message_role            VARCHAR(65533)  NULL,
                    input_tokens            BIGINT          NULL,
                    output_tokens           BIGINT          NULL,
                    cache_read_tokens       BIGINT          NULL,
                    cache_write_tokens      BIGINT          NULL,
                    total_tokens            BIGINT          NULL,
                    cost_usd                DECIMAL(18,6)   NULL,
                    billed_amount_usd       DECIMAL(18,6)   NULL,
                    discount_percent        DECIMAL(8,4)    NULL,
                    raw_event_json          JSON            NULL,
                    ingest_time             DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                ENGINE=OLAP
                DUPLICATE KEY(dt, account_email, event_time)
                PARTITION BY RANGE(dt) ()
                DISTRIBUTED BY HASH(account_email) BUCKETS 16
                PROPERTIES (
                    "dynamic_partition.enable" = "true",
                    "dynamic_partition.time_unit" = "DAY",
                    "dynamic_partition.start" = "-90",
                    "dynamic_partition.end" = "7",
                    "dynamic_partition.prefix" = "p",
                    "dynamic_partition.buckets" = "16",
                    "replication_num" = "2",
                    "in_memory" = "false",
                    "enable_persistent_index" = "false",
                    "replicated_storage" = "true",
                    "storage_medium" = "SSD",
                    "compression" = "LZ4"
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.db}.{self.DWD_TABLE} (
                    dt                      DATE            NOT NULL,
                    account_email           VARCHAR(320)    NOT NULL,
                    event_unique_key        VARCHAR(1024)   NOT NULL,
                    event_time              DATETIME        NOT NULL,
                    event_time_bj           DATETIME        NOT NULL,
                    request_id              VARCHAR(65533)  NULL,
                    model_name              VARCHAR(65533)  NULL,
                    project_name            VARCHAR(65533)  NULL,
                    input_tokens            BIGINT          NULL,
                    output_tokens           BIGINT          NULL,
                    cache_read_tokens       BIGINT          NULL,
                    cache_write_tokens      BIGINT          NULL,
                    total_tokens            BIGINT          NULL,
                    cost_usd                DECIMAL(18,6)   NULL,
                    billed_amount_usd       DECIMAL(18,6)   NULL,
                    discount_percent        DECIMAL(8,4)    NULL,
                    src_run_id              VARCHAR(128)    NOT NULL,
                    etl_time                DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                ENGINE=OLAP
                UNIQUE KEY(dt, account_email, event_unique_key)
                PARTITION BY RANGE(dt) ()
                DISTRIBUTED BY HASH(account_email) BUCKETS 16
                PROPERTIES (
                    "dynamic_partition.enable" = "true",
                    "dynamic_partition.time_unit" = "DAY",
                    "dynamic_partition.start" = "-365",
                    "dynamic_partition.end" = "7",
                    "dynamic_partition.prefix" = "p",
                    "dynamic_partition.buckets" = "16",
                    "replication_num" = "2",
                    "in_memory" = "false",
                    "enable_persistent_index" = "false",
                    "replicated_storage" = "true",
                    "storage_medium" = "SSD",
                    "compression" = "LZ4"
                )
                """
            )

    def _build_tracking_error_message(self, conn: pymysql.connections.Connection, err: Exception) -> str:
        msg = str(err)
        m = _JOB_ID_RE.search(msg)
        if not m:
            return msg
        job_id = m.group(1)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tracking_log FROM information_schema.load_tracking_logs WHERE job_id = %s",
                    (job_id,),
                )
                row = cur.fetchone()
            if not row or not row[0]:
                return msg
            tracking = " ".join(str(row[0]).split())
            if len(tracking) > 1200:
                tracking = tracking[:1200] + "..."
            return f"{msg}; tracking_log={tracking}"
        except Exception as qe:
            return f"{msg}; tracking_log_fetch_failed={qe}"

    def _ensure_date_partition(
        self,
        *,
        conn: pymysql.connections.Connection,
        table_name: str,
        biz_date: str,
    ) -> None:
        """
        确保指定日期分区存在，避免动态分区线程尚未创建导致写入报
        `The row is out of partition ranges`。
        """
        day = datetime.strptime(biz_date, "%Y-%m-%d").date()
        next_day = day + timedelta(days=1)
        part_name = f"p{day.strftime('%Y%m%d')}"
        sql = (
            f"ALTER TABLE {self.db}.{table_name} "
            f"ADD PARTITION {part_name} "
            f"VALUES [('{day.isoformat()}'), ('{next_day.isoformat()}')]"
        )
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
        except Exception as e:
            # 分区已存在或范围重叠时忽略，其余错误继续抛出
            msg = str(e).lower()
            if (
                "exists" in msg
                or "already" in msg
                or "same range" in msg
                or "duplicate partition" in msg
            ):
                return
            raise

    def _normalize_ods_row(self, row: dict) -> dict:
        # 字符串字段保持原样（不 trim、不截断、不改写）；仅对非字符串做 str() 兜底。
        for key in ("run_id", "source_file", "account_email", "model_name", "request_id", "project_name", "message_role"):
            value = row.get(key)
            if value is None:
                row[key] = None
            elif isinstance(value, str):
                row[key] = value
            else:
                row[key] = str(value)
        # key 列必须非空：缺失则显式报错，避免静默篡改原值。
        if row.get("run_id") is None or row.get("account_email") is None:
            raise ValueError("run_id/account_email 不能为空")
        row["input_tokens"] = _to_int_or_none(row.get("input_tokens"))
        row["output_tokens"] = _to_int_or_none(row.get("output_tokens"))
        row["cache_read_tokens"] = _to_int_or_none(row.get("cache_read_tokens"))
        row["cache_write_tokens"] = _to_int_or_none(row.get("cache_write_tokens"))
        row["total_tokens"] = _to_int_or_none(row.get("total_tokens"))
        row["cost_usd"] = _fit_decimal(row.get("cost_usd"), precision=18, scale=6)
        row["billed_amount_usd"] = _fit_decimal(row.get("billed_amount_usd"), precision=18, scale=6)
        row["discount_percent"] = _fit_decimal(row.get("discount_percent"), precision=8, scale=4)
        for dt_key in ("event_time", "event_time_bj"):
            v = row.get(dt_key)
            if isinstance(v, datetime):
                row[dt_key] = self.ensure_datetime(v)
        return row

    def replace_ods_rows(self, *, biz_date: str, rows: list[dict]) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            self._ensure_date_partition(conn=conn, table_name=self.ODS_TABLE, biz_date=biz_date)
            cur.execute(
                f"DELETE FROM {self.db}.{self.ODS_TABLE} WHERE dt = %s",
                (biz_date,),
            )
            if not rows:
                return 0
            sql = (
                f"INSERT INTO {self.db}.{self.ODS_TABLE} ("
                "dt, run_id, source_file, account_email, event_time, event_time_bj, "
                "model_name, request_id, project_name, message_role, "
                "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, total_tokens, "
                "cost_usd, billed_amount_usd, discount_percent, raw_event_json"
                ") VALUES ("
                "%(dt)s, %(run_id)s, %(source_file)s, %(account_email)s, %(event_time)s, %(event_time_bj)s, "
                "%(model_name)s, %(request_id)s, %(project_name)s, %(message_role)s, "
                "%(input_tokens)s, %(output_tokens)s, %(cache_read_tokens)s, %(cache_write_tokens)s, %(total_tokens)s, "
                "%(cost_usd)s, %(billed_amount_usd)s, %(discount_percent)s, %(raw_event_json)s"
                ")"
            )
            try:
                cur.executemany(sql, rows)
            except Exception as e:
                raise RuntimeError(self._build_tracking_error_message(conn, e)) from e
            return len(rows)

    def replace_ods_rows_for_account(
        self,
        *,
        biz_date: str,
        account_email: str,
        rows: list[dict],
    ) -> int:
        """
        按账号+日期覆盖写入 ODS。
        语义：仅清理该账号该日期历史数据，然后写入新数据。
        """
        with self._conn() as conn, conn.cursor() as cur:
            self._ensure_date_partition(conn=conn, table_name=self.ODS_TABLE, biz_date=biz_date)
            cur.execute(
                f"DELETE FROM {self.db}.{self.ODS_TABLE} WHERE dt = %s AND account_email = %s",
                (biz_date, account_email),
            )
            if not rows:
                return 0
            normalized_rows = [self._normalize_ods_row(dict(r)) for r in rows]
            sql = (
                f"INSERT INTO {self.db}.{self.ODS_TABLE} ("
                "dt, run_id, source_file, account_email, event_time, event_time_bj, "
                "model_name, request_id, project_name, message_role, "
                "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, total_tokens, "
                "cost_usd, billed_amount_usd, discount_percent, raw_event_json"
                ") VALUES ("
                "%(dt)s, %(run_id)s, %(source_file)s, %(account_email)s, %(event_time)s, %(event_time_bj)s, "
                "%(model_name)s, %(request_id)s, %(project_name)s, %(message_role)s, "
                "%(input_tokens)s, %(output_tokens)s, %(cache_read_tokens)s, %(cache_write_tokens)s, %(total_tokens)s, "
                "%(cost_usd)s, %(billed_amount_usd)s, %(discount_percent)s, %(raw_event_json)s"
                ")"
            )
            try:
                cur.executemany(sql, normalized_rows)
            except Exception as e:
                raise RuntimeError(self._build_tracking_error_message(conn, e)) from e
            return len(normalized_rows)

    def rebuild_dwd_for_date(self, *, biz_date: str) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            self._ensure_date_partition(conn=conn, table_name=self.DWD_TABLE, biz_date=biz_date)
            cur.execute(
                f"DELETE FROM {self.db}.{self.DWD_TABLE} WHERE dt = %s",
                (biz_date,),
            )
            try:
                cur.execute(
                    f"""
                    INSERT INTO {self.db}.{self.DWD_TABLE} (
                        dt, account_email, event_unique_key, event_time, event_time_bj,
                        request_id, model_name, project_name,
                        input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, total_tokens,
                        cost_usd, billed_amount_usd, discount_percent, src_run_id
                    )
                    SELECT
                        dt,
                        account_email,
                        md5(concat(
                            account_email, '|',
                            ifnull(request_id, ''), '|',
                            date_format(event_time, '%%Y-%%m-%%d %%H:%%i:%%s'), '|',
                            ifnull(model_name, ''), '|',
                            ifnull(cast(total_tokens as varchar(32)), '0'), '|',
                            ifnull(cast(cost_usd as varchar(64)), '0')
                        )) AS event_unique_key,
                        event_time,
                        event_time_bj,
                        request_id,
                        model_name,
                        project_name,
                        input_tokens,
                        output_tokens,
                        cache_read_tokens,
                        cache_write_tokens,
                        total_tokens,
                        cost_usd,
                        billed_amount_usd,
                        discount_percent,
                        run_id
                    FROM {self.db}.{self.ODS_TABLE}
                    WHERE dt = %s
                    """,
                    (biz_date,),
                )
            except Exception as e:
                raise RuntimeError(self._build_tracking_error_message(conn, e)) from e
            cur.execute(
                f"SELECT COUNT(1) FROM {self.db}.{self.DWD_TABLE} WHERE dt = %s",
                (biz_date,),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def rebuild_dwd_for_account_date(self, *, biz_date: str, account_email: str) -> int:
        """
        按账号+日期重建 DWD。
        语义：仅重建该账号该日期，避免整天全量重刷。
        """
        with self._conn() as conn, conn.cursor() as cur:
            self._ensure_date_partition(conn=conn, table_name=self.DWD_TABLE, biz_date=biz_date)
            cur.execute(
                f"DELETE FROM {self.db}.{self.DWD_TABLE} WHERE dt = %s AND account_email = %s",
                (biz_date, account_email),
            )
            try:
                cur.execute(
                    f"""
                    INSERT INTO {self.db}.{self.DWD_TABLE} (
                        dt, account_email, event_unique_key, event_time, event_time_bj,
                        request_id, model_name, project_name,
                        input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, total_tokens,
                        cost_usd, billed_amount_usd, discount_percent, src_run_id
                    )
                    SELECT
                        dt,
                        account_email,
                        md5(concat(
                            account_email, '|',
                            ifnull(request_id, ''), '|',
                            date_format(event_time, '%%Y-%%m-%%d %%H:%%i:%%s'), '|',
                            ifnull(model_name, ''), '|',
                            ifnull(cast(total_tokens as varchar(32)), '0'), '|',
                            ifnull(cast(cost_usd as varchar(64)), '0')
                        )) AS event_unique_key,
                        event_time,
                        event_time_bj,
                        request_id,
                        model_name,
                        project_name,
                        input_tokens,
                        output_tokens,
                        cache_read_tokens,
                        cache_write_tokens,
                        total_tokens,
                        cost_usd,
                        billed_amount_usd,
                        discount_percent,
                        run_id
                    FROM {self.db}.{self.ODS_TABLE}
                    WHERE dt = %s AND account_email = %s
                    """,
                    (biz_date, account_email),
                )
            except Exception as e:
                raise RuntimeError(self._build_tracking_error_message(conn, e)) from e
            cur.execute(
                f"SELECT COUNT(1) FROM {self.db}.{self.DWD_TABLE} WHERE dt = %s AND account_email = %s",
                (biz_date, account_email),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def normalize_decimal_fields(self, row: dict) -> dict:
        return self._normalize_ods_row(row)

    @staticmethod
    def ensure_datetime(value: datetime) -> datetime:
        return value.replace(microsecond=0)

