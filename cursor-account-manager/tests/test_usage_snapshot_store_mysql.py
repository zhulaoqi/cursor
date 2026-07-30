"""用量快照存储真实 MySQL 集成测试。

仅在 CAM_TEST_MYSQL_HOST、CAM_TEST_MYSQL_PORT、CAM_TEST_MYSQL_USER、
CAM_TEST_MYSQL_PASSWORD、CAM_TEST_MYSQL_DATABASE 全部设置时运行。
配置库名必须包含 test，测试还会创建并最终删除独立临时数据库。
强制矩阵还必须用 CAM_TEST_MYSQL_EXPECTED_VERSION 指定 5.7 或 8.0。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from types import SimpleNamespace
from threading import Event, Lock
import unittest
import uuid

import pymysql

from cam.usage_snapshot_models import SnapshotType, UsageSnapshot
from cam.usage_snapshot_store import (
    StaleCycleWriteError,
    UsageSnapshotStore,
    WriteResult,
)


_ENV_KEYS = (
    "CAM_TEST_MYSQL_HOST",
    "CAM_TEST_MYSQL_PORT",
    "CAM_TEST_MYSQL_USER",
    "CAM_TEST_MYSQL_PASSWORD",
    "CAM_TEST_MYSQL_DATABASE",
)
_ENV = {key: os.environ.get(key, "").strip() for key in _ENV_KEYS}
_MISSING_ENV = tuple(key for key, value in _ENV.items() if not value)
_REQUIRE_MYSQL = os.environ.get("CAM_REQUIRE_MYSQL_TESTS", "").strip() == "1"
_EXPECTED_VERSION = os.environ.get(
    "CAM_TEST_MYSQL_EXPECTED_VERSION",
    "",
).strip()


class _AccountLockCoordinator:
    """控制第一个账号行锁暂停，并观察第二个事务进入锁等待。"""

    def __init__(self):
        self.first_lock_acquired = Event()
        self.release_first = Event()
        self.second_lock_attempted = Event()
        self._lock = Lock()
        self._account_lock_attempts = 0

    def before_account_lock(self):
        with self._lock:
            self._account_lock_attempts += 1
            attempt = self._account_lock_attempts
        if attempt == 2:
            self.second_lock_attempted.set()
        return attempt

    def pause_first_after_lock(self):
        self.first_lock_acquired.set()
        if not self.release_first.wait(timeout=10):
            raise AssertionError("第一事务等待释放账号行锁超时")


class _AccountLockCursor:
    """在真实账号行锁获取后暂停第一事务。"""

    def __init__(self, cursor, coordinator):
        self._cursor = cursor
        self._coordinator = coordinator
        self._pause_after_fetch = False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split()).lower()
        is_account_lock = (
            normalized.startswith("select id from cursor_accounts")
            and "for update" in normalized
        )
        if is_account_lock:
            attempt = self._coordinator.before_account_lock()
            self._pause_after_fetch = attempt == 1
        return self._cursor.execute(sql, params)

    def fetchone(self):
        row = self._cursor.fetchone()
        if self._pause_after_fetch:
            self._pause_after_fetch = False
            if row is None:
                raise AssertionError("竞争测试账号主数据意外不存在")
            self._coordinator.pause_first_after_lock()
        return row

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class _AccountLockConnection:
    """为真实连接返回账号行锁同步游标。"""

    def __init__(self, connection, coordinator):
        self._connection = connection
        self._coordinator = coordinator

    def cursor(self):
        return _AccountLockCursor(
            self._connection.cursor(),
            self._coordinator,
        )

    def __getattr__(self, name):
        return getattr(self._connection, name)


class _AccountLockPool:
    """包装真实池，以 READ COMMITTED 验证账号行锁串行语义。"""

    def __init__(self, pool, coordinator):
        self._pool = pool
        self._coordinator = coordinator

    def connection(self):
        connection = self._pool.connection()
        with connection.cursor() as cursor:
            cursor.execute(
                "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED"
            )
            cursor.execute("SET SESSION innodb_lock_wait_timeout = 3")
        return _AccountLockConnection(connection, self._coordinator)


@unittest.skipIf(
    bool(_MISSING_ENV) and not _REQUIRE_MYSQL,
    "未配置完整 CAM_TEST_MYSQL_*，跳过真实 MySQL 集成测试",
)
class UsageSnapshotStoreMySQLTests(unittest.TestCase):
    """在独立临时数据库中验证真实 MySQL 行为。"""

    @classmethod
    def setUpClass(cls):
        required_missing = list(_MISSING_ENV)
        if _REQUIRE_MYSQL and not _EXPECTED_VERSION:
            required_missing.append("CAM_TEST_MYSQL_EXPECTED_VERSION")
        if required_missing:
            raise AssertionError(
                "CAM_REQUIRE_MYSQL_TESTS=1 时必须完整设置："
                + "、".join(required_missing)
            )
        if _REQUIRE_MYSQL and _EXPECTED_VERSION not in {"5.7", "8.0"}:
            raise AssertionError(
                "CAM_REQUIRE_MYSQL_TESTS=1 时必须将 "
                "CAM_TEST_MYSQL_EXPECTED_VERSION 设置为 5.7 或 8.0"
            )
        if _EXPECTED_VERSION and _EXPECTED_VERSION not in {"5.7", "8.0"}:
            raise AssertionError(
                "CAM_TEST_MYSQL_EXPECTED_VERSION 只能是 5.7 或 8.0"
            )
        configured_database = _ENV["CAM_TEST_MYSQL_DATABASE"]
        if "test" not in configured_database.lower():
            raise AssertionError(
                "CAM_TEST_MYSQL_DATABASE 必须是名称包含 test 的专用测试库"
            )

        cls.admin_connection = pymysql.connect(
            host=_ENV["CAM_TEST_MYSQL_HOST"],
            port=int(_ENV["CAM_TEST_MYSQL_PORT"]),
            user=_ENV["CAM_TEST_MYSQL_USER"],
            password=_ENV["CAM_TEST_MYSQL_PASSWORD"],
            database=configured_database,
            charset="utf8mb4",
            autocommit=True,
        )
        cls.temp_database = f"cam_usage_test_{uuid.uuid4().hex[:20]}"
        try:
            with cls.admin_connection.cursor() as cursor:
                cursor.execute("SELECT VERSION()")
                actual_version = str(cursor.fetchone()[0])
                if (
                    _EXPECTED_VERSION
                    and not actual_version.startswith(
                        f"{_EXPECTED_VERSION}."
                    )
                ):
                    raise AssertionError(
                        "真实 MySQL 版本与 "
                        "CAM_TEST_MYSQL_EXPECTED_VERSION 不匹配："
                        f"期望 {_EXPECTED_VERSION}，实际 {actual_version}"
                    )
                cursor.execute(
                    f"CREATE DATABASE `{cls.temp_database}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )

            settings = SimpleNamespace(
                ledger_db_host=_ENV["CAM_TEST_MYSQL_HOST"],
                ledger_db_port=int(_ENV["CAM_TEST_MYSQL_PORT"]),
                ledger_db_user=_ENV["CAM_TEST_MYSQL_USER"],
                ledger_db_password=_ENV["CAM_TEST_MYSQL_PASSWORD"],
                ledger_db_name=cls.temp_database,
                ledger_db_pool_max_connections=8,
                ledger_db_pool_min_cached=0,
                ledger_db_pool_max_cached=4,
                ledger_db_connect_timeout_sec=10,
                ledger_db_read_timeout_sec=30,
                ledger_db_write_timeout_sec=30,
                ledger_db_connect_retry_times=1,
                ledger_db_connect_retry_backoff_sec=0,
            )
            cls.store = UsageSnapshotStore(settings=settings)
            cls._create_master_tables()
            cls.store.ensure_schema()
        except Exception:
            cls._cleanup_mysql_resources()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._cleanup_mysql_resources()

    @classmethod
    def _cleanup_mysql_resources(cls):
        """关闭连接池并删除本测试创建的临时数据库。"""
        store = getattr(cls, "store", None)
        if store is not None:
            store._pool.close()
        connection = getattr(cls, "admin_connection", None)
        database = getattr(cls, "temp_database", "")
        if connection is not None:
            try:
                if database:
                    with connection.cursor() as cursor:
                        cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
            finally:
                connection.close()

    @classmethod
    def _create_master_tables(cls):
        """创建满足监控契约的最小主数据表。"""
        connection = pymysql.connect(
            host=_ENV["CAM_TEST_MYSQL_HOST"],
            port=int(_ENV["CAM_TEST_MYSQL_PORT"]),
            user=_ENV["CAM_TEST_MYSQL_USER"],
            password=_ENV["CAM_TEST_MYSQL_PASSWORD"],
            database=cls.temp_database,
            charset="utf8mb4",
            autocommit=True,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
CREATE TABLE cursor_accounts (
    id BIGINT NOT NULL AUTO_INCREMENT,
    email VARCHAR(320) NOT NULL,
    applicant VARCHAR(128) NOT NULL DEFAULT '',
    department VARCHAR(128) NOT NULL DEFAULT '',
    PRIMARY KEY (id),
    UNIQUE KEY uk_cursor_accounts_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""
                )
                cursor.execute(
                    """
CREATE TABLE cursor_billing_ledger_summary (
    id BIGINT NOT NULL AUTO_INCREMENT,
    email VARCHAR(320) NOT NULL,
    billing_month VARCHAR(7) NOT NULL,
    net_spend_usd DECIMAL(12,4) NOT NULL DEFAULT 0,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""
                )
                cursor.execute(
                    """
INSERT INTO cursor_accounts (email, applicant, department)
VALUES (%s, %s, %s)
""",
                    ("mysql-test@example.com", "集成测试", "质量保障"),
                )
        finally:
            connection.close()

    def setUp(self):
        with self.store._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM cursor_usage_snapshot")
            connection.commit()

    @staticmethod
    def _snapshot(
        *,
        month=7,
        slot_offset_hours=0,
        microsecond=0,
        collected_day=2,
        snapshot_type=SnapshotType.PERIODIC,
        cycle_end=None,
    ):
        """构造真实 MySQL 测试快照。"""
        cycle_start = datetime(
            2026,
            month,
            1,
            microsecond=microsecond,
            tzinfo=timezone.utc,
        )
        snapshot_slot = cycle_start + timedelta(
            days=1,
            hours=slot_offset_hours,
        )
        if snapshot_type is SnapshotType.PRE_RESET:
            snapshot_slot = cycle_start
        return UsageSnapshot(
            email="mysql-test@example.com",
            plan_tier="pro",
            plan_tier_raw="专业版",
            plan_status="active",
            plan_source="api",
            billing_cycle_start=cycle_start,
            billing_cycle_end=cycle_end
            or datetime(
                    2026,
                    month + 1,
                    1,
                    microsecond=microsecond,
                    tzinfo=timezone.utc,
                ),
            total_used_pct=Decimal("12.34"),
            snapshot_type=snapshot_type,
            snapshot_slot=snapshot_slot,
            collected_at=cycle_start
            + timedelta(
                days=collected_day,
                hours=slot_offset_hours,
                minutes=5,
            ),
            source_endpoint="/api/usage",
            parser_version="mysql-test-v1",
            raw_payload={"来源": "真实 MySQL 集成测试"},
        )

    def _snapshot_count(self):
        """查询当前测试账号快照数。"""
        with self.store._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
SELECT COUNT(*) AS count
FROM cursor_usage_snapshot
WHERE email = %s
""",
                    ("mysql-test@example.com",),
                )
                return int(cursor.fetchone()["count"])

    def _run_account_lock_competition(self, first_operation, second_operation):
        """暂停第一事务账号行锁，并证明第二事务在释放前未完成。"""
        coordinator = _AccountLockCoordinator()
        synchronized_pool = _AccountLockPool(
            self.store._pool,
            coordinator,
        )
        original_pool = self.store._pool
        executor = ThreadPoolExecutor(max_workers=2)
        self.store._pool = synchronized_pool
        try:
            first_future = executor.submit(first_operation)
            self.assertTrue(
                coordinator.first_lock_acquired.wait(timeout=5),
                "第一事务未在 5 秒内锁定 cursor_accounts 行",
            )
            second_future = executor.submit(second_operation)
            self.assertTrue(
                coordinator.second_lock_attempted.wait(timeout=5),
                "第二事务未在 5 秒内尝试账号行锁",
            )
            self.assertFalse(
                second_future.done(),
                "账号行锁释放前第二事务不应完成",
            )
            coordinator.release_first.set()
            done, not_done = wait(
                (first_future, second_future),
                timeout=15,
            )
            if not_done:
                self.fail("账号行锁竞争线程在 15 秒整体超时内未结束")
            self.assertEqual(len(done), 2)
            return first_future, second_future
        finally:
            coordinator.release_first.set()
            self.store._pool = original_pool
            executor.shutdown(wait=False, cancel_futures=True)

    def test_ensure_and_validate_schema(self):
        self.store.ensure_schema()
        self.store.validate_schema()

    def test_same_slot_is_idempotent(self):
        snapshot = self._snapshot()
        self.assertEqual(
            self.store.upsert_same_cycle(snapshot),
            WriteResult.INSERTED,
        )
        self.assertEqual(
            self.store.upsert_same_cycle(snapshot),
            WriteResult.IDEMPOTENT,
        )
        self.assertEqual(self._snapshot_count(), 1)

    def test_different_slots_insert_two_rows(self):
        self.assertEqual(
            self.store.upsert_same_cycle(self._snapshot()),
            WriteResult.INSERTED,
        )
        self.assertEqual(
            self.store.upsert_same_cycle(
                self._snapshot(slot_offset_hours=6)
            ),
            WriteResult.INSERTED,
        )
        self.assertEqual(self._snapshot_count(), 2)

    def test_datetime_fields_round_trip_at_millisecond_precision(self):
        snapshot = self._snapshot(microsecond=123456)
        self.assertEqual(
            self.store.upsert_same_cycle(snapshot),
            WriteResult.INSERTED,
        )
        with self.store._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
SELECT billing_cycle_start, billing_cycle_end,
       snapshot_slot, collected_at
FROM cursor_usage_snapshot
WHERE email = %s
""",
                    ("mysql-test@example.com",),
                )
                row = cursor.fetchone()
        expected_values = {
            field_name: getattr(snapshot, field_name).replace(
                tzinfo=None,
                microsecond=123000,
            )
            for field_name in (
                "billing_cycle_start",
                "billing_cycle_end",
                "snapshot_slot",
                "collected_at",
            )
        }
        for field_name, expected in expected_values.items():
            with self.subTest(field_name=field_name):
                self.assertEqual(row[field_name], expected)

    def test_two_concurrent_first_writes_leave_one_row(self):
        snapshot = self._snapshot()
        futures = self._run_account_lock_competition(
            lambda: self.store.upsert_same_cycle(snapshot),
            lambda: self.store.upsert_same_cycle(snapshot),
        )
        results = tuple(future.result() for future in futures)
        self.assertCountEqual(
            results,
            (WriteResult.INSERTED, WriteResult.IDEMPOTENT),
        )
        self.assertEqual(self._snapshot_count(), 1)

    def test_same_cycle_end_can_be_extended_and_shortened(self):
        first = self._snapshot(month=7, collected_day=2)
        extended_end = datetime(2026, 8, 3, tzinfo=timezone.utc)
        shortened_end = datetime(2026, 8, 2, tzinfo=timezone.utc)
        self.assertEqual(
            self.store.upsert_same_cycle(first),
            WriteResult.INSERTED,
        )
        self.assertEqual(
            self.store.upsert_same_cycle(
                self._snapshot(
                    month=7,
                    collected_day=3,
                    cycle_end=extended_end,
                )
            ),
            WriteResult.UPDATED,
        )
        self.assertEqual(
            self.store.upsert_same_cycle(
                self._snapshot(
                    month=7,
                    collected_day=4,
                    cycle_end=shortened_end,
                )
            ),
            WriteResult.UPDATED,
        )
        with self.store._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
SELECT billing_cycle_end
FROM cursor_usage_snapshot
WHERE email = %s
""",
                    ("mysql-test@example.com",),
                )
                stored_end = cursor.fetchone()["billing_cycle_end"]
        self.assertEqual(stored_end, shortened_end.replace(tzinfo=None))

    def test_new_cycle_finalizes_old_with_periodic_fallback(self):
        self.store.reconcile_and_write(self._snapshot(month=7))
        result = self.store.reconcile_and_write(self._snapshot(month=8))
        self.assertEqual(result.finalize_result.status.value, "finalized")
        self.assertEqual(
            result.finalize_result.final_source.value,
            "periodic_fallback",
        )
        finals = self.store.list_final_cycles("mysql-test@example.com")
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0]["snapshot_type"], "periodic")

    def test_new_cycle_prefers_pre_reset_final(self):
        self.store.reconcile_and_write(self._snapshot(month=7))
        self.store.reconcile_and_write(
            self._snapshot(
                month=7,
                collected_day=25,
                snapshot_type=SnapshotType.PRE_RESET,
            )
        )
        result = self.store.reconcile_and_write(self._snapshot(month=8))
        self.assertEqual(
            result.finalize_result.final_source.value,
            "pre_reset",
        )
        finals = self.store.list_final_cycles("mysql-test@example.com")
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0]["snapshot_type"], "pre_reset")

    def test_late_old_cycle_write_is_rejected(self):
        self.store.reconcile_and_write(self._snapshot(month=8))
        with self.assertRaises(StaleCycleWriteError):
            self.store.reconcile_and_write(self._snapshot(month=7))
        self.assertEqual(self._snapshot_count(), 1)

    def test_finalization_serializes_and_rejects_competing_late_write(self):
        self.store.reconcile_and_write(self._snapshot(month=7))
        new_cycle = self._snapshot(month=8)
        late_old = self._snapshot(
            month=7,
            slot_offset_hours=6,
            collected_day=3,
        )

        def write_late():
            try:
                self.store.reconcile_and_write(late_old)
                return "意外写入"
            except StaleCycleWriteError:
                return "已拒绝迟到写"

        futures = self._run_account_lock_competition(
            lambda: self.store.reconcile_and_write(new_cycle),
            write_late,
        )
        finalization_result = futures[0].result()
        late_result = futures[1].result()

        self.assertEqual(
            finalization_result.finalize_result.status.value,
            "finalized",
        )
        self.assertEqual(late_result, "已拒绝迟到写")
        finals = self.store.list_final_cycles("mysql-test@example.com")
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0]["billing_cycle_start"].month, 7)
        self.assertEqual(self._snapshot_count(), 2)

    def test_two_connections_trigger_only_one_cycle_finalization(self):
        self.store.reconcile_and_write(self._snapshot(month=7))
        first_new = self._snapshot(month=8)
        second_new = self._snapshot(month=8, slot_offset_hours=6)
        futures = self._run_account_lock_competition(
            lambda: self.store.reconcile_and_write(first_new),
            lambda: self.store.reconcile_and_write(second_new),
        )
        results = tuple(future.result() for future in futures)
        finalize_results = [
            result.finalize_result
            for result in results
            if result.finalize_result is not None
        ]
        self.assertEqual(len(finalize_results), 1)
        self.assertEqual(finalize_results[0].status.value, "finalized")
        self.assertCountEqual(
            (result.write_result for result in results),
            (WriteResult.INSERTED, WriteResult.INSERTED),
        )
        finals = self.store.list_final_cycles("mysql-test@example.com")
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0]["billing_cycle_start"].month, 7)
        self.assertEqual(self._snapshot_count(), 3)

    # 真实死锁和连接中断受服务端调度影响，稳定覆盖由单元注入测试承担；
    # 此处只构造可控账号行锁竞争，避免集成测试产生不可恢复的挂起。


if __name__ == "__main__":
    unittest.main()
