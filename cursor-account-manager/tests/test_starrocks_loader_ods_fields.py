import sys
import types
import unittest
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

pymysql_mod = types.ModuleType("pymysql")
pymysql_mod.connections = types.SimpleNamespace(Connection=object)
pymysql_mod.cursors = types.SimpleNamespace(Cursor=object)
sys.modules.setdefault("pymysql", pymysql_mod)
dbutils_mod = types.ModuleType("dbutils")
pooled_db_mod = types.ModuleType("dbutils.pooled_db")
pooled_db_mod.PooledDB = object
sys.modules.setdefault("dbutils", dbutils_mod)
sys.modules.setdefault("dbutils.pooled_db", pooled_db_mod)

from cam.starrocks_loader import StarRocksLoader


class FakeCursor:
    def __init__(self):
        self.executed = []
        self.executemany_sql = ""
        self.executemany_rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def executemany(self, sql, rows):
        self.executemany_sql = sql
        self.executemany_rows = rows


class FakeConnection:
    def __init__(self):
        self.cursor_obj = FakeCursor()
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


class FakePool:
    def __init__(self, connection):
        self.connection_obj = connection
        self.connection_calls = 0

    def connection(self):
        self.connection_calls += 1
        return self.connection_obj


class StarRocksLoaderOdsFieldTests(unittest.TestCase):
    def test_create_pool_uses_configured_connection_pool_params(self):
        loader = object.__new__(StarRocksLoader)
        loader.host = "sr.example.com"
        loader.port = 9030
        loader.db = "dataeye_aiboard"
        loader.username = "user"
        loader.password = "pw"
        loader.connect_timeout_sec = 10
        loader.read_timeout_sec = 120
        loader.write_timeout_sec = 120
        loader.pool_min_cached = 1
        loader.pool_max_cached = 4
        loader.pool_max_connections = 8
        loader.pool_blocking = True
        loader.pool_ping = 1

        with patch("cam.starrocks_loader.PooledDB") as pooled:
            loader._create_pool()

        _, kwargs = pooled.call_args
        self.assertEqual(kwargs["creator"].__name__, "pymysql")
        self.assertEqual(kwargs["mincached"], 1)
        self.assertEqual(kwargs["maxcached"], 4)
        self.assertEqual(kwargs["maxconnections"], 8)
        self.assertTrue(kwargs["blocking"])
        self.assertEqual(kwargs["ping"], 1)
        self.assertEqual(kwargs["host"], "sr.example.com")
        self.assertEqual(kwargs["connect_timeout"], 10)
        self.assertEqual(kwargs["read_timeout"], 120)
        self.assertEqual(kwargs["write_timeout"], 120)

    def test_conn_retries_pool_connection_and_returns_to_pool(self):
        loader = object.__new__(StarRocksLoader)
        loader.host = "sr.example.com"
        loader.port = 9030
        loader.db = "dataeye_aiboard"
        loader.connect_retry_times = 2
        loader.connect_retry_backoff_sec = 0
        conn = FakeConnection()
        pool = types.SimpleNamespace()
        attempts = {"count": 0}

        def flaky_connection():
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise OSError("temporary connect timeout")
            return conn

        pool.connection = flaky_connection
        loader._pool = pool

        with loader._conn() as got:
            self.assertIs(got, conn)

        self.assertEqual(attempts["count"], 2)

    def test_replace_ods_rows_for_account_inserts_feishu_email_and_plan_amount(self):
        loader = object.__new__(StarRocksLoader)
        loader.db = "dataeye_aiboard"
        conn = FakeConnection()

        @contextmanager
        def fake_conn():
            yield conn

        row = {
            "dt": "2026-05-13",
            "run_id": "run1",
            "account_email": "cursor@example.com",
            "feishu_email": "owner@example.com",
            "plan_amount": Decimal("20"),
            "event_time": datetime(2026, 5, 13, 1, 0, 0),
            "kind": "Usage",
            "model_name": "gpt-4",
            "max_mode": "No",
            "input_tokens_wo_cache_write": 1,
            "input_tokens_w_cache_write": 2,
            "output_tokens": 3,
            "total_tokens": 6,
            "cost": "$0.10",
            "raw_event_json": "{}",
        }

        with (
            patch.object(loader, "_conn", fake_conn),
            patch.object(loader, "_ensure_date_partition", lambda **_kwargs: None),
        ):
            loader.replace_ods_rows_for_account(
                biz_date="2026-05-13",
                account_email="cursor@example.com",
                rows=[row],
            )

        self.assertIn("feishu_email", conn.cursor_obj.executemany_sql)
        self.assertIn("plan_amount", conn.cursor_obj.executemany_sql)
        self.assertEqual(conn.cursor_obj.executemany_rows[0]["feishu_email"], "owner@example.com")
        self.assertEqual(conn.cursor_obj.executemany_rows[0]["plan_amount"], Decimal("20.00"))

    def test_normalize_ods_row_requires_feishu_email(self):
        loader = object.__new__(StarRocksLoader)
        with self.assertRaisesRegex(ValueError, "feishu_email 不能为空"):
            loader.normalize_decimal_fields({"run_id": "run1", "account_email": "cursor@example.com"})


if __name__ == "__main__":
    unittest.main()
