"""StarRocks 装载器（MySQL 协议）。"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Iterator, Optional

import pymysql

from .config import SETTINGS


_JDBC_RE = re.compile(r"^jdbc:mysql://(?P<host>[^:/]+)(?::(?P<port>\d+))?/(?P<db>[^?]+)")


def _to_decimal(value: object) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
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
                    run_id                  VARCHAR(64)     NOT NULL,
                    source_file             VARCHAR(255)    NULL,
                    account_email           VARCHAR(128)    NOT NULL,
                    event_time              DATETIME        NOT NULL,
                    event_time_bj           DATETIME        NOT NULL,
                    model_name              VARCHAR(128)    NULL,
                    request_id              VARCHAR(128)    NULL,
                    project_name            VARCHAR(255)    NULL,
                    message_role            VARCHAR(32)     NULL,
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
                DUPLICATE KEY(dt, account_email, event_time, model_name)
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
                    account_email           VARCHAR(128)    NOT NULL,
                    event_time              DATETIME        NOT NULL,
                    event_time_bj           DATETIME        NOT NULL,
                    event_unique_key        VARCHAR(256)    NOT NULL,
                    request_id              VARCHAR(128)    NULL,
                    model_name              VARCHAR(128)    NULL,
                    project_name            VARCHAR(255)    NULL,
                    input_tokens            BIGINT          NULL,
                    output_tokens           BIGINT          NULL,
                    cache_read_tokens       BIGINT          NULL,
                    cache_write_tokens      BIGINT          NULL,
                    total_tokens            BIGINT          NULL,
                    cost_usd                DECIMAL(18,6)   NULL,
                    billed_amount_usd       DECIMAL(18,6)   NULL,
                    discount_percent        DECIMAL(8,4)    NULL,
                    src_run_id              VARCHAR(64)     NOT NULL,
                    etl_time                DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                ENGINE=OLAP
                UNIQUE KEY(dt, account_email, event_unique_key)
                PARTITION BY RANGE(dt) ()
                DISTRIBUTED BY HASH(account_email) BUCKETS 16
                PROPERTIES (
                    "enable_unique_key_merge_on_write" = "true",
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

    def replace_ods_rows(self, *, biz_date: str, rows: list[dict]) -> int:
        with self._conn() as conn, conn.cursor() as cur:
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
            cur.executemany(sql, rows)
            return len(rows)

    def rebuild_dwd_for_date(self, *, biz_date: str) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {self.db}.{self.DWD_TABLE} WHERE dt = %s",
                (biz_date,),
            )
            cur.execute(
                f"""
                INSERT INTO {self.db}.{self.DWD_TABLE} (
                    dt, account_email, event_time, event_time_bj, event_unique_key,
                    request_id, model_name, project_name,
                    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, total_tokens,
                    cost_usd, billed_amount_usd, discount_percent, src_run_id
                )
                SELECT
                    dt,
                    account_email,
                    event_time,
                    event_time_bj,
                    md5(concat_ws('|',
                        account_email,
                        ifnull(request_id, ''),
                        cast(event_time as string),
                        ifnull(model_name, ''),
                        cast(ifnull(total_tokens, 0) as string),
                        cast(ifnull(cost_usd, 0) as string)
                    )) AS event_unique_key,
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
            cur.execute(
                f"SELECT COUNT(1) FROM {self.db}.{self.DWD_TABLE} WHERE dt = %s",
                (biz_date,),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def normalize_decimal_fields(self, row: dict) -> dict:
        row["cost_usd"] = _to_decimal(row.get("cost_usd"))
        row["billed_amount_usd"] = _to_decimal(row.get("billed_amount_usd"))
        row["discount_percent"] = _to_decimal(row.get("discount_percent"))
        return row

    @staticmethod
    def ensure_datetime(value: datetime) -> datetime:
        return value.replace(microsecond=0)

