"""账期净支出 MySQL 写库模块（连接池，只写汇总表）。

写入策略：
  - 只写汇总表 cursor_billing_ledger_summary，不写明细表
  - (email, billing_month) 建 UNIQUE KEY，INSERT ... ON DUPLICATE KEY UPDATE 单语句原子覆盖
  - 批量时 executemany 一次性提交，无需先 DELETE，IO 次数减半
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from decimal import Decimal
from typing import TYPE_CHECKING, Iterator, Optional

import pymysql
from dbutils.pooled_db import PooledDB

from .logger import get

if TYPE_CHECKING:
    from .billing_ledger import BillingLedgerSummary

log = get("billing_ledger_store")

# ── DDL ─────────────────────────────────────────────────────────────────────

_DDL_SUMMARY = """
CREATE TABLE IF NOT EXISTS cursor_billing_ledger_summary (
    id                 BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    email              VARCHAR(320)     NOT NULL COMMENT 'Cursor 账号邮箱',
    feishu_email       VARCHAR(320)     NOT NULL DEFAULT '' COMMENT '飞书邮箱',
    billing_month      VARCHAR(7)       NOT NULL COMMENT '账期月份 YYYY-MM',
    amount_total_usd   DECIMAL(12,4)    NOT NULL DEFAULT 0.0000 COMMENT 'Amount列合计(USD)',
    refund_total_usd   DECIMAL(12,4)    NOT NULL DEFAULT 0.0000 COMMENT 'Status退款合计(USD)',
    net_spend_usd      DECIMAL(12,4)    NOT NULL DEFAULT 0.0000 COMMENT '账期真实净支出(USD)',
    row_count          INT              NOT NULL DEFAULT 0 COMMENT '参与计算行数',
    parse_warnings     TEXT             NULL COMMENT '解析备注（分号分隔）',
    created_at         DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_email_billing_month (email, billing_month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='账期净支出汇总'
"""

# ── SQL ─────────────────────────────────────────────────────────────────────

_SQL_UPSERT_SUMMARY = """
INSERT INTO cursor_billing_ledger_summary
    (email, feishu_email, billing_month,
     amount_total_usd, refund_total_usd, net_spend_usd,
     row_count, parse_warnings)
VALUES
    (%(email)s, %(feishu_email)s, %(billing_month)s,
     %(amount_total_usd)s, %(refund_total_usd)s, %(net_spend_usd)s,
     %(row_count)s, %(parse_warnings)s)
ON DUPLICATE KEY UPDATE
    feishu_email       = VALUES(feishu_email),
    amount_total_usd   = VALUES(amount_total_usd),
    refund_total_usd   = VALUES(refund_total_usd),
    net_spend_usd      = VALUES(net_spend_usd),
    row_count          = VALUES(row_count),
    parse_warnings     = VALUES(parse_warnings),
    updated_at         = CURRENT_TIMESTAMP
"""


# ── 连接池 ───────────────────────────────────────────────────────────────────

class LedgerStore:
    """账期净支出 MySQL 写库（连接池，汇总表先删后增）。"""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        pool_max_connections: int = 8,
        pool_min_cached: int = 1,
        pool_max_cached: int = 4,
        connect_timeout_sec: int = 10,
        read_timeout_sec: int = 30,
        write_timeout_sec: int = 30,
        connect_retry_times: int = 3,
        connect_retry_backoff_sec: int = 2,
    ) -> None:
        self.host = host
        self.port = port
        self.database = database
        self.connect_retry_times = connect_retry_times
        self.connect_retry_backoff_sec = connect_retry_backoff_sec

        self._pool = PooledDB(
            creator=pymysql,
            mincached=pool_min_cached,
            maxcached=pool_max_cached,
            maxconnections=pool_max_connections,
            blocking=True,
            ping=1,
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            charset="utf8mb4",
            autocommit=False,
            cursorclass=pymysql.cursors.Cursor,
            connect_timeout=connect_timeout_sec,
            read_timeout=read_timeout_sec,
            write_timeout=write_timeout_sec,
        )
        log.info(
            f"LedgerStore 连接池初始化完成 "
            f"host={host} port={port} db={database} "
            f"max_connections={pool_max_connections}"
        )

    @contextmanager
    def _conn(self) -> Iterator[pymysql.connections.Connection]:
        conn: Optional[pymysql.connections.Connection] = None
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
                    f"LedgerStore 获取连接失败，准备重试 "
                    f"attempt={attempt}/{self.connect_retry_times} "
                    f"sleep={sleep_sec}s error={type(e).__name__}: {e}"
                )
                time.sleep(sleep_sec)

        if conn is None:
            raise RuntimeError(
                f"LedgerStore 连接池获取连接失败 "
                f"host={self.host} port={self.port} db={self.database} "
                f"error={type(last_err).__name__}: {last_err}"
            )
        try:
            yield conn
        finally:
            conn.close()

    def ensure_tables(self) -> None:
        """建表（幂等，CREATE TABLE IF NOT EXISTS）。"""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(_DDL_SUMMARY)
            conn.commit()
        log.info("LedgerStore ensure_tables 完成（cursor_billing_ledger_summary）")

    def upsert_summaries(
        self,
        summaries: list[BillingLedgerSummary],
    ) -> int:
        """覆盖写入汇总记录（INSERT ... ON DUPLICATE KEY UPDATE）。

        - UNIQUE KEY (email, billing_month) 命中时原子更新，未命中时插入
        - executemany 单次事务批量提交，任一失败整体回滚
        """
        if not summaries:
            return 0

        rows = [_summary_to_dict(s) for s in summaries]

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.executemany(_SQL_UPSERT_SUMMARY, rows)
            conn.commit()

        log.info(f"LedgerStore upsert_summaries 完成，写入 {len(rows)} 条")
        return len(rows)


# ── 工具函数 ─────────────────────────────────────────────────────────────────

def _decimal_or_zero(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _summary_to_dict(s: BillingLedgerSummary) -> dict:
    return {
        "email": s.email,
        "feishu_email": s.feishu_email or "",
        "billing_month": s.billing_month,
        "amount_total_usd": _decimal_or_zero(s.amount_total_usd),
        "refund_total_usd": _decimal_or_zero(s.refund_total_usd),
        "net_spend_usd": _decimal_or_zero(s.net_spend_usd),
        "row_count": s.row_count,
        "parse_warnings": "；".join(s.parse_warnings[:10]) if s.parse_warnings else None,
    }


# ── 单例 ─────────────────────────────────────────────────────────────────────

_instance: Optional[LedgerStore] = None


def get_ledger_store() -> LedgerStore:
    """懒加载单例，首次调用时按 SETTINGS 初始化连接池。"""
    global _instance
    if _instance is not None:
        return _instance

    from .config import SETTINGS

    _instance = LedgerStore(
        host=SETTINGS.ledger_db_host,
        port=SETTINGS.ledger_db_port,
        user=SETTINGS.ledger_db_user,
        password=SETTINGS.ledger_db_password,
        database=SETTINGS.ledger_db_name,
        pool_max_connections=SETTINGS.ledger_db_pool_max_connections,
        pool_min_cached=SETTINGS.ledger_db_pool_min_cached,
        pool_max_cached=SETTINGS.ledger_db_pool_max_cached,
        connect_timeout_sec=SETTINGS.ledger_db_connect_timeout_sec,
        read_timeout_sec=SETTINGS.ledger_db_read_timeout_sec,
        write_timeout_sec=SETTINGS.ledger_db_write_timeout_sec,
        connect_retry_times=SETTINGS.ledger_db_connect_retry_times,
        connect_retry_backoff_sec=SETTINGS.ledger_db_connect_retry_backoff_sec,
    )
    return _instance
