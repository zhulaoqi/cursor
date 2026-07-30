"""Cursor 用量快照 MySQL 存储。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import re
import time
from typing import Iterator

import pymysql
from dbutils.pooled_db import PooledDB

from .config import SETTINGS
from .usage_snapshot_models import FinalSource, SnapshotType, UsageSnapshot


class SchemaMismatchError(RuntimeError):
    """数据库结构或主数据契约不符合要求。"""


class StaleCycleWriteError(RuntimeError):
    """拒绝写入已经过期的账期。"""


class WriteResult(str, Enum):
    """快照写入结果。"""

    INSERTED = "inserted"
    UPDATED = "updated"
    IDEMPOTENT = "idempotent"


class FinalizeStatus(str, Enum):
    """账期结算结果状态。"""

    FINALIZED = "finalized"
    MISSING_CYCLE_FINAL = "missing_cycle_final"
    IDEMPOTENT = "idempotent"


@dataclass(frozen=True)
class FinalizeResult:
    """一次账期结算结果及待调用方持久化的修复审计上下文。"""

    status: FinalizeStatus
    cycle_start: datetime
    authoritative_cycle_end: datetime | None
    final_source: FinalSource | None = None
    snapshot_id: int | None = None
    finalized_at: datetime | None = None
    audit_actor: str | None = None
    audit_reason: str | None = None


@dataclass(frozen=True)
class ReconcileResult:
    """账期协调写入及可选旧周期结算结果。"""

    write_result: WriteResult
    finalize_result: FinalizeResult | None = None


_TABLE_NAME = "cursor_usage_snapshot"
_WRITE_TRANSACTION_MAX_ATTEMPTS = 3
_RETRYABLE_MYSQL_ERROR_CODES = {1062, 1205, 1213}
_CHECK_NAMES = {
    "chk_usage_snapshot_type",
    "chk_usage_pct",
    "chk_usage_cycle",
    "chk_usage_final_state",
}
_EXPECTED_CHECK_CLAUSES = {
    "chk_usage_snapshot_type": (
        "snapshot_type IN ('periodic', 'pre_reset')"
    ),
    "chk_usage_pct": (
        "total_used_pct >= 0 AND total_used_pct <= 100"
    ),
    "chk_usage_cycle": "billing_cycle_end > billing_cycle_start",
    "chk_usage_final_state": (
        "(is_cycle_final = 0 AND final_source IS NULL "
        "AND finalized_at IS NULL) OR "
        "(is_cycle_final = 1 "
        "AND final_source IN ('pre_reset', 'periodic_fallback') "
        "AND finalized_at IS NOT NULL)"
    ),
}

_EXPECTED_COLUMNS = {
    "id": ("bigint unsigned", "NO", None, None, None, "auto_increment"),
    "email": ("varchar(320)", "NO", None, "utf8mb4", "table", ""),
    "plan_tier": ("varchar(32)", "NO", "unknown", "utf8mb4", "table", ""),
    "plan_tier_raw": ("varchar(128)", "YES", None, "utf8mb4", "table", ""),
    "plan_status": ("varchar(32)", "NO", "unknown", "utf8mb4", "table", ""),
    "plan_source": ("varchar(32)", "NO", "api", "utf8mb4", "table", ""),
    "billing_cycle_start": ("datetime(3)", "NO", None, None, None, ""),
    "billing_cycle_end": ("datetime(3)", "NO", None, None, None, ""),
    "total_used_pct": ("decimal(5,2)", "NO", None, None, None, ""),
    "snapshot_type": ("varchar(16)", "NO", None, "utf8mb4", "table", ""),
    "snapshot_slot": ("datetime(3)", "NO", None, None, None, ""),
    "collected_at": ("datetime(3)", "NO", None, None, None, ""),
    "is_cycle_final": ("tinyint(1)", "NO", "0", None, None, ""),
    "final_source": ("varchar(32)", "YES", None, "utf8mb4", "table", ""),
    "finalized_at": ("datetime(3)", "YES", None, None, None, ""),
    "source_endpoint": ("varchar(255)", "YES", None, "utf8mb4", "table", ""),
    "parser_version": ("varchar(32)", "NO", None, "utf8mb4", "table", ""),
    "raw_payload": ("json", "YES", None, None, None, ""),
    "created_at": (
        "datetime(3)",
        "NO",
        "current_timestamp(3)",
        None,
        None,
        "",
    ),
    "updated_at": (
        "datetime(3)",
        "NO",
        "current_timestamp(3)",
        None,
        None,
        "on update current_timestamp(3)",
    ),
}

_EXPECTED_INDEXES = {
    "PRIMARY": (False, ("id",)),
    "uk_usage_snapshot_slot": (
        False,
        ("email", "billing_cycle_start", "snapshot_type", "snapshot_slot"),
    ),
    "idx_usage_email_collected": (True, ("email", "collected_at")),
    "idx_usage_email_cycle_end": (True, ("email", "billing_cycle_end")),
    "idx_usage_due_scan": (
        True,
        ("billing_cycle_end", "snapshot_type", "collected_at"),
    ),
    "idx_usage_final": (
        True,
        ("email", "is_cycle_final", "billing_cycle_end"),
    ),
}

_SQL_LIST_MONITOR_ACCOUNTS = (
    "SELECT id,email,applicant,department FROM cursor_accounts "
    "WHERE email IS NOT NULL AND TRIM(email)<>''"
)

_SQL_LIST_USAGE_DASHBOARD_SNAPSHOTS = """
SELECT
    accounts.email AS email,
    accounts.applicant AS applicant,
    accounts.department AS department,
    snapshots.id AS snapshot_id,
    snapshots.plan_tier AS plan_tier,
    snapshots.plan_tier_raw AS plan_tier_raw,
    snapshots.plan_status AS plan_status,
    snapshots.plan_source AS plan_source,
    snapshots.billing_cycle_start AS billing_cycle_start,
    snapshots.billing_cycle_end AS billing_cycle_end,
    snapshots.total_used_pct AS total_used_pct,
    snapshots.snapshot_type AS snapshot_type,
    snapshots.snapshot_slot AS snapshot_slot,
    snapshots.collected_at AS collected_at,
    snapshots.is_cycle_final AS is_cycle_final,
    snapshots.final_source AS final_source,
    snapshots.finalized_at AS finalized_at,
    snapshots.source_endpoint AS source_endpoint,
    snapshots.parser_version AS parser_version,
    snapshots.raw_payload AS raw_payload,
    snapshots.created_at AS created_at,
    snapshots.updated_at AS updated_at
FROM cursor_accounts AS accounts
LEFT JOIN cursor_usage_snapshot AS snapshots
  ON snapshots.email = accounts.email
WHERE accounts.email IS NOT NULL AND TRIM(accounts.email) <> ''
ORDER BY accounts.email ASC, snapshots.collected_at ASC, snapshots.id ASC
"""

_SQL_SELECT_UNIQUE_FOR_UPDATE = """
SELECT id, collected_at
FROM cursor_usage_snapshot
WHERE email = %(email)s
  AND billing_cycle_start = %(billing_cycle_start)s
  AND snapshot_type = %(snapshot_type)s
  AND snapshot_slot = %(snapshot_slot)s
FOR UPDATE
"""

_SQL_LOCK_MONITOR_ACCOUNT = """
SELECT id
FROM cursor_accounts
WHERE email = %s
FOR UPDATE
"""

_SQL_INSERT = """
INSERT INTO cursor_usage_snapshot (
    email, plan_tier, plan_tier_raw, plan_status, plan_source,
    billing_cycle_start, billing_cycle_end, total_used_pct,
    snapshot_type, snapshot_slot, collected_at,
    is_cycle_final, final_source, finalized_at,
    source_endpoint, parser_version, raw_payload
) VALUES (
    %(email)s, %(plan_tier)s, %(plan_tier_raw)s, %(plan_status)s,
    %(plan_source)s, %(billing_cycle_start)s, %(billing_cycle_end)s,
    %(total_used_pct)s, %(snapshot_type)s, %(snapshot_slot)s,
    %(collected_at)s, %(is_cycle_final)s, %(final_source)s,
    %(finalized_at)s, %(source_endpoint)s, %(parser_version)s,
    %(raw_payload)s
)
"""

_SQL_UPDATE = """
UPDATE cursor_usage_snapshot
SET plan_tier = %(plan_tier)s,
    plan_tier_raw = %(plan_tier_raw)s,
    plan_status = %(plan_status)s,
    plan_source = %(plan_source)s,
    billing_cycle_end = %(billing_cycle_end)s,
    total_used_pct = %(total_used_pct)s,
    snapshot_slot = %(snapshot_slot)s,
    collected_at = %(collected_at)s,
    source_endpoint = %(source_endpoint)s,
    parser_version = %(parser_version)s,
    raw_payload = %(raw_payload)s
WHERE id = %(id)s
"""

_SQL_SELECT_LATEST_CYCLE_FOR_UPDATE = """
SELECT billing_cycle_start
FROM cursor_usage_snapshot
WHERE email = %(email)s
ORDER BY billing_cycle_start DESC, id DESC
LIMIT 1
FOR UPDATE
"""

_SQL_SELECT_CYCLE_FOR_UPDATE = """
SELECT id, snapshot_type, billing_cycle_end, collected_at,
       is_cycle_final, final_source, finalized_at
FROM cursor_usage_snapshot
WHERE email = %(email)s
  AND billing_cycle_start = %(billing_cycle_start)s
ORDER BY collected_at DESC, id DESC
FOR UPDATE
"""

_SQL_RESET_CYCLE_FINAL = """
UPDATE cursor_usage_snapshot
SET is_cycle_final = 0,
    final_source = NULL,
    finalized_at = NULL
WHERE email = %(email)s
  AND billing_cycle_start = %(billing_cycle_start)s
"""

_SQL_MARK_CYCLE_FINAL = """
UPDATE cursor_usage_snapshot
SET billing_cycle_end = %(authoritative_cycle_end)s,
    is_cycle_final = 1,
    final_source = %(final_source)s,
    finalized_at = %(finalized_at)s
WHERE id = %(id)s
"""

_SQL_LIST_FINAL_CYCLES = """
SELECT *
FROM cursor_usage_snapshot
WHERE email = %(email)s
  AND is_cycle_final = 1
ORDER BY billing_cycle_end DESC
"""


def _ddl(email_collation: str) -> str:
    """按主数据邮箱排序规则生成快照表 DDL。"""
    if not re.fullmatch(r"[A-Za-z0-9_]+", email_collation):
        raise SchemaMismatchError("cursor_accounts.email collation 名称非法")
    return f"""
CREATE TABLE IF NOT EXISTS cursor_usage_snapshot (
    id                   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    email                VARCHAR(320) NOT NULL COMMENT '规范化 Cursor 账号邮箱',
    plan_tier            VARCHAR(32) NOT NULL DEFAULT 'unknown'
                           COMMENT 'pro/pro_plus/ultra/free/unknown...',
    plan_tier_raw        VARCHAR(128) NULL COMMENT 'Cursor 返回的原始套餐名',
    plan_status          VARCHAR(32) NOT NULL DEFAULT 'unknown',
    plan_source          VARCHAR(32) NOT NULL DEFAULT 'api'
                           COMMENT 'api/stripe/spending_page/unknown',
    billing_cycle_start  DATETIME(3) NOT NULL COMMENT 'UTC，无时区 DATETIME',
    billing_cycle_end    DATETIME(3) NOT NULL COMMENT 'UTC，无时区 DATETIME',
    total_used_pct       DECIMAL(5,2) NOT NULL COMMENT '0.00~100.00',
    snapshot_type        VARCHAR(16) NOT NULL COMMENT 'periodic/pre_reset',
    snapshot_slot        DATETIME(3) NOT NULL
                           COMMENT '幂等时间槽；periodic 为频率槽，pre_reset 为 cycle_start',
    collected_at         DATETIME(3) NOT NULL COMMENT '实际成功采集时间 UTC',
    is_cycle_final       TINYINT(1) NOT NULL DEFAULT 0,
    final_source         VARCHAR(32) NULL COMMENT 'pre_reset/periodic_fallback',
    finalized_at         DATETIME(3) NULL COMMENT '确认账期切换并结算的时间 UTC',
    source_endpoint      VARCHAR(255) NULL,
    parser_version       VARCHAR(32) NOT NULL,
    raw_payload          JSON NULL,
    created_at           DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at           DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                           ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY uk_usage_snapshot_slot (
        email, billing_cycle_start, snapshot_type, snapshot_slot
    ),
    KEY idx_usage_email_collected (email, collected_at),
    KEY idx_usage_email_cycle_end (email, billing_cycle_end),
    KEY idx_usage_due_scan (billing_cycle_end, snapshot_type, collected_at),
    KEY idx_usage_final (email, is_cycle_final, billing_cycle_end),
    CONSTRAINT chk_usage_snapshot_type
        CHECK (snapshot_type IN ('periodic', 'pre_reset')),
    CONSTRAINT chk_usage_pct
        CHECK (total_used_pct >= 0 AND total_used_pct <= 100),
    CONSTRAINT chk_usage_cycle
        CHECK (billing_cycle_end > billing_cycle_start),
    CONSTRAINT chk_usage_final_state
        CHECK (
            (is_cycle_final = 0 AND final_source IS NULL AND finalized_at IS NULL)
            OR
            (is_cycle_final = 1
             AND final_source IN ('pre_reset', 'periodic_fallback')
             AND finalized_at IS NOT NULL)
        )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COLLATE={email_collation}
  COMMENT='Cursor 账号订阅档位与账期用量时序快照'
"""


def _normalize_default(value: object) -> object:
    """统一 information_schema 返回的默认值格式。"""
    if value is None:
        return None
    return str(value).strip().lower()


def _normalize_extra(value: object) -> str:
    """统一 information_schema 返回的 EXTRA 格式。"""
    tokens = str(value or "").strip().lower().split()
    return " ".join(token for token in tokens if token != "default_generated")


def _server_requires_check_validation(version: object) -> bool:
    """解析完整服务端版本并判断是否必须校验 CHECK。"""
    version_text = str(version or "").strip()
    if "mariadb" in version_text.lower():
        raise SchemaMismatchError("当前未验证 MariaDB，不允许启动")
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version_text)
    if match is None:
        raise SchemaMismatchError(f"无法解析 MySQL 版本：{version_text}")
    parsed = tuple(int(part) for part in match.groups())
    if parsed[:2] == (5, 7):
        return False
    if parsed[:2] == (8, 0) and parsed < (8, 0, 16):
        raise SchemaMismatchError(
            "MySQL 8.0.0~8.0.15 不强制 CHECK，要求至少 8.0.16"
        )
    if parsed >= (8, 0, 16):
        return True
    raise SchemaMismatchError(f"不支持的 MySQL 版本：{version_text}")


def _normalize_check_clause(value: object) -> str:
    """统一 MySQL 对 CHECK 表达式的引号、空白和括号格式。"""
    normalized = str(value or "").lower().replace("`", "")
    normalized = re.sub(r"_[a-z0-9]+(?=')", "", normalized)
    return re.sub(r"[\s()]+", "", normalized)


def _naive_utc_millis(value: datetime) -> datetime:
    """将有时区 UTC 时间转为无时区 DATETIME(3)。"""
    if not isinstance(value, datetime):
        raise ValueError("时间参数必须是 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间参数必须是有时区时间")
    utc_value = value.astimezone(timezone.utc)
    return utc_value.replace(
        tzinfo=None,
        microsecond=(utc_value.microsecond // 1000) * 1000,
    )


def _validate_email(email: str) -> None:
    """要求查询邮箱已去空白、转小写且非空。"""
    if not isinstance(email, str) or not email.strip():
        raise ValueError("email 去除首尾空白后不能为空")
    if email != email.strip():
        raise ValueError("email 必须已经去除首尾空白")
    if email != email.lower():
        raise ValueError("email 必须已经规范化为小写")


def _row_value(row: dict[str, object], key: str) -> object:
    """从 DictCursor 行中按不区分大小写的字段名取值。"""
    if key in row:
        return row[key]
    lower_key = key.lower()
    for candidate, value in row.items():
        if str(candidate).lower() == lower_key:
            return value
    return None


def _is_retryable_write_error(exc: BaseException) -> bool:
    """判断异常是否属于可安全重放完整事务的 MySQL 错误。"""
    if not isinstance(exc, pymysql.err.MySQLError) or not exc.args:
        return False
    try:
        error_code = int(exc.args[0])
    except (TypeError, ValueError):
        return False
    return error_code in _RETRYABLE_MYSQL_ERROR_CODES


class UsageSnapshotStore:
    """用量快照 MySQL 存储，支持注入连接池测试。"""

    def __init__(self, settings=SETTINGS, pool=None) -> None:
        self.settings = settings
        self.database = getattr(settings, "ledger_db_name", "")
        self.connect_retry_times = max(
            1,
            int(getattr(settings, "ledger_db_connect_retry_times", 3)),
        )
        self.connect_retry_backoff_sec = max(
            0,
            int(getattr(settings, "ledger_db_connect_retry_backoff_sec", 2)),
        )
        self._pool = pool if pool is not None else self._create_pool()

    def _create_pool(self):
        """使用 Ledger 数据库配置创建连接池。"""
        return PooledDB(
            creator=pymysql,
            mincached=self.settings.ledger_db_pool_min_cached,
            maxcached=self.settings.ledger_db_pool_max_cached,
            maxconnections=self.settings.ledger_db_pool_max_connections,
            blocking=True,
            ping=1,
            host=self.settings.ledger_db_host,
            port=self.settings.ledger_db_port,
            user=self.settings.ledger_db_user,
            password=self.settings.ledger_db_password,
            database=self.settings.ledger_db_name,
            charset="utf8mb4",
            autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=self.settings.ledger_db_connect_timeout_sec,
            read_timeout=self.settings.ledger_db_read_timeout_sec,
            write_timeout=self.settings.ledger_db_write_timeout_sec,
        )

    @contextmanager
    def _connection(self) -> Iterator[object]:
        """借用连接并确保归还连接池。"""
        connection = None
        last_error = None
        for attempt in range(1, self.connect_retry_times + 1):
            try:
                connection = self._pool.connection()
                break
            except Exception as exc:
                last_error = exc
                if attempt < self.connect_retry_times:
                    time.sleep(self.connect_retry_backoff_sec * attempt)
        if connection is None:
            raise RuntimeError(
                "UsageSnapshotStore 获取数据库连接失败"
                f" host={getattr(self.settings, 'ledger_db_host', '')}"
                f" port={getattr(self.settings, 'ledger_db_port', '')}"
                f" db={self.database}"
                f" error={type(last_error).__name__}"
            )
        try:
            yield connection
        finally:
            connection.close()

    def _email_metadata(
        self,
        cursor,
        table_name: str,
    ) -> dict[str, object]:
        """读取指定表 email 字段的字符集和排序规则。"""
        cursor.execute(
            """
SELECT CHARACTER_SET_NAME, COLLATION_NAME
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = 'email'
""",
            (self.database, table_name),
        )
        row = cursor.fetchone()
        if row is None:
            raise SchemaMismatchError(f"{table_name}.email 不存在")
        return {
            "charset": _row_value(row, "CHARACTER_SET_NAME"),
            "collation": _row_value(row, "COLLATION_NAME"),
        }

    def _validate_collation_catalog(self, cursor, collation: str) -> None:
        """确认动态 collation 存在且属于 utf8mb4。"""
        if not re.fullmatch(r"[A-Za-z0-9_]+", collation):
            raise SchemaMismatchError(
                "cursor_accounts.email collation 名称非法"
            )
        cursor.execute(
            """
SELECT COLLATION_NAME, CHARACTER_SET_NAME
FROM information_schema.COLLATIONS
WHERE COLLATION_NAME = %s
""",
            (collation,),
        )
        row = cursor.fetchone()
        if row is None:
            raise SchemaMismatchError(
                f"information_schema 中不存在 collation：{collation}"
            )
        if (
            _row_value(row, "COLLATION_NAME") != collation
            or _row_value(row, "CHARACTER_SET_NAME") != "utf8mb4"
        ):
            raise SchemaMismatchError(
                f"collation {collation} 必须属于 utf8mb4"
            )

    def ensure_schema(self) -> None:
        """同一连接内完成主数据预检、建表和结构校验。"""
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                email_meta = self._validate_monitor_contract_with_cursor(
                    cursor,
                    include_snapshot=False,
                )
                self._validate_collation_catalog(
                    cursor,
                    str(email_meta["collation"]),
                )
                cursor.execute(_ddl(str(email_meta["collation"])))
                connection.commit()
                self._validate_schema_with_cursor(cursor)
                snapshot_email = self._email_metadata(cursor, _TABLE_NAME)
                if snapshot_email != email_meta:
                    raise SchemaMismatchError(
                        "cursor_accounts、Ledger 与快照表 email 的 "
                        "charset/collation 不一致"
                    )
            except Exception:
                connection.rollback()
                raise
            finally:
                cursor.close()

    def validate_schema(self) -> None:
        """逐项验证快照表结构，不执行自动 ALTER。"""
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                self._validate_schema_with_cursor(cursor)
            finally:
                cursor.close()

    def _validate_schema_with_cursor(self, cursor) -> None:
        """使用现有游标逐项验证快照表结构。"""
        cursor.execute("SELECT VERSION() AS version")
        version_row = cursor.fetchone()
        version = _row_value(version_row, "version")
        requires_check_validation = _server_requires_check_validation(version)

        cursor.execute(
            """
SELECT ENGINE, TABLE_COLLATION
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
""",
            (self.database, _TABLE_NAME),
        )
        table_row = cursor.fetchone()
        if table_row is None:
            raise SchemaMismatchError(f"{_TABLE_NAME} 表不存在")
        engine = str(_row_value(table_row, "ENGINE") or "")
        table_collation = str(
            _row_value(table_row, "TABLE_COLLATION") or ""
        )
        if engine.lower() != "innodb":
            raise SchemaMismatchError(
                f"{_TABLE_NAME} 引擎不符，要求 InnoDB"
            )
        if not table_collation.startswith("utf8mb4_"):
            raise SchemaMismatchError(
                f"{_TABLE_NAME} charset/collation 不符"
            )

        cursor.execute(
            """
SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT,
       CHARACTER_SET_NAME, COLLATION_NAME, EXTRA
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
ORDER BY ORDINAL_POSITION
""",
            (self.database, _TABLE_NAME),
        )
        self._validate_columns(cursor.fetchall(), table_collation)

        cursor.execute(
            """
SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
ORDER BY INDEX_NAME, SEQ_IN_INDEX
""",
            (self.database, _TABLE_NAME),
        )
        self._validate_indexes(cursor.fetchall())

        if requires_check_validation:
            cursor.execute(
                """
SELECT cc.CONSTRAINT_NAME, cc.CHECK_CLAUSE, tc.ENFORCED
FROM information_schema.CHECK_CONSTRAINTS cc
JOIN information_schema.TABLE_CONSTRAINTS tc
  ON tc.CONSTRAINT_SCHEMA = cc.CONSTRAINT_SCHEMA
 AND tc.CONSTRAINT_NAME = cc.CONSTRAINT_NAME
WHERE tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s
""",
                (self.database, _TABLE_NAME),
            )
            check_rows = cursor.fetchall()
            found_checks = {
                str(_row_value(row, "CONSTRAINT_NAME"))
                for row in check_rows
            }
            missing_checks = _CHECK_NAMES - found_checks
            if missing_checks:
                raise SchemaMismatchError(
                    "缺少 CHECK 约束："
                    + ", ".join(sorted(missing_checks))
                )
            enforced_states = {
                str(_row_value(row, "CONSTRAINT_NAME")):
                str(_row_value(row, "ENFORCED") or "").upper()
                for row in check_rows
            }
            for name in sorted(_CHECK_NAMES):
                if enforced_states[name] != "YES":
                    raise SchemaMismatchError(
                        f"CHECK 约束 {name} 的 ENFORCED 必须为 YES"
                    )
            clauses = {
                str(_row_value(row, "CONSTRAINT_NAME")):
                _normalize_check_clause(
                    _row_value(row, "CHECK_CLAUSE")
                )
                for row in check_rows
            }
            for name, expected_clause in _EXPECTED_CHECK_CLAUSES.items():
                if clauses[name] != _normalize_check_clause(expected_clause):
                    raise SchemaMismatchError(
                        f"CHECK 约束 {name} 定义不符"
                    )

    def _validate_columns(
        self,
        rows: list[dict[str, object]],
        table_collation: str,
    ) -> None:
        """校验字段集合、类型、NULL、默认值、字符集和精度。"""
        actual = {
            str(_row_value(row, "COLUMN_NAME")): row
            for row in rows
        }
        if set(actual) != set(_EXPECTED_COLUMNS):
            missing = sorted(set(_EXPECTED_COLUMNS) - set(actual))
            extra = sorted(set(actual) - set(_EXPECTED_COLUMNS))
            raise SchemaMismatchError(
                f"快照字段集合不符，缺少={missing}，多余={extra}"
            )
        for name, expected in _EXPECTED_COLUMNS.items():
            row = actual[name]
            expected_type, nullable, default, charset, collation, extra = expected
            actual_charset = _row_value(row, "CHARACTER_SET_NAME")
            actual_collation = _row_value(row, "COLLATION_NAME")
            if (
                name == "raw_payload"
                and actual_charset == "utf8mb4"
                and actual_collation == "utf8mb4_bin"
            ):
                actual_charset = None
                actual_collation = None
            actual_values = (
                str(_row_value(row, "COLUMN_TYPE") or "").lower(),
                str(_row_value(row, "IS_NULLABLE") or "").upper(),
                _normalize_default(_row_value(row, "COLUMN_DEFAULT")),
                actual_charset,
                actual_collation,
                _normalize_extra(_row_value(row, "EXTRA")),
            )
            expected_values = (
                expected_type,
                nullable,
                _normalize_default(default),
                charset,
                table_collation if collation == "table" else collation,
                extra,
            )
            if actual_values != expected_values:
                raise SchemaMismatchError(
                    f"字段 {name} 类型/NULL/默认值/charset/collation "
                    f"结构不符，期望={expected_values}，"
                    f"实际={actual_values}"
                )

    def _validate_indexes(self, rows: list[dict[str, object]]) -> None:
        """校验唯一键和普通索引的列顺序。"""
        grouped: dict[str, dict] = {}
        for row in rows:
            name = str(_row_value(row, "INDEX_NAME"))
            grouped.setdefault(
                name,
                {
                    "non_unique": bool(
                        int(_row_value(row, "NON_UNIQUE") or 0)
                    ),
                    "columns": [],
                },
            )
            grouped[name]["columns"].append(
                (
                    int(_row_value(row, "SEQ_IN_INDEX")),
                    str(_row_value(row, "COLUMN_NAME")),
                )
            )
        for name, (non_unique, columns) in _EXPECTED_INDEXES.items():
            index = grouped.get(name)
            actual_columns = (
                tuple(
                    column
                    for _, column in sorted(index["columns"])
                )
                if index
                else ()
            )
            if (
                index is None
                or index["non_unique"] != non_unique
                or actual_columns != columns
            ):
                raise SchemaMismatchError(
                    f"索引 {name} 结构不符，期望列={columns}"
                )

    def validate_monitor_contract(self) -> None:
        """验证监控主数据和 Ledger 的启动契约。"""
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                self._validate_monitor_contract_with_cursor(
                    cursor,
                    include_snapshot=True,
                )
            finally:
                cursor.close()

    def _validate_monitor_contract_with_cursor(
        self,
        cursor,
        *,
        include_snapshot: bool,
    ) -> dict[str, object]:
        """使用现有游标验证主数据契约并返回账号邮箱元数据。"""
        account_columns = self._column_names(cursor, "cursor_accounts")
        required_accounts = {"id", "email", "applicant", "department"}
        missing_accounts = required_accounts - account_columns
        if missing_accounts:
            raise SchemaMismatchError(
                "cursor_accounts 缺少字段："
                + ", ".join(sorted(missing_accounts))
            )

        cursor.execute(
            """
SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
ORDER BY INDEX_NAME, SEQ_IN_INDEX
""",
            (self.database, "cursor_accounts"),
        )
        if not self._has_raw_email_unique_index(cursor.fetchall()):
            raise SchemaMismatchError(
                "cursor_accounts.email 缺少原始单列唯一索引"
            )

        cursor.execute(
            """
SELECT LOWER(TRIM(email)) AS normalized_email, COUNT(*) AS count
FROM cursor_accounts
WHERE email IS NOT NULL AND TRIM(email) <> ''
GROUP BY LOWER(TRIM(email))
HAVING COUNT(*) > 1
LIMIT 1
"""
        )
        if cursor.fetchone() is not None:
            raise SchemaMismatchError(
                "cursor_accounts 规范化 email 存在重复"
            )

        ledger_columns = self._column_names(
            cursor,
            "cursor_billing_ledger_summary",
        )
        required_ledger = {"email", "billing_month", "net_spend_usd"}
        missing_ledger = required_ledger - ledger_columns
        if missing_ledger:
            raise SchemaMismatchError(
                "cursor_billing_ledger_summary 缺少字段："
                + ", ".join(sorted(missing_ledger))
            )

        account_email = self._email_metadata(cursor, "cursor_accounts")
        ledger_email = self._email_metadata(
            cursor,
            "cursor_billing_ledger_summary",
        )
        signatures = {
            (item["charset"], item["collation"])
            for item in (account_email, ledger_email)
        }
        if len(signatures) != 1:
            raise SchemaMismatchError(
                "cursor_accounts 与 Ledger email 的 "
                "charset/collation 不一致"
            )
        if account_email["charset"] != "utf8mb4":
            raise SchemaMismatchError(
                "cursor_accounts.email 字符集必须为 utf8mb4"
            )
        if include_snapshot:
            snapshot_email = self._email_metadata(cursor, _TABLE_NAME)
            if snapshot_email != account_email:
                raise SchemaMismatchError(
                    "cursor_accounts、Ledger 与快照表 email 的 "
                    "charset/collation 不一致"
                )
        return account_email

    def _column_names(self, cursor, table_name: str) -> set[str]:
        """读取表字段名。"""
        cursor.execute(
            """
SELECT COLUMN_NAME
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
""",
            (self.database, table_name),
        )
        return {
            str(_row_value(row, "COLUMN_NAME"))
            for row in cursor.fetchall()
        }

    @staticmethod
    def _has_raw_email_unique_index(
        rows: list[dict[str, object]],
    ) -> bool:
        """判断是否存在只包含原始 email 的唯一索引。"""
        indexes: dict[str, dict] = {}
        for row in rows:
            name = str(_row_value(row, "INDEX_NAME"))
            entry = indexes.setdefault(
                name,
                {
                    "non_unique": int(
                        _row_value(row, "NON_UNIQUE") or 0
                    ),
                    "columns": [],
                },
            )
            entry["columns"].append(
                (
                    int(_row_value(row, "SEQ_IN_INDEX")),
                    str(_row_value(row, "COLUMN_NAME")),
                )
            )
        return any(
            entry["non_unique"] == 0
            and tuple(column for _, column in sorted(entry["columns"]))
            == ("email",)
            for entry in indexes.values()
        )

    def list_monitor_accounts(self) -> list[dict]:
        """按固定查询返回所有非空邮箱主数据。"""
        return self._fetch_all(_SQL_LIST_MONITOR_ACCOUNTS)

    def get_monitor_account(self, email: str) -> dict | None:
        """按规范化邮箱返回一条主数据；不存在时返回 None。"""
        _validate_email(email)
        return self._fetch_one(
            """
SELECT email, applicant, department
FROM cursor_accounts
WHERE email = %(email)s
LIMIT 1
""",
            {"email": email.strip().lower()},
        )

    def list_usage_dashboard_snapshots(self) -> list[dict]:
        """返回看板所需主数据及全部快照，保留尚无快照的账号。"""
        return self._fetch_all(_SQL_LIST_USAGE_DASHBOARD_SNAPSHOTS)

    def get_latest_cycle(self, email: str) -> dict | None:
        """返回账号最新账期。"""
        _validate_email(email)
        return self._fetch_one(
            """
SELECT email, plan_tier, billing_cycle_start, billing_cycle_end
FROM cursor_usage_snapshot
WHERE email = %(email)s
ORDER BY billing_cycle_start DESC, collected_at DESC, id DESC
LIMIT 1
""",
            {"email": email},
        )

    def get_latest_snapshot(self, email: str) -> dict | None:
        """返回账号最新成功快照。"""
        _validate_email(email)
        return self._fetch_one(
            """
SELECT *
FROM cursor_usage_snapshot
WHERE email = %(email)s
ORDER BY collected_at DESC, id DESC
LIMIT 1
""",
            {"email": email},
        )

    def has_periodic_slot(self, email: str, slot: datetime) -> bool:
        """判断账号指定 periodic 时间槽是否已写入。"""
        _validate_email(email)
        row = self._fetch_one(
            """
SELECT COUNT(*) AS count
FROM cursor_usage_snapshot
WHERE email = %(email)s
  AND snapshot_type = 'periodic'
  AND snapshot_slot = %(snapshot_slot)s
""",
            {
                "email": email,
                "snapshot_slot": _naive_utc_millis(slot),
            },
        )
        return bool(row and int(_row_value(row, "count") or 0))

    def has_pre_reset_slot(self, email: str, cycle_start: datetime) -> bool:
        """判断账号账期是否已有 pre-reset 最终快照。"""
        _validate_email(email)
        row = self._fetch_one(
            """
SELECT COUNT(*) AS count
FROM cursor_usage_snapshot
WHERE email = %(email)s
  AND billing_cycle_start = %(billing_cycle_start)s
  AND snapshot_type = 'pre_reset'
""",
            {
                "email": email,
                "billing_cycle_start": _naive_utc_millis(cycle_start),
            },
        )
        return bool(row and int(_row_value(row, "count") or 0))

    def _fetch_one(self, sql: str, params=None) -> dict | None:
        """执行只读查询并返回一行。"""
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, params)
                return cursor.fetchone()
            finally:
                cursor.close()

    def _fetch_all(self, sql: str, params=None) -> list[dict]:
        """执行只读查询并返回全部行。"""
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, params)
                return list(cursor.fetchall())
            finally:
                cursor.close()

    def list_final_cycles(self, email: str) -> list[dict]:
        """按账期结束时间倒序返回账号最终记录。"""
        _validate_email(email)
        return self._fetch_all(
            _SQL_LIST_FINAL_CYCLES,
            {"email": email},
        )

    def upsert_same_cycle(self, snapshot: UsageSnapshot) -> WriteResult:
        """兼容接口；统一委托协调写入以拒绝迟到周期。"""
        return self.reconcile_and_write(snapshot).write_result

    def reconcile_and_write(
        self,
        snapshot: UsageSnapshot,
    ) -> ReconcileResult:
        """原子协调账期切换、旧周期结算和新快照写入。"""
        self._validate_snapshot_for_write(snapshot)
        incoming_start = _naive_utc_millis(
            snapshot.billing_cycle_start
        )

        def operation(cursor):
            self._lock_monitor_account_with_cursor(
                cursor,
                snapshot.email,
            )
            cursor.execute(
                _SQL_SELECT_LATEST_CYCLE_FOR_UPDATE,
                {"email": snapshot.email},
            )
            latest = cursor.fetchone()
            finalize_result = None
            if latest is not None:
                latest_start = _row_value(
                    latest,
                    "billing_cycle_start",
                )
                if incoming_start < latest_start:
                    raise StaleCycleWriteError(
                        "拒绝写入早于最新已知账期的快照"
                    )
                if incoming_start > latest_start:
                    finalize_result = self._finalize_cycle_with_cursor(
                        cursor,
                        email=snapshot.email,
                        cycle_start=latest_start,
                        require_existing=True,
                    )
            write_result = self._upsert_snapshot_with_cursor(
                cursor,
                snapshot,
            )
            return ReconcileResult(
                write_result=write_result,
                finalize_result=finalize_result,
            )

        return self._run_write_transaction(operation)

    def repair_finalize_cycle(
        self,
        email: str,
        cycle_start: datetime,
        *,
        actor: str,
        reason: str,
    ) -> FinalizeResult:
        """幂等修复账期；调用方必须将返回审计上下文写入日志。"""
        _validate_email(email)
        if (
            not isinstance(cycle_start, datetime)
            or cycle_start.tzinfo is None
            or cycle_start.utcoffset() != timedelta(0)
        ):
            raise ValueError("cycle_start 必须是有时区的 UTC 时间")
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor 不能为空")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 不能为空")
        normalized_start = _naive_utc_millis(cycle_start)
        def operation(cursor):
            self._lock_monitor_account_with_cursor(cursor, email)
            return self._finalize_cycle_with_cursor(
                cursor,
                email=email,
                cycle_start=normalized_start,
                require_existing=True,
            )
        result = self._run_write_transaction(operation)
        return replace(
            result,
            audit_actor=actor,
            audit_reason=reason,
        )

    @staticmethod
    def _lock_monitor_account_with_cursor(cursor, email: str) -> int:
        """锁定主数据账号行，使同邮箱写事务不依赖 gap lock 串行。"""
        cursor.execute(_SQL_LOCK_MONITOR_ACCOUNT, (email,))
        row = cursor.fetchone()
        if row is None:
            raise ValueError("主数据账号不存在，拒绝写入用量快照")
        return int(_row_value(row, "id"))

    @staticmethod
    def _validate_snapshot_for_write(snapshot: UsageSnapshot) -> None:
        """校验普通写入和协调写入共用的快照约束。"""
        if not isinstance(snapshot, UsageSnapshot):
            raise ValueError("snapshot 必须是 UsageSnapshot")
        if (
            snapshot.snapshot_type is SnapshotType.PRE_RESET
            and snapshot.snapshot_slot != snapshot.billing_cycle_start
        ):
            raise ValueError(
                "pre_reset 的 snapshot_slot 必须等于 billing_cycle_start"
            )

    def _run_write_transaction(self, operation):
        """对指定写事务执行至多三次完整重放。"""
        for attempt in range(1, _WRITE_TRANSACTION_MAX_ATTEMPTS + 1):
            retry = False
            with self._connection() as connection:
                cursor = connection.cursor()
                try:
                    result = operation(cursor)
                    connection.commit()
                    return result
                except Exception as exc:
                    connection.rollback()
                    retry = (
                        _is_retryable_write_error(exc)
                        and attempt < _WRITE_TRANSACTION_MAX_ATTEMPTS
                    )
                    if not retry:
                        raise
                finally:
                    cursor.close()
            if retry:
                continue
        raise AssertionError("事务重试循环不应到达此处")

    def _upsert_snapshot_with_cursor(
        self,
        cursor,
        snapshot: UsageSnapshot,
    ) -> WriteResult:
        """在现有事务内按唯一键写入同周期快照。"""
        params = self._snapshot_params(snapshot)
        cursor.execute(_SQL_SELECT_UNIQUE_FOR_UPDATE, params)
        stored = cursor.fetchone()
        if stored is None:
            cursor.execute(_SQL_INSERT, params)
            return WriteResult.INSERTED
        stored_collected_at = _row_value(stored, "collected_at")
        if params["collected_at"] <= stored_collected_at:
            return WriteResult.IDEMPOTENT
        params["id"] = _row_value(stored, "id")
        cursor.execute(_SQL_UPDATE, params)
        return WriteResult.UPDATED

    def _finalize_cycle_with_cursor(
        self,
        cursor,
        *,
        email: str,
        cycle_start: datetime,
        require_existing: bool,
    ) -> FinalizeResult:
        """在现有事务内选择并标记一个账期最终记录。"""
        params = {
            "email": email,
            "billing_cycle_start": cycle_start,
        }
        cursor.execute(_SQL_SELECT_CYCLE_FOR_UPDATE, params)
        rows = list(cursor.fetchall())
        if not rows:
            if require_existing:
                raise ValueError("指定 email/cycle_start 的账期不存在")
            return FinalizeResult(
                status=FinalizeStatus.MISSING_CYCLE_FINAL,
                cycle_start=cycle_start,
                authoritative_cycle_end=None,
            )

        authoritative_cycle_end = _row_value(
            rows[0],
            "billing_cycle_end",
        )
        eligible = [
            row
            for row in rows
            if _row_value(row, "collected_at")
            <= authoritative_cycle_end
        ]
        selected = next(
            (
                row
                for row in eligible
                if _row_value(row, "snapshot_type")
                == SnapshotType.PRE_RESET.value
            ),
            None,
        )
        final_source = FinalSource.PRE_RESET if selected else None
        if selected is None:
            selected = next(
                (
                    row
                    for row in eligible
                    if _row_value(row, "snapshot_type")
                    == SnapshotType.PERIODIC.value
                ),
                None,
            )
            if selected is not None:
                final_source = FinalSource.PERIODIC_FALLBACK

        if selected is None:
            cursor.execute(_SQL_RESET_CYCLE_FINAL, params)
            return FinalizeResult(
                status=FinalizeStatus.MISSING_CYCLE_FINAL,
                cycle_start=cycle_start,
                authoritative_cycle_end=authoritative_cycle_end,
            )

        final_rows = [
            row
            for row in rows
            if int(_row_value(row, "is_cycle_final") or 0) == 1
        ]
        if (
            len(final_rows) == 1
            and _row_value(final_rows[0], "id")
            == _row_value(selected, "id")
            and _row_value(final_rows[0], "final_source")
            == final_source.value
            and _row_value(final_rows[0], "billing_cycle_end")
            == authoritative_cycle_end
            and _row_value(final_rows[0], "finalized_at") is not None
        ):
            return FinalizeResult(
                status=FinalizeStatus.IDEMPOTENT,
                cycle_start=cycle_start,
                authoritative_cycle_end=authoritative_cycle_end,
                final_source=final_source,
                snapshot_id=int(_row_value(selected, "id")),
                finalized_at=_row_value(selected, "finalized_at"),
            )

        cursor.execute(_SQL_RESET_CYCLE_FINAL, params)
        finalized_at = _naive_utc_millis(datetime.now(timezone.utc))
        mark_params = {
            "id": _row_value(selected, "id"),
            "authoritative_cycle_end": authoritative_cycle_end,
            "final_source": final_source.value,
            "finalized_at": finalized_at,
        }
        cursor.execute(_SQL_MARK_CYCLE_FINAL, mark_params)
        return FinalizeResult(
            status=FinalizeStatus.FINALIZED,
            cycle_start=cycle_start,
            authoritative_cycle_end=authoritative_cycle_end,
            final_source=final_source,
            snapshot_id=int(_row_value(selected, "id")),
            finalized_at=finalized_at,
        )

    @staticmethod
    def _snapshot_params(snapshot: UsageSnapshot) -> dict:
        """将领域模型映射为 MySQL 参数。"""
        return {
            "email": snapshot.email,
            "plan_tier": snapshot.plan_tier,
            "plan_tier_raw": snapshot.plan_tier_raw,
            "plan_status": snapshot.plan_status,
            "plan_source": snapshot.plan_source,
            "billing_cycle_start": _naive_utc_millis(
                snapshot.billing_cycle_start
            ),
            "billing_cycle_end": _naive_utc_millis(
                snapshot.billing_cycle_end
            ),
            "total_used_pct": snapshot.total_used_pct,
            "snapshot_type": snapshot.snapshot_type.value,
            "snapshot_slot": _naive_utc_millis(snapshot.snapshot_slot),
            "collected_at": _naive_utc_millis(snapshot.collected_at),
            "is_cycle_final": 0,
            "final_source": None,
            "finalized_at": None,
            "source_endpoint": snapshot.source_endpoint,
            "parser_version": snapshot.parser_version,
            "raw_payload": UsageSnapshotStore._serialize_raw_payload(
                snapshot.raw_payload
            ),
        }

    @staticmethod
    def _serialize_raw_payload(raw_payload: dict) -> str:
        """序列化 JSON，并拒绝 NaN 和无穷大。"""
        try:
            return json.dumps(
                raw_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except ValueError as exc:
            raise ValueError(
                "raw_payload 不允许非有限浮点数"
            ) from exc
