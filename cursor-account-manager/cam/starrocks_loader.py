"""StarRocks 装载器（MySQL 协议）。"""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterator, Optional

import pymysql
from dbutils.pooled_db import PooledDB

from .config import SETTINGS
from .logger import get


_JDBC_RE = re.compile(r"^jdbc:mysql://(?P<host>[^:/]+)(?::(?P<port>\d+))?/(?P<db>[^?]+)")
_JOB_ID_RE = re.compile(r"job_id\s*=\s*(\d+)")
log = get("starrocks")


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
        self.query_timeout_sec = max(1, int(SETTINGS.bi_sync_db_query_timeout_sec or 120))
        self.connect_timeout_sec = max(1, int(SETTINGS.bi_sync_db_connect_timeout_sec or 10))
        self.read_timeout_sec = max(1, int(SETTINGS.bi_sync_db_read_timeout_sec or self.query_timeout_sec))
        self.write_timeout_sec = max(1, int(SETTINGS.bi_sync_db_write_timeout_sec or self.query_timeout_sec))
        self.pool_min_cached = max(0, int(SETTINGS.bi_sync_db_pool_min_cached or 0))
        self.pool_max_cached = max(0, int(SETTINGS.bi_sync_db_pool_max_cached or 0))
        self.pool_max_connections = max(1, int(SETTINGS.bi_sync_db_pool_max_connections or 8))
        if self.pool_max_cached and self.pool_max_cached > self.pool_max_connections:
            self.pool_max_cached = self.pool_max_connections
        if self.pool_min_cached > self.pool_max_connections:
            self.pool_min_cached = self.pool_max_connections
        self.pool_blocking = bool(SETTINGS.bi_sync_db_pool_blocking)
        self.pool_ping = max(0, int(SETTINGS.bi_sync_db_pool_ping or 0))
        self.connect_retry_times = max(1, int(SETTINGS.bi_sync_db_connect_retry_times or 3))
        self.connect_retry_backoff_sec = max(0, int(SETTINGS.bi_sync_db_connect_retry_backoff_sec or 2))
        self._dynamic_partition_policy_checked: set[str] = set()
        self._pool = self._create_pool()
        log.info(
            "StarRocks 连接池初始化完成 "
            f"host={self.host} port={self.port} db={self.db} "
            f"min_cached={self.pool_min_cached} max_cached={self.pool_max_cached} "
            f"max_connections={self.pool_max_connections} blocking={self.pool_blocking} "
            f"ping={self.pool_ping} connect_timeout={self.connect_timeout_sec} "
            f"read_timeout={self.read_timeout_sec} write_timeout={self.write_timeout_sec}"
        )

    def _create_pool(self) -> PooledDB:
        return PooledDB(
            creator=pymysql,
            mincached=self.pool_min_cached,
            maxcached=self.pool_max_cached,
            maxconnections=self.pool_max_connections,
            blocking=self.pool_blocking,
            ping=self.pool_ping,
            host=self.host,
            port=self.port,
            user=self.username,
            password=self.password,
            database=self.db,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=pymysql.cursors.Cursor,
            connect_timeout=self.connect_timeout_sec,
            read_timeout=self.read_timeout_sec,
            write_timeout=self.write_timeout_sec,
        )

    @contextmanager
    def _conn(self) -> Iterator[pymysql.connections.Connection]:
        conn = None
        last_err: Optional[BaseException] = None
        for attempt in range(1, self.connect_retry_times + 1):
            try:
                conn = self._pool.connection()
                break
            except BaseException as e:
                last_err = e
                if attempt >= self.connect_retry_times:
                    break
                sleep_sec = self.connect_retry_backoff_sec * attempt
                log.warning(
                    "StarRocks 连接池获取连接失败，准备重试 "
                    f"host={self.host} port={self.port} attempt={attempt}/{self.connect_retry_times} "
                    f"sleep_sec={sleep_sec} error={type(e).__name__}: {e}"
                )
                if sleep_sec > 0:
                    time.sleep(sleep_sec)
        if conn is None:
            raise RuntimeError(
                "StarRocks 连接池获取连接失败 "
                f"host={self.host} port={self.port} db={self.db} "
                f"retry_times={self.connect_retry_times} error={type(last_err).__name__}: {last_err}"
            )
        try:
            yield conn
        finally:
            conn.close()

    def check_connection(self) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")

    def ensure_tables(self) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.db}.{self.ODS_TABLE} (
                    dt                      DATE            NOT NULL,
                    account_email           VARCHAR(320)    NOT NULL,
                    event_time              DATETIME        NOT NULL,
                    run_id                  VARCHAR(128)    NOT NULL,
                    feishu_email            VARCHAR(320)    NULL,
                    plan_amount             DECIMAL(10,2)   NULL,
                    kind                    VARCHAR(128)    NULL,
                    model_name              VARCHAR(65533)  NULL,
                    max_mode                VARCHAR(64)     NULL,
                    input_tokens_wo_cache_write BIGINT      NULL,
                    input_tokens_w_cache_write  BIGINT      NULL,
                    output_tokens           BIGINT          NULL,
                    total_tokens            BIGINT          NULL,
                    cost                    VARCHAR(128)    NULL,
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
                    "dynamic_partition.create_history_partition" = "true",
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

    def _partition_exists(
        self,
        *,
        conn: pymysql.connections.Connection,
        table_name: str,
        partition_name: str,
    ) -> bool:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SHOW PARTITIONS FROM {self.db}.{table_name} WHERE PartitionName = %s",
                    (partition_name,),
                )
                row = cur.fetchone()
            return row is not None
        except Exception:
            return False

    def _is_dynamic_partition_enabled(
        self,
        *,
        conn: pymysql.connections.Connection,
        table_name: str,
    ) -> bool:
        try:
            with conn.cursor() as cur:
                cur.execute(f"SHOW CREATE TABLE {self.db}.{table_name}")
                row = cur.fetchone()
            ddl = ""
            if row and len(row) >= 2:
                ddl = str(row[1] or "")
            txt = ddl.lower().replace(" ", "")
            return '"dynamic_partition.enable"="true"' in txt
        except Exception:
            return False

    def _ensure_dynamic_partition_policy(
        self,
        *,
        conn: pymysql.connections.Connection,
        table_name: str,
    ) -> None:
        """
        动态分区策略兜底：确保历史回补场景有足够窗口。
        """
        cache_key = f"{self.db}.{table_name}"
        if cache_key in self._dynamic_partition_policy_checked:
            return
        sql_full = (
            f"ALTER TABLE {self.db}.{table_name} SET ("
            "\"dynamic_partition.enable\" = \"true\", "
            "\"dynamic_partition.time_unit\" = \"DAY\", "
            "\"dynamic_partition.start\" = \"-365\", "
            "\"dynamic_partition.end\" = \"30\", "
            "\"dynamic_partition.create_history_partition\" = \"true\", "
            "\"dynamic_partition.prefix\" = \"p\", "
            "\"dynamic_partition.buckets\" = \"16\""
            ")"
        )
        sql_no_history = (
            f"ALTER TABLE {self.db}.{table_name} SET ("
            "\"dynamic_partition.enable\" = \"true\", "
            "\"dynamic_partition.time_unit\" = \"DAY\", "
            "\"dynamic_partition.start\" = \"-365\", "
            "\"dynamic_partition.end\" = \"30\", "
            "\"dynamic_partition.prefix\" = \"p\", "
            "\"dynamic_partition.buckets\" = \"16\""
            ")"
        )
        try:
            with conn.cursor() as cur:
                cur.execute(sql_full)
        except Exception as e:
            # 兼容旧版本不支持 create_history_partition
            if "create_history_partition" in str(e).lower() or "unknown properties" in str(e).lower():
                with conn.cursor() as cur:
                    cur.execute(sql_no_history)
            else:
                raise
        self._dynamic_partition_policy_checked.add(cache_key)

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
        if self._partition_exists(conn=conn, table_name=table_name, partition_name=part_name):
            return

        # 动态分区表不允许手工 ADD PARTITION。
        # 写入链路仅等待分区生成，不在运行期 ALTER TABLE 改配置，
        # 避免写入中触发表状态短暂非 NORMAL。
        if self._is_dynamic_partition_enabled(conn=conn, table_name=table_name):
            for _ in range(10):
                if self._partition_exists(conn=conn, table_name=table_name, partition_name=part_name):
                    return
                time.sleep(1)
            raise RuntimeError(
                f"{self.db}.{table_name} 缺少分区 {part_name}（biz_date={biz_date}），"
                "且动态分区等待超时。请检查 dynamic_partition.start/end 配置是否覆盖该日期。"
            )

        sql = (
            f"ALTER TABLE {self.db}.{table_name} "
            f"ADD PARTITION {part_name} "
            f"VALUES [('{day.isoformat()}'), ('{next_day.isoformat()}'))"
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

    def ensure_biz_date_partitions_ready(self, *, biz_date: str) -> None:
        """
        在任务拉取前完成 ODS 分区就绪检查。
        如果目标分区不可用，直接抛错，阻断后续拉取/写入。
        """
        with self._conn() as conn:
            self._ensure_date_partition(conn=conn, table_name=self.ODS_TABLE, biz_date=biz_date)

    def _normalize_ods_row(self, row: dict) -> dict:
        # 字符串字段保持原样（不 trim、不截断、不改写）；仅对非字符串做 str() 兜底。
        for key in (
            "run_id",
            "account_email",
            "feishu_email",
            "kind",
            "model_name",
            "max_mode",
        ):
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
        row["feishu_email"] = str(row.get("feishu_email") or "").strip().lower()
        if not row["feishu_email"]:
            raise ValueError("feishu_email 不能为空")
        row["plan_amount"] = _fit_decimal(row.get("plan_amount"), precision=10, scale=2)
        row["input_tokens_wo_cache_write"] = _to_int_or_none(row.get("input_tokens_wo_cache_write"))
        row["input_tokens_w_cache_write"] = _to_int_or_none(row.get("input_tokens_w_cache_write"))
        row["output_tokens"] = _to_int_or_none(row.get("output_tokens"))
        row["total_tokens"] = _to_int_or_none(row.get("total_tokens"))
        cost_val = row.get("cost")
        if cost_val is None:
            row["cost"] = None
        elif isinstance(cost_val, str):
            row["cost"] = cost_val
        else:
            row["cost"] = str(cost_val)
        for dt_key in ("event_time",):
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
                "dt, run_id, account_email, event_time, "
                "feishu_email, plan_amount, "
                "kind, model_name, max_mode, "
                "input_tokens_wo_cache_write, input_tokens_w_cache_write, "
                "output_tokens, total_tokens, cost, raw_event_json"
                ") VALUES ("
                "%(dt)s, %(run_id)s, %(account_email)s, %(event_time)s, "
                "%(feishu_email)s, %(plan_amount)s, "
                "%(kind)s, %(model_name)s, %(max_mode)s, "
                "%(input_tokens_wo_cache_write)s, %(input_tokens_w_cache_write)s, "
                "%(output_tokens)s, %(total_tokens)s, %(cost)s, %(raw_event_json)s"
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
                "dt, run_id, account_email, event_time, "
                "feishu_email, plan_amount, "
                "kind, model_name, max_mode, "
                "input_tokens_wo_cache_write, input_tokens_w_cache_write, "
                "output_tokens, total_tokens, cost, raw_event_json"
                ") VALUES ("
                "%(dt)s, %(run_id)s, %(account_email)s, %(event_time)s, "
                "%(feishu_email)s, %(plan_amount)s, "
                "%(kind)s, %(model_name)s, %(max_mode)s, "
                "%(input_tokens_wo_cache_write)s, %(input_tokens_w_cache_write)s, "
                "%(output_tokens)s, %(total_tokens)s, %(cost)s, %(raw_event_json)s"
                ")"
            )
            try:
                cur.executemany(sql, normalized_rows)
            except Exception as e:
                raise RuntimeError(self._build_tracking_error_message(conn, e)) from e
            return len(normalized_rows)

    def normalize_decimal_fields(self, row: dict) -> dict:
        return self._normalize_ods_row(row)

    @staticmethod
    def ensure_datetime(value: datetime) -> datetime:
        return value.replace(microsecond=0)

