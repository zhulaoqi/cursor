"""MySQL 用量快照存储测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

import pymysql

from cam.usage_snapshot_models import SnapshotType, UsageSnapshot
from cam.usage_snapshot_store import (
    SchemaMismatchError,
    StaleCycleWriteError,
    UsageSnapshotStore,
    WriteResult,
)


UTC = timezone.utc


class FakeCursor:
    """按处理器返回结果的最小游标。"""

    def __init__(self, handler):
        self.handler = handler
        self.executed = []
        self.rows = []
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        result = self.handler(sql, params)
        self.rows = [] if result is None else list(result)
        return len(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class FakeConnection:
    """记录事务和资源释放的最小连接。"""

    def __init__(self, handler):
        self.cursor_obj = FakeCursor(handler)
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class FakePool:
    """每次借用都创建独立连接并保留记录。"""

    def __init__(self, handler):
        self.handler = handler
        self.connections = []

    def connection(self):
        connection = FakeConnection(self.handler)
        self.connections.append(connection)
        return connection


class StatefulSnapshotDB:
    """模拟快照表状态的最小字典游标处理器。"""

    def __init__(self):
        self.rows = []
        self.next_id = 1
        self.calls = []
        self.account_exists = True
        self.fail_once_contains = ""
        self.fail_error = None

    def add_snapshot(self, snapshot, **overrides):
        """直接加入一条已持久化快照。"""
        params = UsageSnapshotStore._snapshot_params(snapshot)
        row = {
            **params,
            "id": self.next_id,
            "created_at": params["collected_at"],
            "updated_at": params["collected_at"],
            **overrides,
        }
        self.next_id += 1
        self.rows.append(row)
        return row

    def handler(self, sql, params):
        """执行存储模块使用的有限 SQL 子集。"""
        normalized = " ".join(sql.split()).lower()
        self.calls.append((sql, params))
        if (
            self.fail_once_contains
            and self.fail_once_contains in normalized
        ):
            self.fail_once_contains = ""
            raise self.fail_error

        if (
            normalized.startswith("select id from cursor_accounts")
            and "for update" in normalized
        ):
            return [{"id": 1}] if self.account_exists else []

        if (
            normalized.startswith("select billing_cycle_start")
            and "for update" in normalized
        ):
            rows = [
                row
                for row in self.rows
                if row["email"] == params["email"]
            ]
            if not rows:
                return []
            latest = max(
                rows,
                key=lambda row: (row["billing_cycle_start"], row["id"]),
            )
            return [{"billing_cycle_start": latest["billing_cycle_start"]}]

        if (
            normalized.startswith(
                "select id, snapshot_type, billing_cycle_end"
            )
            and "for update" in normalized
        ):
            rows = [
                dict(row)
                for row in self.rows
                if row["email"] == params["email"]
                and row["billing_cycle_start"]
                == params["billing_cycle_start"]
            ]
            return sorted(
                rows,
                key=lambda row: (row["collected_at"], row["id"]),
                reverse=True,
            )

        if normalized.startswith("select id, collected_at"):
            for row in self.rows:
                if all(
                    row[key] == params[key]
                    for key in (
                        "email",
                        "billing_cycle_start",
                        "snapshot_type",
                        "snapshot_slot",
                    )
                ):
                    return [
                        {
                            "id": row["id"],
                            "collected_at": row["collected_at"],
                        }
                    ]
            return []

        if normalized.startswith("insert into cursor_usage_snapshot"):
            self.add_snapshot_from_params(params)
            return []

        if (
            normalized.startswith("update cursor_usage_snapshot")
            and "set plan_tier =" in normalized
        ):
            row = self._row_by_id(params["id"])
            for key in (
                "plan_tier",
                "plan_tier_raw",
                "plan_status",
                "plan_source",
                "billing_cycle_end",
                "total_used_pct",
                "snapshot_slot",
                "collected_at",
                "source_endpoint",
                "parser_version",
                "raw_payload",
            ):
                row[key] = params[key]
            return []

        if (
            normalized.startswith("update cursor_usage_snapshot")
            and "set is_cycle_final = 0" in normalized
        ):
            for row in self._cycle_rows(params):
                row["is_cycle_final"] = 0
                row["final_source"] = None
                row["finalized_at"] = None
            return []

        if (
            normalized.startswith("update cursor_usage_snapshot")
            and "is_cycle_final = 1" in normalized
        ):
            row = self._row_by_id(params["id"])
            row["billing_cycle_end"] = params["authoritative_cycle_end"]
            row["is_cycle_final"] = 1
            row["final_source"] = params["final_source"]
            row["finalized_at"] = params["finalized_at"]
            return []

        if (
            normalized.startswith("select *")
            and "is_cycle_final = 1" in normalized
        ):
            return sorted(
                [
                    dict(row)
                    for row in self.rows
                    if row["email"] == params["email"]
                    and row["is_cycle_final"] == 1
                ],
                key=lambda row: row["billing_cycle_end"],
                reverse=True,
            )

        raise AssertionError(f"未处理 SQL：{sql}")

    def add_snapshot_from_params(self, params):
        """按 INSERT 参数加入快照。"""
        row = {
            **params,
            "id": self.next_id,
            "created_at": params["collected_at"],
            "updated_at": params["collected_at"],
        }
        self.next_id += 1
        self.rows.append(row)
        return row

    def _cycle_rows(self, params):
        return [
            row
            for row in self.rows
            if row["email"] == params["email"]
            and row["billing_cycle_start"] == params["billing_cycle_start"]
        ]

    def _row_by_id(self, row_id):
        return next(row for row in self.rows if row["id"] == row_id)


def make_snapshot(
    *,
    email="user@example.com",
    cycle_start=None,
    cycle_end=None,
    snapshot_type=SnapshotType.PERIODIC,
    snapshot_slot=None,
    collected_at=None,
    total_used_pct=Decimal("12.34"),
    raw_payload=None,
):
    """构造有效快照。"""
    cycle_start = cycle_start or datetime(
        2026,
        7,
        1,
        0,
        0,
        0,
        123456,
        tzinfo=UTC,
    )
    if snapshot_slot is None:
        snapshot_slot = datetime(2026, 7, 2, 0, 0, 0, 987654, tzinfo=UTC)
    if snapshot_type is SnapshotType.PRE_RESET:
        snapshot_slot = cycle_start
    return UsageSnapshot(
        email=email,
        plan_tier="pro",
        plan_tier_raw="专业版",
        plan_status="active",
        plan_source="api",
        billing_cycle_start=cycle_start,
        billing_cycle_end=cycle_end or datetime(2026, 8, 1, tzinfo=UTC),
        total_used_pct=total_used_pct,
        snapshot_type=snapshot_type,
        snapshot_slot=snapshot_slot,
        collected_at=collected_at
        or datetime(2026, 7, 2, 3, 4, 5, 654321, tzinfo=UTC),
        source_endpoint="/api/usage",
        parser_version="v1",
        raw_payload=raw_payload or {"说明": "中文", "items": [1, 2]},
    )


def schema_columns(collation="utf8mb4_unicode_ci"):
    """返回与设计 DDL 一致的字段元数据。"""
    rows = [
        ("id", "bigint unsigned", "NO", None, None, None, "auto_increment"),
        ("email", "varchar(320)", "NO", None, "utf8mb4", collation, ""),
        ("plan_tier", "varchar(32)", "NO", "unknown", "utf8mb4", collation, ""),
        ("plan_tier_raw", "varchar(128)", "YES", None, "utf8mb4", collation, ""),
        ("plan_status", "varchar(32)", "NO", "unknown", "utf8mb4", collation, ""),
        ("plan_source", "varchar(32)", "NO", "api", "utf8mb4", collation, ""),
        ("billing_cycle_start", "datetime(3)", "NO", None, None, None, ""),
        ("billing_cycle_end", "datetime(3)", "NO", None, None, None, ""),
        ("total_used_pct", "decimal(5,2)", "NO", None, None, None, ""),
        ("snapshot_type", "varchar(16)", "NO", None, "utf8mb4", collation, ""),
        ("snapshot_slot", "datetime(3)", "NO", None, None, None, ""),
        ("collected_at", "datetime(3)", "NO", None, None, None, ""),
        ("is_cycle_final", "tinyint(1)", "NO", "0", None, None, ""),
        ("final_source", "varchar(32)", "YES", None, "utf8mb4", collation, ""),
        ("finalized_at", "datetime(3)", "YES", None, None, None, ""),
        ("source_endpoint", "varchar(255)", "YES", None, "utf8mb4", collation, ""),
        ("parser_version", "varchar(32)", "NO", None, "utf8mb4", collation, ""),
        ("raw_payload", "json", "YES", None, None, None, ""),
        ("created_at", "datetime(3)", "NO", "CURRENT_TIMESTAMP(3)", None, None, ""),
        (
            "updated_at",
            "datetime(3)",
            "NO",
            "CURRENT_TIMESTAMP(3)",
            None,
            None,
            "on update CURRENT_TIMESTAMP(3)",
        ),
    ]
    return [
        {
            "COLUMN_NAME": name,
            "COLUMN_TYPE": column_type,
            "IS_NULLABLE": nullable,
            "COLUMN_DEFAULT": default,
            "CHARACTER_SET_NAME": charset,
            "COLLATION_NAME": column_collation,
            "EXTRA": extra,
        }
        for name, column_type, nullable, default, charset, column_collation, extra in rows
    ]


def schema_indexes():
    """返回与设计 DDL 一致的索引元数据。"""
    definitions = {
        "PRIMARY": (0, ("id",)),
        "uk_usage_snapshot_slot": (
            0,
            ("email", "billing_cycle_start", "snapshot_type", "snapshot_slot"),
        ),
        "idx_usage_email_collected": (1, ("email", "collected_at")),
        "idx_usage_email_cycle_end": (1, ("email", "billing_cycle_end")),
        "idx_usage_due_scan": (
            1,
            ("billing_cycle_end", "snapshot_type", "collected_at"),
        ),
        "idx_usage_final": (
            1,
            ("email", "is_cycle_final", "billing_cycle_end"),
        ),
    }
    return [
        {
            "INDEX_NAME": name,
            "NON_UNIQUE": non_unique,
            "SEQ_IN_INDEX": position,
            "COLUMN_NAME": column,
        }
        for name, (non_unique, columns) in definitions.items()
        for position, column in enumerate(columns, 1)
    ]


def schema_checks():
    """返回与设计 DDL 一致的 CHECK 元数据。"""
    return [
        {
            "CONSTRAINT_NAME": "chk_usage_snapshot_type",
            "CHECK_CLAUSE": "snapshot_type IN ('periodic', 'pre_reset')",
            "ENFORCED": "YES",
        },
        {
            "CONSTRAINT_NAME": "chk_usage_pct",
            "CHECK_CLAUSE": "total_used_pct >= 0 AND total_used_pct <= 100",
            "ENFORCED": "YES",
        },
        {
            "CONSTRAINT_NAME": "chk_usage_cycle",
            "CHECK_CLAUSE": "billing_cycle_end > billing_cycle_start",
            "ENFORCED": "YES",
        },
        {
            "CONSTRAINT_NAME": "chk_usage_final_state",
            "CHECK_CLAUSE": (
                "(is_cycle_final = 0 AND final_source IS NULL "
                "AND finalized_at IS NULL) OR "
                "(is_cycle_final = 1 AND final_source IN "
                "('pre_reset', 'periodic_fallback') "
                "AND finalized_at IS NOT NULL)"
            ),
            "ENFORCED": "YES",
        },
    ]


def schema_handler(
    *,
    table_collation="utf8mb4_unicode_ci",
    columns=None,
    indexes=None,
    version="8.0.36",
    checks=None,
):
    """构造结构校验查询处理器。"""
    columns = schema_columns(table_collation) if columns is None else columns
    indexes = schema_indexes() if indexes is None else indexes
    checks = schema_checks() if checks is None else checks

    def handler(sql, params):
        normalized = " ".join(sql.split()).lower()
        if "select version()" in normalized:
            return [{"version": version}]
        if "information_schema.tables" in normalized:
            return [
                {
                    "ENGINE": "InnoDB",
                    "TABLE_COLLATION": table_collation,
                }
            ]
        if "information_schema.columns" in normalized:
            return columns
        if "information_schema.statistics" in normalized:
            return indexes
        if "information_schema.check_constraints" in normalized:
            return checks
        raise AssertionError(f"未处理 SQL：{sql}")

    return handler


class UsageSnapshotStoreSchemaTests(unittest.TestCase):
    def _collation_catalog_handler(
        self,
        *,
        catalog_row,
        collation="utf8mb4_unicode_ci",
    ):
        """构造可执行到 collation 目录校验的建表处理器。"""
        executed = []

        def handler(sql, params):
            executed.append(sql)
            normalized = " ".join(sql.split()).lower()
            if "information_schema.columns" in normalized:
                table = params[1]
                if "character_set_name" in normalized:
                    return [
                        {
                            "CHARACTER_SET_NAME": "utf8mb4",
                            "COLLATION_NAME": collation,
                        }
                    ]
                columns = (
                    ("id", "email", "applicant", "department")
                    if table == "cursor_accounts"
                    else ("email", "billing_month", "net_spend_usd")
                )
                return [{"COLUMN_NAME": name} for name in columns]
            if "information_schema.statistics" in normalized:
                return [
                    {
                        "INDEX_NAME": "uk_cursor_accounts_email",
                        "NON_UNIQUE": 0,
                        "SEQ_IN_INDEX": 1,
                        "COLUMN_NAME": "email",
                    }
                ]
            if "lower(trim(email))" in normalized:
                return []
            if "information_schema.collations" in normalized:
                return [] if catalog_row is None else [catalog_row]
            return []

        return handler, executed

    def test_ensure_schema_rejects_ledger_collation_before_create_table(self):
        executed = []

        def handler(sql, params):
            executed.append(sql)
            normalized = " ".join(sql.split()).lower()
            if "information_schema.columns" in normalized:
                table = params[1]
                if "character_set_name" in normalized:
                    collation = (
                        "utf8mb4_general_ci"
                        if table == "cursor_billing_ledger_summary"
                        else "utf8mb4_unicode_ci"
                    )
                    return [
                        {
                            "CHARACTER_SET_NAME": "utf8mb4",
                            "COLLATION_NAME": collation,
                        }
                    ]
                columns = (
                    ("id", "email", "applicant", "department")
                    if table == "cursor_accounts"
                    else ("email", "billing_month", "net_spend_usd")
                )
                return [{"COLUMN_NAME": name} for name in columns]
            if "information_schema.statistics" in normalized:
                return [
                    {
                        "INDEX_NAME": "uk_cursor_accounts_email",
                        "NON_UNIQUE": 0,
                        "SEQ_IN_INDEX": 1,
                        "COLUMN_NAME": "email",
                    }
                ]
            if "lower(trim(email))" in normalized:
                return []
            if "information_schema.collations" in normalized:
                return [
                    {
                        "COLLATION_NAME": "utf8mb4_bin",
                        "CHARACTER_SET_NAME": "utf8mb4",
                    }
                ]
            return []

        pool = FakePool(handler)
        store = UsageSnapshotStore(pool=pool)
        with self.assertRaisesRegex(SchemaMismatchError, "collation"):
            store.ensure_schema()

        self.assertFalse(any("CREATE TABLE" in sql for sql in executed))
        self.assertEqual(len(pool.connections), 1)
        self.assertEqual(pool.connections[0].rollbacks, 1)
        self.assertTrue(pool.connections[0].cursor_obj.closed)
        self.assertTrue(pool.connections[0].closed)

    def test_ensure_schema_contains_key_structure_and_dynamic_collation(self):
        executed = []

        def handler(sql, params):
            executed.append(sql)
            normalized = " ".join(sql.split()).lower()
            if "information_schema.columns" in normalized:
                table = params[1]
                if "character_set_name" not in normalized:
                    columns = (
                        ("id", "email", "applicant", "department")
                        if table == "cursor_accounts"
                        else ("email", "billing_month", "net_spend_usd")
                    )
                    return [{"COLUMN_NAME": name} for name in columns]
                return [
                    {
                        "CHARACTER_SET_NAME": "utf8mb4",
                        "COLLATION_NAME": "utf8mb4_bin",
                    }
                ]
            if "information_schema.statistics" in normalized:
                return [
                    {
                        "INDEX_NAME": "uk_cursor_accounts_email",
                        "NON_UNIQUE": 0,
                        "SEQ_IN_INDEX": 1,
                        "COLUMN_NAME": "email",
                    }
                ]
            if "lower(trim(email))" in normalized:
                return []
            if "information_schema.collations" in normalized:
                return [
                    {
                        "COLLATION_NAME": "utf8mb4_bin",
                        "CHARACTER_SET_NAME": "utf8mb4",
                    }
                ]
            return []

        pool = FakePool(handler)
        store = UsageSnapshotStore(pool=pool)
        with patch.object(store, "_validate_schema_with_cursor"):
            store.ensure_schema()

        ddl = next(sql for sql in executed if "CREATE TABLE" in sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS cursor_usage_snapshot", ddl)
        self.assertIn("ENGINE=InnoDB", ddl)
        self.assertIn("DEFAULT CHARSET=utf8mb4", ddl)
        self.assertIn("COLLATE=utf8mb4_bin", ddl)
        self.assertIn("UNIQUE KEY uk_usage_snapshot_slot", ddl)
        for index_name in (
            "idx_usage_email_collected",
            "idx_usage_email_cycle_end",
            "idx_usage_due_scan",
            "idx_usage_final",
        ):
            self.assertIn(index_name, ddl)
        for check_name in (
            "chk_usage_snapshot_type",
            "chk_usage_pct",
            "chk_usage_cycle",
            "chk_usage_final_state",
        ):
            self.assertIn(check_name, ddl)
        self.assertIn("COMMENT='Cursor 账号订阅档位与账期用量时序快照'", ddl)
        self.assertEqual(len(pool.connections), 1)
        self.assertEqual(pool.connections[0].commits, 1)
        self.assertEqual(pool.connections[0].rollbacks, 0)
        self.assertTrue(pool.connections[0].cursor_obj.closed)
        self.assertTrue(pool.connections[0].closed)

    def test_ensure_schema_rejects_unknown_collation_before_create_table(self):
        handler, executed = self._collation_catalog_handler(catalog_row=None)
        store = UsageSnapshotStore(pool=FakePool(handler))
        with patch.object(store, "_validate_schema_with_cursor"):
            with self.assertRaisesRegex(SchemaMismatchError, "collation"):
                store.ensure_schema()
        self.assertFalse(any("CREATE TABLE" in sql for sql in executed))

    def test_ensure_schema_rejects_non_utf8mb4_collation_before_create_table(self):
        handler, executed = self._collation_catalog_handler(
            catalog_row={
                "COLLATION_NAME": "latin1_bin",
                "CHARACTER_SET_NAME": "latin1",
            },
            collation="latin1_bin",
        )
        store = UsageSnapshotStore(pool=FakePool(handler))
        with patch.object(store, "_validate_schema_with_cursor"):
            with self.assertRaisesRegex(SchemaMismatchError, "utf8mb4"):
                store.ensure_schema()
        self.assertFalse(any("CREATE TABLE" in sql for sql in executed))

    def test_validate_schema_accepts_exact_mysql8_schema(self):
        store = UsageSnapshotStore(pool=FakePool(schema_handler()))
        store.validate_schema()

    def test_validate_schema_accepts_mysql8_default_generated_marker(self):
        columns = schema_columns()
        columns[-2] = {
            **columns[-2],
            "EXTRA": "DEFAULT_GENERATED",
        }
        columns[-1] = {
            **columns[-1],
            "EXTRA": "DEFAULT_GENERATED on update CURRENT_TIMESTAMP(3)",
        }
        store = UsageSnapshotStore(
            pool=FakePool(schema_handler(columns=columns))
        )
        store.validate_schema()

    def test_validate_schema_accepts_mysql_json_internal_collation(self):
        columns = schema_columns()
        columns[-3] = {
            **columns[-3],
            "CHARACTER_SET_NAME": "utf8mb4",
            "COLLATION_NAME": "utf8mb4_bin",
        }
        store = UsageSnapshotStore(
            pool=FakePool(schema_handler(columns=columns))
        )
        store.validate_schema()

    def test_validate_schema_rejects_wrong_collation(self):
        store = UsageSnapshotStore(
            pool=FakePool(
                schema_handler(
                    table_collation="utf8mb4_general_ci",
                    columns=schema_columns("utf8mb4_unicode_ci"),
                )
            )
        )
        with self.assertRaisesRegex(SchemaMismatchError, "collation"):
            store.validate_schema()

    def test_validate_schema_rejects_column_type_mismatch(self):
        columns = schema_columns()
        columns[8] = {**columns[8], "COLUMN_TYPE": "decimal(6,2)"}
        store = UsageSnapshotStore(
            pool=FakePool(schema_handler(columns=columns))
        )
        with self.assertRaisesRegex(SchemaMismatchError, "total_used_pct"):
            store.validate_schema()

    def test_validate_schema_rejects_index_order_mismatch(self):
        indexes = schema_indexes()
        for row in indexes:
            if (
                row["INDEX_NAME"] == "idx_usage_email_collected"
                and row["SEQ_IN_INDEX"] == 2
            ):
                row["COLUMN_NAME"] = "billing_cycle_end"
        store = UsageSnapshotStore(
            pool=FakePool(schema_handler(indexes=indexes))
        )
        with self.assertRaisesRegex(
            SchemaMismatchError,
            "idx_usage_email_collected",
        ):
            store.validate_schema()

    def test_mysql8_requires_all_checks(self):
        store = UsageSnapshotStore(
            pool=FakePool(schema_handler(checks=[{"CONSTRAINT_NAME": "chk_usage_pct"}]))
        )
        with self.assertRaisesRegex(SchemaMismatchError, "CHECK"):
            store.validate_schema()

    def test_mysql8_rejects_wrong_check_definition(self):
        wrong_checks = [
            {
                **row,
                "CHECK_CLAUSE": (
                    "total_used_pct >= 0 AND total_used_pct <= 999"
                    if row["CONSTRAINT_NAME"] == "chk_usage_pct"
                    else row["CHECK_CLAUSE"]
                ),
            }
            for row in schema_checks()
        ]
        store = UsageSnapshotStore(
            pool=FakePool(schema_handler(checks=wrong_checks))
        )
        with self.assertRaisesRegex(SchemaMismatchError, "chk_usage_pct"):
            store.validate_schema()

    def test_mysql8_rejects_not_enforced_check(self):
        checks = [
            {
                **row,
                "ENFORCED": (
                    "NO"
                    if row["CONSTRAINT_NAME"] == "chk_usage_pct"
                    else "YES"
                ),
            }
            for row in schema_checks()
        ]
        store = UsageSnapshotStore(
            pool=FakePool(schema_handler(checks=checks))
        )
        with self.assertRaisesRegex(
            SchemaMismatchError,
            "chk_usage_pct.*ENFORCED",
        ):
            store.validate_schema()

    def test_mysql8_rejects_missing_enforced_metadata(self):
        checks = schema_checks()
        del checks[0]["ENFORCED"]
        store = UsageSnapshotStore(
            pool=FakePool(schema_handler(checks=checks))
        )
        with self.assertRaisesRegex(
            SchemaMismatchError,
            "chk_usage_snapshot_type.*ENFORCED",
        ):
            store.validate_schema()

    def test_mysql57_does_not_require_checks(self):
        calls = []
        base_handler = schema_handler(version="5.7.44-log", checks=[])

        def handler(sql, params):
            calls.append(sql)
            return base_handler(sql, params)

        store = UsageSnapshotStore(pool=FakePool(handler))
        store.validate_schema()
        self.assertFalse(
            any("CHECK_CONSTRAINTS" in sql for sql in calls)
        )

    def test_mysql_8_before_check_enforcement_is_rejected(self):
        store = UsageSnapshotStore(
            pool=FakePool(schema_handler(version="8.0.15"))
        )
        with self.assertRaisesRegex(SchemaMismatchError, "8.0.16"):
            store.validate_schema()

    def test_mysql_8016_validates_checks(self):
        calls = []
        base_handler = schema_handler(version="8.0.16")

        def handler(sql, params):
            calls.append(sql)
            return base_handler(sql, params)

        store = UsageSnapshotStore(pool=FakePool(handler))
        store.validate_schema()
        check_sql = next(
            sql for sql in calls if "CHECK_CONSTRAINTS" in sql
        )
        self.assertIn("tc.ENFORCED", check_sql)

    def test_mariadb_is_rejected_explicitly(self):
        store = UsageSnapshotStore(
            pool=FakePool(
                schema_handler(version="10.11.6-MariaDB-0+deb12u1")
            )
        )
        with self.assertRaisesRegex(SchemaMismatchError, "MariaDB"):
            store.validate_schema()


class UsageSnapshotStoreContractTests(unittest.TestCase):
    def make_handler(
        self,
        *,
        account_columns=("id", "email", "applicant", "department"),
        ledger_columns=("email", "billing_month", "net_spend_usd"),
        unique_columns=("email",),
        duplicate=None,
        account_collation="utf8mb4_unicode_ci",
        ledger_collation="utf8mb4_unicode_ci",
        snapshot_collation="utf8mb4_unicode_ci",
    ):
        def handler(sql, params):
            normalized = " ".join(sql.split()).lower()
            if "information_schema.columns" in normalized:
                table = params[1]
                if table == "cursor_accounts":
                    if "character_set_name" in normalized:
                        return [
                            {
                                "CHARACTER_SET_NAME": "utf8mb4",
                                "COLLATION_NAME": account_collation,
                            }
                        ]
                    return [{"COLUMN_NAME": name} for name in account_columns]
                if table == "cursor_billing_ledger_summary":
                    if "character_set_name" in normalized:
                        return [
                            {
                                "CHARACTER_SET_NAME": "utf8mb4",
                                "COLLATION_NAME": ledger_collation,
                            }
                        ]
                    return [{"COLUMN_NAME": name} for name in ledger_columns]
                if table == "cursor_usage_snapshot":
                    return [
                        {
                            "CHARACTER_SET_NAME": "utf8mb4",
                            "COLLATION_NAME": snapshot_collation,
                        }
                    ]
            if "information_schema.statistics" in normalized:
                return [
                    {
                        "INDEX_NAME": "uk_cursor_accounts_email",
                        "NON_UNIQUE": 0,
                        "SEQ_IN_INDEX": index,
                        "COLUMN_NAME": column,
                    }
                    for index, column in enumerate(unique_columns, 1)
                ]
            if "lower(trim(email))" in normalized:
                return [] if duplicate is None else [duplicate]
            raise AssertionError(f"未处理 SQL：{sql}")

        return handler

    def test_monitor_contract_accepts_required_master_data(self):
        store = UsageSnapshotStore(pool=FakePool(self.make_handler()))
        store.validate_monitor_contract()

    def test_monitor_contract_requires_four_account_columns(self):
        store = UsageSnapshotStore(
            pool=FakePool(
                self.make_handler(
                    account_columns=("id", "email", "applicant"),
                )
            )
        )
        with self.assertRaisesRegex(SchemaMismatchError, "department"):
            store.validate_monitor_contract()

    def test_monitor_contract_requires_raw_unique_email_index(self):
        store = UsageSnapshotStore(
            pool=FakePool(self.make_handler(unique_columns=("email", "id")))
        )
        with self.assertRaisesRegex(SchemaMismatchError, "唯一索引"):
            store.validate_monitor_contract()

    def test_monitor_contract_rejects_normalized_email_duplicates(self):
        store = UsageSnapshotStore(
            pool=FakePool(
                self.make_handler(
                    duplicate={"normalized_email": "same@example.com", "count": 2},
                )
            )
        )
        with self.assertRaisesRegex(SchemaMismatchError, "规范化.*重复"):
            store.validate_monitor_contract()

    def test_monitor_contract_checks_all_email_collations(self):
        store = UsageSnapshotStore(
            pool=FakePool(
                self.make_handler(ledger_collation="utf8mb4_general_ci")
            )
        )
        with self.assertRaisesRegex(SchemaMismatchError, "collation"):
            store.validate_monitor_contract()

    def test_monitor_contract_requires_ledger_columns(self):
        store = UsageSnapshotStore(
            pool=FakePool(
                self.make_handler(ledger_columns=("email", "billing_month"))
            )
        )
        with self.assertRaisesRegex(SchemaMismatchError, "net_spend_usd"):
            store.validate_monitor_contract()

    def test_contract_error_never_contains_database_password(self):
        settings = type(
            "Settings",
            (),
            {
                "ledger_db_name": "aicoding",
                "ledger_db_password": "绝密密码",
            },
        )()
        store = UsageSnapshotStore(
            settings=settings,
            pool=FakePool(self.make_handler(account_columns=())),
        )
        with self.assertRaises(SchemaMismatchError) as captured:
            store.validate_monitor_contract()
        self.assertNotIn("绝密密码", str(captured.exception))


class UsageSnapshotStoreQueryTests(unittest.TestCase):
    def test_list_monitor_accounts_uses_fixed_query(self):
        rows = [
            {
                "id": 1,
                "email": " User@Example.COM ",
                "applicant": "张三",
                "department": "研发",
            }
        ]

        def handler(sql, params):
            self.assertEqual(
                " ".join(sql.split()),
                "SELECT id,email,applicant,department FROM cursor_accounts "
                "WHERE email IS NOT NULL AND TRIM(email)<>''",
            )
            self.assertIsNone(params)
            return rows

        store = UsageSnapshotStore(pool=FakePool(handler))
        self.assertEqual(store.list_monitor_accounts(), rows)

    def test_email_queries_require_trimmed_lowercase_nonempty_email(self):
        store = UsageSnapshotStore(pool=FakePool(lambda sql, params: []))
        for bad_email in ("", "   ", " User@example.com", "USER@example.com"):
            with self.subTest(email=bad_email):
                with self.assertRaises(ValueError):
                    store.get_latest_cycle(bad_email)

    def test_latest_queries_and_slot_convert_utc_to_millisecond_datetime(self):
        calls = []

        def handler(sql, params):
            calls.append((sql, params))
            if "COUNT(*)" in sql:
                return [{"count": 1}]
            return [{"email": "user@example.com"}]

        store = UsageSnapshotStore(pool=FakePool(handler))
        slot = datetime(2026, 7, 2, 1, 2, 3, 987654, tzinfo=UTC)
        self.assertIsNotNone(store.get_latest_cycle("user@example.com"))
        self.assertIsNotNone(store.get_latest_snapshot("user@example.com"))
        self.assertTrue(store.has_periodic_slot("user@example.com", slot))
        for _, params in calls:
            self.assertEqual(params["email"], "user@example.com")
        slot_call = next(item for item in calls if "COUNT(*)" in item[0])
        self.assertEqual(
            slot_call[1]["snapshot_slot"],
            datetime(2026, 7, 2, 1, 2, 3, 987000),
        )


class UsageSnapshotStoreWriteTests(unittest.TestCase):
    def run_write(self, snapshot, stored=None, fail_on=None):
        calls = []

        def handler(sql, params):
            calls.append((sql, params))
            normalized = " ".join(sql.split()).lower()
            if normalized.startswith("select id from cursor_accounts"):
                return [{"id": 1}]
            if normalized.startswith("select billing_cycle_start"):
                return []
            if "select id, collected_at" in normalized:
                return [] if stored is None else [stored]
            if fail_on and fail_on in normalized:
                raise RuntimeError("模拟写库失败")
            return []

        pool = FakePool(handler)
        result = UsageSnapshotStore(pool=pool).upsert_same_cycle(snapshot)
        return result, calls, pool

    def test_periodic_same_slot_first_insert_then_idempotent(self):
        snapshot = make_snapshot()
        result, calls, _ = self.run_write(snapshot)
        self.assertEqual(result, WriteResult.INSERTED)
        self.assertTrue(any("INSERT INTO cursor_usage_snapshot" in sql for sql, _ in calls))

        result, calls, _ = self.run_write(
            snapshot,
            stored={
                "id": 9,
                "collected_at": datetime(2026, 7, 2, 3, 4, 5, 654000),
            },
        )
        self.assertEqual(result, WriteResult.IDEMPOTENT)
        self.assertFalse(any("UPDATE cursor_usage_snapshot" in sql for sql, _ in calls))

    def test_periodic_different_slot_inserts_new_row(self):
        first = make_snapshot()
        second = make_snapshot(
            snapshot_slot=first.snapshot_slot + timedelta(hours=24),
        )
        first_result, _, _ = self.run_write(first)
        second_result, second_calls, _ = self.run_write(second)
        self.assertEqual(first_result, WriteResult.INSERTED)
        self.assertEqual(second_result, WriteResult.INSERTED)
        select_params = next(
            params
            for sql, params in second_calls
            if "SELECT id, collected_at" in sql
        )
        self.assertEqual(
            select_params["snapshot_slot"],
            datetime(2026, 7, 3, 0, 0, 0, 987000),
        )

    def test_pre_reset_newer_value_updates_same_cycle(self):
        snapshot = make_snapshot(snapshot_type=SnapshotType.PRE_RESET)
        result, calls, _ = self.run_write(
            snapshot,
            stored={
                "id": 5,
                "collected_at": datetime(2026, 7, 1, tzinfo=None),
            },
        )
        self.assertEqual(result, WriteResult.UPDATED)
        update_sql, params = next(
            item for item in calls if "UPDATE cursor_usage_snapshot" in item[0]
        )
        self.assertNotIn("is_cycle_final", update_sql)
        self.assertNotIn("final_source", update_sql)
        self.assertNotIn("finalized_at", update_sql)
        self.assertEqual(params["id"], 5)
        self.assertEqual(
            params["snapshot_slot"],
            params["billing_cycle_start"],
        )

    def test_older_value_does_not_overwrite(self):
        snapshot = make_snapshot(
            collected_at=datetime(2026, 7, 2, tzinfo=UTC),
        )
        result, calls, _ = self.run_write(
            snapshot,
            stored={
                "id": 5,
                "collected_at": datetime(2026, 7, 3),
            },
        )
        self.assertEqual(result, WriteResult.IDEMPOTENT)
        self.assertFalse(
            any(
                "INSERT INTO cursor_usage_snapshot" in sql
                or "UPDATE cursor_usage_snapshot" in sql
                for sql, _ in calls
            )
        )

    def test_zero_percent_decimal_and_chinese_json_are_preserved(self):
        snapshot = make_snapshot(
            total_used_pct=Decimal("0"),
            raw_payload={"说明": "中文", "嵌套": {"值": 0}},
        )
        result, calls, _ = self.run_write(snapshot)
        self.assertEqual(result, WriteResult.INSERTED)
        _, params = next(
            item for item in calls if "INSERT INTO cursor_usage_snapshot" in item[0]
        )
        self.assertEqual(params["total_used_pct"], Decimal("0"))
        self.assertEqual(
            params["raw_payload"],
            '{"说明":"中文","嵌套":{"值":0}}',
        )
        self.assertNotIn("\\u", params["raw_payload"])

    def test_utc_values_are_naive_and_truncated_to_milliseconds(self):
        snapshot = make_snapshot()
        _, calls, _ = self.run_write(snapshot)
        _, params = next(
            item for item in calls if "INSERT INTO cursor_usage_snapshot" in item[0]
        )
        self.assertIsNone(params["collected_at"].tzinfo)
        self.assertEqual(params["collected_at"].microsecond, 654000)
        self.assertEqual(params["billing_cycle_start"].microsecond, 123000)

    def test_insert_initializes_final_fields_without_updating_them(self):
        snapshot = make_snapshot()
        _, calls, _ = self.run_write(snapshot)
        insert_sql, params = next(
            item for item in calls if "INSERT INTO cursor_usage_snapshot" in item[0]
        )
        compact = " ".join(insert_sql.split())
        self.assertIn("is_cycle_final", compact)
        self.assertIn("final_source", compact)
        self.assertIn("finalized_at", compact)
        self.assertEqual(params["is_cycle_final"], 0)
        self.assertIsNone(params["final_source"])
        self.assertIsNone(params["finalized_at"])

    def test_write_commits_and_closes_cursor_and_connection(self):
        snapshot = make_snapshot()
        _, _, pool = self.run_write(snapshot)
        connection = pool.connections[0]
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        self.assertTrue(connection.cursor_obj.closed)
        self.assertTrue(connection.closed)

    def test_write_exception_rolls_back_and_closes_resources(self):
        snapshot = make_snapshot()

        def handler(sql, params):
            normalized = " ".join(sql.split()).lower()
            if normalized.startswith("select id from cursor_accounts"):
                return [{"id": 1}]
            if normalized.startswith("select billing_cycle_start"):
                return []
            if "SELECT id, collected_at" in sql:
                return []
            if "INSERT INTO cursor_usage_snapshot" in sql:
                raise RuntimeError("模拟写库失败")
            return []

        pool = FakePool(handler)
        with self.assertRaisesRegex(RuntimeError, "模拟写库失败"):
            UsageSnapshotStore(pool=pool).upsert_same_cycle(snapshot)
        connection = pool.connections[0]
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(connection.cursor_obj.closed)
        self.assertTrue(connection.closed)

    def test_duplicate_key_retries_and_then_returns_idempotent(self):
        snapshot = make_snapshot()
        insert_attempts = 0

        def handler(sql, params):
            nonlocal insert_attempts
            normalized = " ".join(sql.split()).lower()
            if normalized.startswith("select id from cursor_accounts"):
                return [{"id": 1}]
            if normalized.startswith("select billing_cycle_start"):
                return []
            if "SELECT id, collected_at" in sql:
                if insert_attempts:
                    return [
                        {
                            "id": 8,
                            "collected_at": datetime(
                                2026,
                                7,
                                2,
                                3,
                                4,
                                5,
                                654000,
                            ),
                        }
                    ]
                return []
            if "INSERT INTO cursor_usage_snapshot" in sql:
                insert_attempts += 1
                raise pymysql.err.IntegrityError(1062, "模拟重复键")
            return []

        pool = FakePool(handler)
        result = UsageSnapshotStore(pool=pool).upsert_same_cycle(snapshot)
        self.assertEqual(result, WriteResult.IDEMPOTENT)
        self.assertEqual(len(pool.connections), 2)
        self.assertEqual(pool.connections[0].rollbacks, 1)
        self.assertEqual(pool.connections[1].commits, 1)
        self.assertTrue(all(item.closed for item in pool.connections))
        self.assertTrue(
            all(item.cursor_obj.closed for item in pool.connections)
        )

    def test_deadlock_retries_with_a_new_complete_transaction(self):
        snapshot = make_snapshot()
        insert_attempts = 0

        def handler(sql, params):
            nonlocal insert_attempts
            normalized = " ".join(sql.split()).lower()
            if normalized.startswith("select id from cursor_accounts"):
                return [{"id": 1}]
            if normalized.startswith("select billing_cycle_start"):
                return []
            if "SELECT id, collected_at" in sql:
                return []
            if "INSERT INTO cursor_usage_snapshot" in sql:
                insert_attempts += 1
                if insert_attempts == 1:
                    raise pymysql.err.OperationalError(1213, "模拟死锁")
            return []

        pool = FakePool(handler)
        result = UsageSnapshotStore(pool=pool).upsert_same_cycle(snapshot)
        self.assertEqual(result, WriteResult.INSERTED)
        self.assertEqual(len(pool.connections), 2)
        self.assertEqual(pool.connections[0].rollbacks, 1)
        self.assertEqual(pool.connections[1].commits, 1)
        self.assertTrue(all(item.closed for item in pool.connections))

    def test_non_retryable_database_error_is_raised_immediately(self):
        snapshot = make_snapshot()
        error = pymysql.err.OperationalError(1048, "模拟非重试错误")

        def handler(sql, params):
            normalized = " ".join(sql.split()).lower()
            if normalized.startswith("select id from cursor_accounts"):
                return [{"id": 1}]
            if normalized.startswith("select billing_cycle_start"):
                return []
            if "SELECT id, collected_at" in sql:
                return []
            if "INSERT INTO cursor_usage_snapshot" in sql:
                raise error
            return []

        pool = FakePool(handler)
        with self.assertRaises(pymysql.err.OperationalError) as captured:
            UsageSnapshotStore(pool=pool).upsert_same_cycle(snapshot)
        self.assertIs(captured.exception, error)
        self.assertEqual(len(pool.connections), 1)
        self.assertEqual(pool.connections[0].rollbacks, 1)
        self.assertTrue(pool.connections[0].closed)

    def test_retry_exhaustion_raises_last_original_error(self):
        snapshot = make_snapshot()
        errors = [
            pymysql.err.OperationalError(1205, f"第 {index} 次锁等待超时")
            for index in range(1, 4)
        ]
        attempts = 0

        def handler(sql, params):
            nonlocal attempts
            normalized = " ".join(sql.split()).lower()
            if normalized.startswith("select id from cursor_accounts"):
                return [{"id": 1}]
            if normalized.startswith("select billing_cycle_start"):
                return []
            if "SELECT id, collected_at" in sql:
                return []
            if "INSERT INTO cursor_usage_snapshot" in sql:
                error = errors[attempts]
                attempts += 1
                raise error
            return []

        pool = FakePool(handler)
        with self.assertRaises(pymysql.err.OperationalError) as captured:
            UsageSnapshotStore(pool=pool).upsert_same_cycle(snapshot)
        self.assertIs(captured.exception, errors[-1])
        self.assertEqual(len(pool.connections), 3)
        for connection in pool.connections:
            self.assertEqual(connection.rollbacks, 1)
            self.assertEqual(connection.commits, 0)
            self.assertTrue(connection.cursor_obj.closed)
            self.assertTrue(connection.closed)

    def test_non_finite_float_in_raw_payload_rolls_back_as_value_error(self):
        snapshot = make_snapshot(raw_payload={"值": float("nan")})

        def handler(sql, params):
            normalized = " ".join(sql.split()).lower()
            if normalized.startswith("select id from cursor_accounts"):
                return [{"id": 1}]
            return []

        pool = FakePool(handler)
        with self.assertRaisesRegex(ValueError, "raw_payload"):
            UsageSnapshotStore(pool=pool).upsert_same_cycle(snapshot)
        self.assertEqual(len(pool.connections), 1)
        self.assertEqual(pool.connections[0].rollbacks, 1)
        self.assertEqual(pool.connections[0].commits, 0)
        self.assertTrue(pool.connections[0].cursor_obj.closed)
        self.assertTrue(pool.connections[0].closed)

    def test_pre_reset_slot_must_equal_cycle_start(self):
        snapshot = make_snapshot(snapshot_type=SnapshotType.PRE_RESET)
        object.__setattr__(
            snapshot,
            "snapshot_slot",
            snapshot.billing_cycle_start + timedelta(seconds=1),
        )
        pool = FakePool(lambda sql, params: [])
        with self.assertRaisesRegex(ValueError, "snapshot_slot"):
            UsageSnapshotStore(pool=pool).upsert_same_cycle(snapshot)
        self.assertEqual(pool.connections, [])


class UsageSnapshotStoreReconcileTests(unittest.TestCase):
    """账期切换、结算和修复事务测试。"""

    def make_store(self):
        database = StatefulSnapshotDB()
        pool = FakePool(database.handler)
        return UsageSnapshotStore(pool=pool), database, pool

    @staticmethod
    def cycle_start(month):
        return datetime(2026, month, 1, tzinfo=UTC)

    def snapshot(
        self,
        *,
        month=7,
        cycle_end=None,
        collected_day=2,
        snapshot_type=SnapshotType.PERIODIC,
        slot_hour=0,
    ):
        start = self.cycle_start(month)
        return make_snapshot(
            cycle_start=start,
            cycle_end=cycle_end
            or self.cycle_start(month + 1),
            snapshot_type=snapshot_type,
            snapshot_slot=start + timedelta(days=1, hours=slot_hour),
            collected_at=start + timedelta(days=collected_day),
        )

    def test_reconcile_first_snapshot_inserts_without_finalization(self):
        store, database, pool = self.make_store()
        result = store.reconcile_and_write(self.snapshot())
        self.assertEqual(result.write_result, WriteResult.INSERTED)
        self.assertIsNone(result.finalize_result)
        self.assertEqual(len(database.rows), 1)
        self.assertEqual(pool.connections[0].commits, 1)

    def test_read_committed_reconcile_locks_account_before_empty_snapshot_range(
        self,
    ):
        store, database, _ = self.make_store()
        store.reconcile_and_write(self.snapshot())
        sql_calls = [
            " ".join(sql.split()).lower()
            for sql, _ in database.calls
        ]
        account_lock_index = next(
            index
            for index, sql in enumerate(sql_calls)
            if sql.startswith("select id from cursor_accounts")
        )
        latest_cycle_index = next(
            index
            for index, sql in enumerate(sql_calls)
            if sql.startswith("select billing_cycle_start")
        )
        self.assertEqual(account_lock_index, 0)
        self.assertLess(account_lock_index, latest_cycle_index)
        self.assertEqual(len(database.rows), 1)

    def test_reconcile_rejects_missing_master_account(self):
        store, database, pool = self.make_store()
        database.account_exists = False
        with self.assertRaisesRegex(ValueError, "主数据账号不存在"):
            store.reconcile_and_write(self.snapshot())
        self.assertEqual(database.rows, [])
        self.assertEqual(pool.connections[0].rollbacks, 1)

    def test_same_start_end_extension_or_reduction_does_not_finalize(self):
        for cycle_end in (
            datetime(2026, 8, 5, tzinfo=UTC),
            datetime(2026, 7, 28, tzinfo=UTC),
        ):
            with self.subTest(cycle_end=cycle_end):
                store, database, _ = self.make_store()
                database.add_snapshot(self.snapshot())
                result = store.reconcile_and_write(
                    self.snapshot(
                        cycle_end=cycle_end,
                        collected_day=3,
                        slot_hour=6,
                    )
                )
                self.assertEqual(result.write_result, WriteResult.INSERTED)
                self.assertIsNone(result.finalize_result)
                self.assertFalse(
                    any(row["is_cycle_final"] for row in database.rows)
                )

    def test_new_start_finalizes_old_cycle_before_insert(self):
        store, database, _ = self.make_store()
        database.add_snapshot(self.snapshot(collected_day=5))
        result = store.reconcile_and_write(self.snapshot(month=8))
        self.assertEqual(result.write_result, WriteResult.INSERTED)
        self.assertEqual(result.finalize_result.status.value, "finalized")
        self.assertEqual(
            result.finalize_result.final_source.value,
            "periodic_fallback",
        )
        self.assertEqual(len(database.rows), 2)
        self.assertEqual(
            sum(row["is_cycle_final"] for row in database.rows),
            1,
        )

    def test_finalization_prefers_latest_pre_reset(self):
        store, database, _ = self.make_store()
        periodic = database.add_snapshot(
            self.snapshot(collected_day=5, slot_hour=1)
        )
        pre_reset = database.add_snapshot(
            self.snapshot(
                collected_day=28,
                snapshot_type=SnapshotType.PRE_RESET,
            )
        )
        database.add_snapshot(
            self.snapshot(collected_day=30, slot_hour=2)
        )
        result = store.repair_finalize_cycle(
            "user@example.com",
            self.cycle_start(7),
            actor="质量管理员",
            reason="补做账期结算",
        )
        self.assertEqual(result.final_source.value, "pre_reset")
        self.assertEqual(result.snapshot_id, pre_reset["id"])
        self.assertEqual(pre_reset["is_cycle_final"], 1)
        self.assertEqual(periodic["is_cycle_final"], 0)

    def test_finalization_falls_back_to_latest_periodic(self):
        store, database, _ = self.make_store()
        database.add_snapshot(self.snapshot(collected_day=5, slot_hour=1))
        latest = database.add_snapshot(
            self.snapshot(collected_day=8, slot_hour=2)
        )
        result = store.repair_finalize_cycle(
            "user@example.com",
            self.cycle_start(7),
            actor="质量管理员",
            reason="缺少重置前快照",
        )
        self.assertEqual(result.final_source.value, "periodic_fallback")
        self.assertEqual(result.snapshot_id, latest["id"])

    def test_candidate_must_not_be_later_than_authoritative_end(self):
        store, database, _ = self.make_store()
        periodic = database.add_snapshot(
            self.snapshot(
                cycle_end=datetime(2026, 7, 10, tzinfo=UTC),
                collected_day=5,
                slot_hour=1,
            )
        )
        pre_reset = database.add_snapshot(
            self.snapshot(
                cycle_end=datetime(2026, 7, 10, tzinfo=UTC),
                collected_day=20,
                snapshot_type=SnapshotType.PRE_RESET,
            )
        )
        database.add_snapshot(
            self.snapshot(
                cycle_end=datetime(2026, 7, 10, tzinfo=UTC),
                collected_day=30,
                slot_hour=2,
            )
        )
        result = store.repair_finalize_cycle(
            "user@example.com",
            self.cycle_start(7),
            actor="质量管理员",
            reason="排除账期结束后的候选",
        )
        self.assertEqual(result.snapshot_id, periodic["id"])
        self.assertEqual(pre_reset["is_cycle_final"], 0)

    def test_authoritative_end_comes_from_latest_collected_row_and_syncs_candidate(self):
        store, database, _ = self.make_store()
        pre_reset = database.add_snapshot(
            self.snapshot(
                cycle_end=datetime(2026, 8, 1, tzinfo=UTC),
                collected_day=20,
                snapshot_type=SnapshotType.PRE_RESET,
            )
        )
        authoritative_end = datetime(2026, 8, 5, tzinfo=UTC)
        database.add_snapshot(
            self.snapshot(
                cycle_end=authoritative_end,
                collected_day=30,
                slot_hour=2,
            )
        )
        result = store.repair_finalize_cycle(
            "user@example.com",
            self.cycle_start(7),
            actor="质量管理员",
            reason="同步权威账期结束时间",
        )
        expected_end = authoritative_end.replace(tzinfo=None)
        self.assertEqual(result.authoritative_cycle_end, expected_end)
        self.assertEqual(pre_reset["billing_cycle_end"], expected_end)

    def test_missing_candidate_does_not_fabricate_zero_percent(self):
        store, database, _ = self.make_store()
        database.add_snapshot(
            self.snapshot(
                cycle_end=datetime(2026, 7, 10, tzinfo=UTC),
                collected_day=30,
            )
        )
        result = store.repair_finalize_cycle(
            "user@example.com",
            self.cycle_start(7),
            actor="质量管理员",
            reason="确认无合法候选",
        )
        self.assertEqual(result.status.value, "missing_cycle_final")
        self.assertIsNone(result.final_source)
        self.assertFalse(any(row["is_cycle_final"] for row in database.rows))
        self.assertEqual(database.rows[0]["total_used_pct"], Decimal("12.34"))

    def test_new_cycle_is_inserted_even_when_old_cycle_has_no_candidate(self):
        store, database, _ = self.make_store()
        database.add_snapshot(
            self.snapshot(
                cycle_end=datetime(2026, 7, 10, tzinfo=UTC),
                collected_day=30,
            )
        )
        result = store.reconcile_and_write(self.snapshot(month=8))
        self.assertEqual(
            result.finalize_result.status.value,
            "missing_cycle_final",
        )
        self.assertEqual(result.write_result, WriteResult.INSERTED)
        self.assertEqual(len(database.rows), 2)

    def test_repeated_finalization_preserves_finalized_at(self):
        store, database, _ = self.make_store()
        database.add_snapshot(self.snapshot(collected_day=5))
        first = store.repair_finalize_cycle(
            "user@example.com",
            self.cycle_start(7),
            actor="质量管理员",
            reason="首次修复",
        )
        finalized_at = database.rows[0]["finalized_at"]
        second = store.repair_finalize_cycle(
            "user@example.com",
            self.cycle_start(7),
            actor="质量管理员",
            reason="重复修复",
        )
        self.assertEqual(first.status.value, "finalized")
        self.assertEqual(second.status.value, "idempotent")
        self.assertEqual(database.rows[0]["finalized_at"], finalized_at)

    def test_final_row_without_finalized_at_is_repaired_not_idempotent(self):
        store, database, _ = self.make_store()
        row = database.add_snapshot(
            self.snapshot(collected_day=5),
            is_cycle_final=1,
            final_source="periodic_fallback",
            finalized_at=None,
        )
        result = store.repair_finalize_cycle(
            "user@example.com",
            self.cycle_start(7),
            actor="质量管理员",
            reason="修复 MySQL 5.7 异常最终态",
        )
        self.assertEqual(result.status.value, "finalized")
        self.assertIsNotNone(result.finalized_at)
        self.assertIsNotNone(row["finalized_at"])

    def test_refinalization_leaves_exactly_one_final_row(self):
        store, database, _ = self.make_store()
        first = database.add_snapshot(
            self.snapshot(collected_day=5, slot_hour=1),
            is_cycle_final=1,
            final_source="periodic_fallback",
            finalized_at=datetime(2026, 8, 2),
        )
        second = database.add_snapshot(
            self.snapshot(collected_day=8, slot_hour=2),
            is_cycle_final=1,
            final_source="periodic_fallback",
            finalized_at=datetime(2026, 8, 2),
        )
        store.repair_finalize_cycle(
            "user@example.com",
            self.cycle_start(7),
            actor="质量管理员",
            reason="纠正重复最终记录",
        )
        finals = [row for row in database.rows if row["is_cycle_final"]]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0]["id"], second["id"])
        self.assertEqual(first["is_cycle_final"], 0)

    def test_late_old_cycle_is_rejected_without_write(self):
        store, database, pool = self.make_store()
        database.add_snapshot(self.snapshot(month=8))
        with self.assertRaises(StaleCycleWriteError):
            store.reconcile_and_write(self.snapshot(month=7))
        self.assertEqual(len(database.rows), 1)
        self.assertEqual(pool.connections[0].rollbacks, 1)
        self.assertEqual(pool.connections[0].commits, 0)

    def test_public_upsert_cannot_bypass_stale_cycle_rejection(self):
        store, database, _ = self.make_store()
        database.add_snapshot(self.snapshot(month=8))
        with self.assertRaises(StaleCycleWriteError):
            store.upsert_same_cycle(self.snapshot(month=7))
        self.assertEqual(len(database.rows), 1)

    def test_finalize_then_insert_failure_rolls_back_single_transaction(self):
        store, database, pool = self.make_store()
        database.add_snapshot(self.snapshot(collected_day=5))
        database.fail_once_contains = "insert into cursor_usage_snapshot"
        database.fail_error = RuntimeError("模拟新周期插入失败")
        with self.assertRaisesRegex(RuntimeError, "模拟新周期插入失败"):
            store.reconcile_and_write(self.snapshot(month=8))
        self.assertEqual(len(pool.connections), 1)
        self.assertEqual(pool.connections[0].rollbacks, 1)
        self.assertEqual(pool.connections[0].commits, 0)

    def test_final_mark_failure_prevents_new_cycle_insert(self):
        store, database, pool = self.make_store()
        database.add_snapshot(self.snapshot(collected_day=5))
        database.fail_once_contains = "is_cycle_final = 1"
        database.fail_error = RuntimeError("模拟最终记录标记失败")
        with self.assertRaisesRegex(RuntimeError, "模拟最终记录标记失败"):
            store.reconcile_and_write(self.snapshot(month=8))
        self.assertFalse(
            any(
                row["billing_cycle_start"] == self.cycle_start(8).replace(
                    tzinfo=None
                )
                for row in database.rows
            )
        )
        self.assertEqual(pool.connections[0].rollbacks, 1)
        self.assertEqual(pool.connections[0].commits, 0)

    def test_deadlock_retries_complete_reconcile_transaction(self):
        store, database, pool = self.make_store()
        database.add_snapshot(self.snapshot(collected_day=5))
        database.fail_once_contains = "set is_cycle_final = 0"
        database.fail_error = pymysql.err.OperationalError(1213, "模拟死锁")
        result = store.reconcile_and_write(self.snapshot(month=8))
        self.assertEqual(result.write_result, WriteResult.INSERTED)
        self.assertEqual(len(pool.connections), 2)
        self.assertEqual(pool.connections[0].rollbacks, 1)
        self.assertEqual(pool.connections[1].commits, 1)

    def test_repair_validates_parameters_and_existing_cycle(self):
        store, _, _ = self.make_store()
        valid_start = self.cycle_start(7)
        invalid_cases = (
            (" User@example.com", valid_start, "操作者", "原因"),
            ("user@example.com", valid_start.replace(tzinfo=None), "操作者", "原因"),
            ("user@example.com", valid_start, "", "原因"),
            ("user@example.com", valid_start, "操作者", "  "),
        )
        for email, cycle_start, actor, reason in invalid_cases:
            with self.subTest(
                email=email,
                cycle_start=cycle_start,
                actor=actor,
                reason=reason,
            ):
                with self.assertRaises(ValueError):
                    store.repair_finalize_cycle(
                        email,
                        cycle_start,
                        actor=actor,
                        reason=reason,
                    )
        with self.assertRaisesRegex(ValueError, "不存在"):
            store.repair_finalize_cycle(
                "user@example.com",
                valid_start,
                actor="操作者",
                reason="原因",
            )

    def test_repair_locks_account_first_and_returns_audit_context(self):
        store, database, _ = self.make_store()
        database.add_snapshot(self.snapshot(month=7))
        actor = "  值班管理员  "
        reason = " 修复异常最终记录 "
        result = store.repair_finalize_cycle(
            "user@example.com",
            self.cycle_start(7),
            actor=actor,
            reason=reason,
        )
        self.assertEqual(result.audit_actor, actor)
        self.assertEqual(result.audit_reason, reason)
        sql_calls = [
            " ".join(sql.split()).lower()
            for sql, _ in database.calls
        ]
        account_lock_index = next(
            index
            for index, sql in enumerate(sql_calls)
            if sql.startswith("select id from cursor_accounts")
        )
        cycle_lock_index = next(
            index
            for index, sql in enumerate(sql_calls)
            if sql.startswith(
                "select id, snapshot_type, billing_cycle_end"
            )
        )
        self.assertEqual(account_lock_index, 0)
        self.assertLess(account_lock_index, cycle_lock_index)
        self.assertFalse(
            any(
                actor in str(params) or reason in str(params)
                for _, params in database.calls
            )
        )

    def test_repair_rejects_missing_master_account_without_leaking_reason(self):
        store, database, _ = self.make_store()
        database.account_exists = False
        sensitive_reason = "敏感修复原因"
        with self.assertRaises(ValueError) as captured:
            store.repair_finalize_cycle(
                "user@example.com",
                self.cycle_start(7),
                actor="管理员",
                reason=sensitive_reason,
            )
        self.assertIn("主数据账号不存在", str(captured.exception))
        self.assertNotIn(sensitive_reason, str(captured.exception))

    def test_normal_reconcile_finalize_result_has_no_audit_context(self):
        store, database, _ = self.make_store()
        database.add_snapshot(self.snapshot(month=7))
        result = store.reconcile_and_write(self.snapshot(month=8))
        self.assertIsNone(result.finalize_result.audit_actor)
        self.assertIsNone(result.finalize_result.audit_reason)

    def test_repair_only_modifies_requested_cycle(self):
        store, database, _ = self.make_store()
        old = database.add_snapshot(self.snapshot(month=7))
        newer = database.add_snapshot(self.snapshot(month=8))
        store.repair_finalize_cycle(
            "user@example.com",
            self.cycle_start(7),
            actor="质量管理员",
            reason="仅修复指定周期",
        )
        self.assertEqual(old["is_cycle_final"], 1)
        self.assertEqual(newer["is_cycle_final"], 0)

    def test_list_final_cycles_uses_fixed_descending_query(self):
        store, database, _ = self.make_store()
        july = database.add_snapshot(
            self.snapshot(month=7),
            is_cycle_final=1,
            final_source="periodic_fallback",
            finalized_at=datetime(2026, 8, 2),
        )
        august = database.add_snapshot(
            self.snapshot(month=8),
            is_cycle_final=1,
            final_source="periodic_fallback",
            finalized_at=datetime(2026, 9, 2),
        )
        rows = store.list_final_cycles("user@example.com")
        self.assertEqual(
            [row["id"] for row in rows],
            [august["id"], july["id"]],
        )
        sql = database.calls[-1][0]
        self.assertIn("is_cycle_final = 1", sql)
        self.assertIn("billing_cycle_end DESC", sql)


class UsageSnapshotStorePoolTests(unittest.TestCase):
    def test_without_injected_pool_reuses_ledger_database_settings(self):
        settings = type(
            "Settings",
            (),
            {
                "ledger_db_host": "mysql.example.com",
                "ledger_db_port": 3307,
                "ledger_db_user": "cam",
                "ledger_db_password": "secret",
                "ledger_db_name": "aicoding",
                "ledger_db_pool_max_connections": 9,
                "ledger_db_pool_min_cached": 2,
                "ledger_db_pool_max_cached": 5,
                "ledger_db_connect_timeout_sec": 11,
                "ledger_db_read_timeout_sec": 31,
                "ledger_db_write_timeout_sec": 32,
                "ledger_db_connect_retry_times": 1,
                "ledger_db_connect_retry_backoff_sec": 0,
            },
        )()
        with patch("cam.usage_snapshot_store.PooledDB") as pooled:
            UsageSnapshotStore(settings=settings)
        kwargs = pooled.call_args.kwargs
        self.assertEqual(kwargs["host"], "mysql.example.com")
        self.assertEqual(kwargs["port"], 3307)
        self.assertEqual(kwargs["user"], "cam")
        self.assertEqual(kwargs["password"], "secret")
        self.assertEqual(kwargs["database"], "aicoding")
        self.assertFalse(kwargs["autocommit"])


if __name__ == "__main__":
    unittest.main()
