"""用量监控看板 API 的代表性合同测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from cam.web_server import app


UTC = timezone.utc


def _snapshot(
    email: str,
    *,
    collected_at: datetime,
    used_pct: str = "10.00",
    final: bool = False,
    final_source: str | None = None,
    plan_tier: str = "pro",
    applicant: str = "张三",
    department: str = "研发",
) -> dict:
    """构造数据库 LEFT JOIN 查询返回的一条账号/快照行。"""
    return {
        "email": email,
        "applicant": applicant,
        "department": department,
        "plan_tier": plan_tier,
        "plan_status": "active",
        "billing_cycle_start": datetime(2026, 1, 1, tzinfo=UTC),
        "billing_cycle_end": datetime(2026, 2, 1, tzinfo=UTC),
        "total_used_pct": used_pct,
        "collected_at": collected_at,
        "is_cycle_final": final,
        "final_source": final_source,
    }


class _DashboardStore:
    """返回指定看板行的最小存储替身。"""

    def __init__(self, rows: list[dict] | None = None, error: Exception | None = None):
        self.rows = rows or []
        self.error = error

    def list_usage_dashboard_snapshots(self) -> list[dict]:
        if self.error is not None:
            raise self.error
        return self.rows


class UsageDashboardApiTests(unittest.TestCase):
    """验证看板聚合、过滤、排序及故障响应。"""

    def _get(self, rows: list[dict], path: str = "/api/usage-monitor/dashboard"):
        class _EmptyOutcomeLog:
            def map_latest_usage_collect_outcomes(self, emails):
                return {}

        with patch(
            "cam.web_server.UsageSnapshotStore",
            return_value=_DashboardStore(rows),
        ), patch(
            "cam.web_server.SyncLogStore",
            return_value=_EmptyOutcomeLog(),
        ):
            return TestClient(app).get(path)

    def test_dashboard_sorts_waste_levels_then_usage_then_email(self) -> None:
        """默认顺序严格按 L3 到 UNKNOWN，并按当前用量和邮箱打破平级。"""
        rows = [
            _snapshot("l1@example.com", collected_at=datetime(2026, 1, 3, tzinfo=UTC)),
            _snapshot(
                "l1@example.com",
                collected_at=datetime(2026, 2, 3, tzinfo=UTC),
                final=True,
                final_source="pre_reset",
            ),
            _snapshot("l0@example.com", collected_at=datetime(2026, 1, 2, tzinfo=UTC), used_pct="60.00"),
            _snapshot(
                "l0@example.com",
                collected_at=datetime(2026, 2, 2, tzinfo=UTC),
                used_pct="60.00",
                final=True,
                final_source="pre_reset",
            ),
            {"email": "unknown@example.com", "applicant": "王五", "department": "产品"},
        ]

        response = self._get(rows)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            [row["email"] for row in payload["rows"]],
            ["l1@example.com", "l0@example.com", "unknown@example.com"],
        )
        self.assertEqual(payload["summary"], {"total": 3, "l3": 0, "l2": 0, "l1": 1, "l0": 1, "unknown": 1})
        self.assertIn("low_threshold_pct", payload)

    def test_dashboard_filters_by_query_department_and_waste_level(self) -> None:
        """q 模糊匹配人员字段，部门和浪费等级应用精确过滤。"""
        rows = [
            _snapshot("dev@example.com", collected_at=datetime(2026, 1, 3, tzinfo=UTC), applicant="张三", department="研发"),
            _snapshot("dev@example.com", collected_at=datetime(2026, 2, 3, tzinfo=UTC), final=True, final_source="pre_reset", applicant="张三", department="研发"),
            _snapshot("ops@example.com", collected_at=datetime(2026, 1, 3, tzinfo=UTC), applicant="李四", department="运维"),
            _snapshot("ops@example.com", collected_at=datetime(2026, 2, 3, tzinfo=UTC), final=True, final_source="pre_reset", applicant="李四", department="运维"),
        ]

        response = self._get(
            rows,
            "/api/usage-monitor/dashboard?q=%E5%BC%A0&department=%E7%A0%94%E5%8F%91&waste_level=l1",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["email"] for row in response.json()["rows"]], ["dev@example.com"])

    def test_dashboard_keeps_account_without_snapshot_as_unknown(self) -> None:
        """LEFT JOIN 空快照账号仍返回 UNKNOWN，并保留人员字段。"""
        response = self._get(
            [{"email": "new@example.com", "applicant": "赵六", "department": "销售"}]
        )

        self.assertEqual(response.status_code, 200)
        row = response.json()["rows"][0]
        self.assertEqual(row["waste_level"], "unknown")
        self.assertEqual(row["data_quality_status"], "unknown")
        self.assertEqual(row["applicant"], "赵六")
        self.assertIsNone(row["collected_at"])
        self.assertEqual(row["reason"], "暂无用量快照（尚未采集成功）")

    def test_dashboard_reason_uses_latest_collect_failure_when_no_snapshot(self) -> None:
        """无快照时 reason 带最近一次用量采集失败信息，供数据状态悬停。"""
        from cam.sync_log_store import UsageCollectOutcome

        class _OutcomeLog:
            def map_latest_usage_collect_outcomes(self, emails):
                return {
                    "fail@example.com": UsageCollectOutcome(
                        email="fail@example.com",
                        status="failed",
                        error_message="401 unauthorized",
                        ended_at=1,
                    )
                }

        with patch("cam.web_server.UsageSnapshotStore", return_value=_DashboardStore(
            [{"email": "fail@example.com", "applicant": "甲", "department": "研发"}]
        )), patch("cam.web_server.SyncLogStore", return_value=_OutcomeLog()):
            response = TestClient(app).get("/api/usage-monitor/dashboard")

        self.assertEqual(response.status_code, 200)
        row = response.json()["rows"][0]
        self.assertEqual(row["reason"], "采集失败：401 unauthorized")

    def test_dashboard_reason_uses_latest_collect_skip_when_no_snapshot(self) -> None:
        """无快照且最近采集被跳过时，reason 说明跳过原因。"""
        from cam.sync_log_store import UsageCollectOutcome

        class _OutcomeLog:
            def map_latest_usage_collect_outcomes(self, emails):
                return {
                    "skip@example.com": UsageCollectOutcome(
                        email="skip@example.com",
                        status="skipped",
                        error_message="auth_circuit_open",
                        ended_at=2,
                    )
                }

        with patch("cam.web_server.UsageSnapshotStore", return_value=_DashboardStore(
            [{"email": "skip@example.com", "applicant": "乙", "department": "研发"}]
        )), patch("cam.web_server.SyncLogStore", return_value=_OutcomeLog()):
            response = TestClient(app).get("/api/usage-monitor/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["rows"][0]["reason"],
            "采集跳过：auth_circuit_open",
        )

    def test_dashboard_aggregates_current_and_latest_final_fields(self) -> None:
        """最新采集快照充当当前值，最新最终快照补充结算字段。"""
        rows = [
            _snapshot(
                "user@example.com",
                collected_at=datetime(2026, 2, 5, tzinfo=UTC),
                used_pct="40.00",
            ),
            _snapshot(
                "user@example.com",
                collected_at=datetime(2026, 2, 1, tzinfo=UTC),
                used_pct="20.00",
                final=True,
                final_source="periodic_fallback",
            ),
        ]

        response = self._get(rows)

        self.assertEqual(response.status_code, 200)
        row = response.json()["rows"][0]
        self.assertEqual(row["current_used_pct"], 40.0)
        self.assertEqual(row["latest_final_used_pct"], 20.0)
        self.assertEqual(row["latest_final_source"], "periodic_fallback")
        self.assertEqual(row["latest_cycle_start"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(row["latest_cycle_end"], "2026-02-01T00:00:00+00:00")
        self.assertEqual(row["plan_tier"], "pro")
        self.assertEqual(row["collected_at"], "2026-02-05T00:00:00+00:00")

    def test_dashboard_returns_recoverable_503_without_database_secrets(self) -> None:
        """数据源故障只暴露可恢复中文提示，不回传连接字符串或密码。"""
        with patch(
            "cam.web_server.UsageSnapshotStore",
            return_value=_DashboardStore(error=RuntimeError("password=secret@db")),
        ):
            response = TestClient(app).get("/api/usage-monitor/dashboard")

        self.assertEqual(response.status_code, 503)
        self.assertIn("请稍后重试", response.json()["detail"])
        self.assertNotIn("secret", response.text)


class _CyclesStore:
    """单账号历史账期接口的最小存储替身。"""

    def __init__(
        self,
        *,
        account: dict | None = None,
        latest: dict | None = None,
        finals: list[dict] | None = None,
        error: Exception | None = None,
    ):
        self.account = account
        self.latest = latest
        self.finals = finals or []
        self.error = error

    def get_monitor_account(self, email: str) -> dict | None:
        if self.error is not None:
            raise self.error
        if self.account is None:
            return None
        if str(self.account.get("email") or "").lower() != email.lower():
            return None
        return self.account

    def get_latest_snapshot(self, email: str) -> dict | None:
        if self.error is not None:
            raise self.error
        return self.latest

    def list_final_cycles(self, email: str) -> list[dict]:
        if self.error is not None:
            raise self.error
        return list(self.finals)


class UsageAccountCyclesApiTests(unittest.TestCase):
    """验证单账号历史账期接口。"""

    def _get(self, store: _CyclesStore, email: str = "user@example.com"):
        with patch("cam.web_server.UsageSnapshotStore", return_value=store):
            return TestClient(app).get(
                f"/api/usage-monitor/accounts/{email}/cycles"
            )

    def test_cycles_returns_current_and_final_history(self) -> None:
        """返回当前账期、完整账期历史，并按阈值标记偏低。"""
        store = _CyclesStore(
            account={
                "email": "user@example.com",
                "applicant": "张三",
                "department": "研发",
            },
            latest={
                "plan_tier": "pro",
                "billing_cycle_start": datetime(2026, 7, 1, tzinfo=UTC),
                "billing_cycle_end": datetime(2026, 8, 1, tzinfo=UTC),
                "total_used_pct": "12.50",
                "collected_at": datetime(2026, 7, 15, tzinfo=UTC),
                "is_cycle_final": 0,
            },
            finals=[
                {
                    "plan_tier": "pro",
                    "billing_cycle_start": datetime(2026, 6, 1, tzinfo=UTC),
                    "billing_cycle_end": datetime(2026, 7, 1, tzinfo=UTC),
                    "total_used_pct": "8.00",
                    "final_source": "pre_reset",
                    "finalized_at": datetime(2026, 7, 1, 1, tzinfo=UTC),
                }
            ],
        )

        response = self._get(store)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["email"], "user@example.com")
        self.assertEqual(payload["applicant"], "张三")
        self.assertEqual(float(payload["current"]["used_pct"]), 12.5)
        self.assertFalse(payload["current"]["is_final"])
        self.assertEqual(len(payload["finals"]), 1)
        self.assertTrue(payload["finals"][0]["is_low"])
        self.assertEqual(payload["finals"][0]["final_source"], "pre_reset")
        self.assertEqual(payload["waste_level"], "l1")
        self.assertEqual(payload["low_usage_streak"], 1)
        self.assertEqual(float(payload["low_threshold_pct"]), 30.0)

    def test_cycles_empty_finals_when_only_current_snapshot(self) -> None:
        """仅有当前快照时 finals 为空，等级为待确认。"""
        store = _CyclesStore(
            account={"email": "user@example.com", "applicant": "", "department": ""},
            latest={
                "plan_tier": "pro",
                "billing_cycle_start": datetime(2026, 7, 1, tzinfo=UTC),
                "billing_cycle_end": datetime(2026, 8, 1, tzinfo=UTC),
                "total_used_pct": "5.00",
                "collected_at": datetime(2026, 7, 15, tzinfo=UTC),
                "is_cycle_final": 0,
            },
            finals=[],
        )

        response = self._get(store)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["finals"], [])
        self.assertEqual(payload["waste_level"], "unknown")
        self.assertTrue(
            "完整账期" in payload["reason"] or "最终用量" in payload["reason"]
        )

    def test_cycles_returns_404_for_missing_account(self) -> None:
        """主数据不存在时返回 404。"""
        response = self._get(_CyclesStore(account=None))
        self.assertEqual(response.status_code, 404)
        self.assertIn("不存在", response.json()["detail"])

    def test_cycles_returns_503_without_database_secrets(self) -> None:
        """数据库故障不回传密码等敏感信息。"""
        response = self._get(
            _CyclesStore(error=RuntimeError("password=secret@db"))
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("请稍后重试", response.json()["detail"])
        self.assertNotIn("secret", response.text)


class UsageAccountCollectApiTests(unittest.TestCase):
    """验证单账号强制采集接口。"""

    def test_collect_success_returns_dashboard_row(self) -> None:
        from cam.usage_snapshot_models import CollectionStatus

        row = {
            "email": "user@example.com",
            "applicant": "张三",
            "department": "研发",
            "waste_level": "unknown",
            "current_used_pct": "15.00",
        }
        success = type(
            "R",
            (),
            {
                "email": "user@example.com",
                "status": CollectionStatus.SUCCESS,
                "error_message": "",
            },
        )()

        with (
            patch(
                "cam.web_server.run_usage_manual_collect",
                return_value=success,
            ),
            patch(
                "cam.web_server._load_usage_dashboard_row_for_email",
                return_value=row,
            ),
        ):
            response = TestClient(app).post(
                "/api/usage-monitor/accounts/user@example.com/collect"
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["row"]["email"], "user@example.com")

    def test_collect_maps_lock_busy_to_409(self) -> None:
        from cam.usage_snapshot_models import CollectionStatus

        busy = type(
            "R",
            (),
            {
                "email": "user@example.com",
                "status": CollectionStatus.LOCK_BUSY,
                "error_message": "该账号正在采集，请稍后",
            },
        )()
        with patch("cam.web_server.run_usage_manual_collect", return_value=busy):
            response = TestClient(app).post(
                "/api/usage-monitor/accounts/user@example.com/collect"
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn("正在采集", response.json()["detail"])

    def test_collect_maps_not_collectable_to_404(self) -> None:
        from cam.usage_snapshot_models import CollectionStatus

        missing = type(
            "R",
            (),
            {
                "email": "missing@example.com",
                "status": CollectionStatus.NOT_COLLECTABLE,
                "error_message": "账号不存在或无法采集",
            },
        )()
        with patch(
            "cam.web_server.run_usage_manual_collect", return_value=missing
        ):
            response = TestClient(app).post(
                "/api/usage-monitor/accounts/missing@example.com/collect"
            )
        self.assertEqual(response.status_code, 404)

    def test_collect_maps_auth_circuit_to_503(self) -> None:
        from cam.usage_snapshot_models import CollectionStatus

        open_breaker = type(
            "R",
            (),
            {
                "email": "user@example.com",
                "status": CollectionStatus.AUTH_CIRCUIT_OPEN,
                "error_message": "熔断开启",
            },
        )()
        with patch(
            "cam.web_server.run_usage_manual_collect",
            return_value=open_breaker,
        ):
            response = TestClient(app).post(
                "/api/usage-monitor/accounts/user@example.com/collect"
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn("熔断", response.json()["detail"])

    def test_collect_maps_failed_to_502(self) -> None:
        from cam.usage_snapshot_models import CollectionStatus

        failed = type(
            "R",
            (),
            {
                "email": "user@example.com",
                "status": CollectionStatus.FAILED,
                "error_message": "网络错误",
            },
        )()
        with patch(
            "cam.web_server.run_usage_manual_collect", return_value=failed
        ):
            response = TestClient(app).post(
                "/api/usage-monitor/accounts/user@example.com/collect"
            )
        self.assertEqual(response.status_code, 502)
        self.assertIn("网络错误", response.json()["detail"])
        self.assertNotIn("password=", response.text)


if __name__ == "__main__":
    unittest.main()
