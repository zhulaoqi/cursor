import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.modules.setdefault("requests", types.ModuleType("requests"))
patchright_mod = types.ModuleType("patchright")
patchright_sync_api = types.ModuleType("patchright.sync_api")
patchright_sync_api.Page = object
patchright_sync_api.Playwright = object
patchright_sync_api.sync_playwright = lambda: None
sys.modules.setdefault("patchright", patchright_mod)
sys.modules.setdefault("patchright.sync_api", patchright_sync_api)

from fastapi.testclient import TestClient

from cam.plan_scraper import PlanInfo, SpendingPanelInfo
from decimal import Decimal
from cam.token_store import TokenStore
from cam.web_server import _format_on_demand_alert_table, app


class AccountRefreshSpendingTests(unittest.TestCase):
    def test_on_demand_alert_table_format(self):
        body = _format_on_demand_alert_table([
            ("a@x.com", "fs@y.com"),
            ("b@x.com", ""),
        ])
        self.assertIn("| # | 账号邮箱 | 飞书邮箱 |", body)
        self.assertIn("| 1 | a@x.com | fs@y.com |", body)
        self.assertIn("| 2 | b@x.com | — |", body)
        self.assertIn("**共 2 个账号**", body)

    def test_refresh_spending_updates_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TokenStore(Path(tmp) / "tokens.db")
            store.upsert_account(
                email="cursor@example.com",
                imap_password="pw",
                imap_host="imap.feishu.cn",
                imap_port=993,
                feishu_email="owner@example.com",
                source="upload",
            )
            with (
                patch("cam.web_server.get_default_store", return_value=store),
                patch(
                    "cam.web_server.fetch_spending_panel_from_dashboard",
                    return_value=SpendingPanelInfo(
                        plan_name="Ultra",
                        on_demand_enabled=False,
                        error="",
                        plan_snapshot=PlanInfo(status="active", amount=Decimal("200"), error=""),
                    ),
                ),
            ):
                res = TestClient(app).post(
                    "/api/accounts/refresh-spending",
                    json={"emails": ["cursor@example.com"]},
                )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["results"][0]["plan_name"], "Ultra")
            self.assertFalse(data["results"][0]["on_demand_enabled"])
            self.assertFalse(data["results"][0].get("on_demand_historical"))
            row = store.get_account("cursor@example.com")
            self.assertEqual(row["plan_name"], "Ultra")
            self.assertEqual(row["on_demand_enabled"], 0)
            self.assertEqual(row.get("on_demand_historical"), 0)
            self.assertEqual(row["plan_status"], "active")
            self.assertEqual(row["plan_amount"], "200")

    def test_refresh_spending_historical_on_demand_triggers_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TokenStore(Path(tmp) / "tokens.db")
            store.upsert_account(
                email="hist@example.com",
                imap_password="pw",
                imap_host="imap.feishu.cn",
                imap_port=993,
                feishu_email="fs@example.com",
                source="upload",
            )
            with (
                patch("cam.web_server.get_default_store", return_value=store),
                patch("cam.web_server.send_alert") as mock_alert,
                patch(
                    "cam.web_server.fetch_spending_panel_from_dashboard",
                    return_value=SpendingPanelInfo(
                        plan_name="Pro",
                        on_demand_enabled=False,
                        error="",
                        plan_snapshot=PlanInfo(status="active", amount=Decimal("20"), error=""),
                        on_demand_historical=True,
                    ),
                ),
            ):
                res = TestClient(app).post(
                    "/api/accounts/refresh-spending",
                    json={"emails": ["hist@example.com"]},
                )
            self.assertEqual(res.status_code, 200)
            mock_alert.assert_called_once()
            args, _kwargs = mock_alert.call_args
            self.assertIn("hist@example.com", args[1])
            self.assertIn("fs@example.com", args[1])
            self.assertIn("曾有按需消费", args[1])

    def test_refresh_spending_on_demand_triggers_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TokenStore(Path(tmp) / "tokens.db")
            store.upsert_account(
                email="cursor@example.com",
                imap_password="pw",
                imap_host="imap.feishu.cn",
                imap_port=993,
                feishu_email="owner@example.com",
                source="upload",
            )
            with (
                patch("cam.web_server.get_default_store", return_value=store),
                patch("cam.web_server.send_alert") as mock_alert,
                patch(
                    "cam.web_server.fetch_spending_panel_from_dashboard",
                    return_value=SpendingPanelInfo(
                        plan_name="Pro",
                        on_demand_enabled=True,
                        error="",
                        plan_snapshot=PlanInfo(status="active", amount=Decimal("20"), error=""),
                    ),
                ),
            ):
                res = TestClient(app).post(
                    "/api/accounts/refresh-spending",
                    json={"emails": ["cursor@example.com"]},
                )
            self.assertEqual(res.status_code, 200)
            mock_alert.assert_called_once()
            args, kwargs = mock_alert.call_args
            self.assertIn("On-demand", args[0])
            self.assertIn("cursor@example.com", args[1])
            self.assertIn("owner@example.com", args[1])
            self.assertIn("|", args[1])
            self.assertEqual(kwargs.get("level"), "on_demand")

    def test_refresh_spending_on_demand_alert_when_enabled_is_int_one(self):
        """整型 1 与 True 等价，须计入按需开启告警（避免 1 is True 为假漏发）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = TokenStore(Path(tmp) / "tokens.db")
            store.upsert_account(
                email="odint@example.com",
                imap_password="pw",
                imap_host="imap.feishu.cn",
                imap_port=993,
                feishu_email="owner@example.com",
                source="upload",
            )
            with (
                patch("cam.web_server.get_default_store", return_value=store),
                patch("cam.web_server.send_alert") as mock_alert,
                patch(
                    "cam.web_server.fetch_spending_panel_from_dashboard",
                    return_value=SpendingPanelInfo(
                        plan_name="Pro",
                        on_demand_enabled=1,  # type: ignore[arg-type]
                        error="",
                        plan_snapshot=PlanInfo(status="active", amount=Decimal("20"), error=""),
                    ),
                ),
            ):
                res = TestClient(app).post(
                    "/api/accounts/refresh-spending",
                    json={"emails": ["odint@example.com"]},
                )
            self.assertEqual(res.status_code, 200)
            mock_alert.assert_called_once()
            args, kwargs = mock_alert.call_args
            self.assertIn("按需", args[1] or "")
            self.assertIn("odint@example.com", args[1])
            self.assertEqual(kwargs.get("level"), "on_demand")

    def test_spending_refresh_busy_endpoint(self):
        res = TestClient(app).get("/api/accounts/spending-refresh-busy")
        self.assertEqual(res.status_code, 200)
        self.assertIn("busy", res.json())
        self.assertIsInstance(res.json()["busy"], bool)

    def test_spending_refresh_status_endpoint(self):
        res = TestClient(app).get("/api/accounts/spending-refresh-status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("busy", data)
        self.assertIn("progress", data)
        self.assertIn("percent", data["progress"])
        self.assertFalse(data["progress"]["running"])

    def test_spending_refresh_busy_true_when_lock_not_acquired(self):
        def fake_try_lock(_path):
            @contextmanager
            def cm():
                yield False

            return cm()

        with patch("cam.web_server._try_lock", side_effect=fake_try_lock):
            res = TestClient(app).get("/api/accounts/spending-refresh-busy")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["busy"])

    def test_refresh_spending_423_when_lock_busy(self):
        def fake_try_lock(_path):
            @contextmanager
            def cm():
                yield False

            return cm()

        with patch("cam.web_server._try_lock", side_effect=fake_try_lock):
            res = TestClient(app).post("/api/accounts/refresh-spending", json={"emails": []})
        self.assertEqual(res.status_code, 423)


if __name__ == "__main__":
    unittest.main()
