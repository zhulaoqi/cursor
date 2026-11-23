"""Devin 平台：解析 / 存储 / 批次 / 播报 / 导入 / 调度 / HTTP 接口。"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from cam import devin_api, devin_runner
from cam.devin_collector import CollectResult
from cam.devin_errors import DevinError, classify_exception
from cam.devin_import import DevinImportError, build_template_xlsx, parse_accounts_file
from cam.devin_parser import (
    extract_from_api,
    has_core_data,
    merge_results,
    parse_date_text,
    parse_reset_seconds,
    parse_usage_text,
)
from cam.devin_scheduler import DevinScheduler, next_run_at
from cam.devin_store import DevinStore
from cam.email_client import IMAPAuthError, VerificationCodeTimeoutError
from cam.web_server import app

NOW = datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc)

USAGE_TEXT = """Usage & limits
Current plan
Max
$200/month
Renews on December 13, 2026
Weekly quota
1% used
Resets in 6 days 4 hours
On-demand usage
$1.50
Remaining balance
$20.00
Message usage limit $20
"""


# ─── 解析 ─────────────────────────────────────────────────────────

def test_parse_usage_text_full_page():
    data = parse_usage_text(USAGE_TEXT, now=NOW)
    assert data["plan_name"] == "Max"
    assert data["plan_price_usd"] == 200.0
    assert data["billing_period"] == "month"
    assert data["renews_at"] == "2026-12-13"
    assert data["weekly_used_pct"] == 1.0
    assert data["weekly_reset_at"] == int(NOW.timestamp()) + 6 * 86400 + 4 * 3600
    assert data["on_demand_used_usd"] == 1.5
    assert data["on_demand_balance_usd"] == 20.0
    assert data["message_limit_usd"] == 20.0


def test_parse_usage_text_remaining_percent_and_empty():
    data = parse_usage_text("Daily quota\n30% remaining\nResets in 5 hours", now=NOW)
    assert data["daily_used_pct"] == 70.0
    assert parse_usage_text("", now=NOW) == {}
    assert not has_core_data({})


@pytest.mark.parametrize(
    "text,expected",
    [
        ("December 13, 2026", "2026-12-13"),
        ("Dec 3 2026", "2026-12-03"),
        ("13 Dec 2026", "2026-12-13"),
        ("2026-12-13", "2026-12-13"),
        ("12/13/2026", "2026-12-13"),
        ("garbage", ""),
    ],
)
def test_parse_date_text(text, expected):
    assert parse_date_text(text, now=NOW) == expected


def test_parse_date_without_year_rolls_forward():
    assert parse_date_text("Jan 5", now=NOW) == "2027-01-05"


def test_parse_reset_seconds():
    assert parse_reset_seconds("Resets in 2 days") == 2 * 86400
    assert parse_reset_seconds("resets in 3 hours 30 minutes") == 3 * 3600 + 1800
    assert parse_reset_seconds("no reset info") is None


def test_extract_from_api_and_merge():
    payloads = [
        {
            "url": "https://api.devin.ai/org/billing/quota/usage",
            "body": {
                "weekly": {"usedPercent": 3.5, "resetAt": "2026-11-29T00:00:00Z"},
                "daily": {"usedPercent": 0},
                "overageBalanceDollars": 12,
            },
        },
        {
            "url": "https://api.devin.ai/org/billing/quota/my-on-demand-usage",
            "body": {"dollarsConsumed": 4.25},
        },
    ]
    api = extract_from_api(payloads)
    assert api["weekly_used_pct"] == 3.5
    assert api["daily_used_pct"] == 0.0
    assert api["on_demand_balance_usd"] == 12.0
    assert api["on_demand_used_usd"] == 4.25
    assert api["weekly_reset_at"] == int(datetime(2026, 11, 29, tzinfo=timezone.utc).timestamp())

    text = parse_usage_text(USAGE_TEXT, now=NOW)
    merged, source = merge_results(api, text)
    assert source == "api+dom"
    assert merged["weekly_used_pct"] == 3.5  # API 优先
    assert merged["plan_name"] == "Max"  # 文本补齐
    assert merge_results({}, text)[1] == "dom"
    assert merge_results(api, {})[1] == "api"


# ─── 错误分类 ─────────────────────────────────────────────────────

def test_classify_exception():
    assert classify_exception(IMAPAuthError("x"))[0] == "imap_auth"
    assert classify_exception(VerificationCodeTimeoutError("x"))[0] == "code_timeout"
    assert classify_exception(DevinError("code_invalid", "m")) == ("code_invalid", "m")
    assert classify_exception(RuntimeError("net::ERR_PROXY_CONNECTION_FAILED"))[0] == "network"
    assert classify_exception(RuntimeError("???"))[0] == "unknown"


# ─── 存储 ─────────────────────────────────────────────────────────

@pytest.fixture()
def store(tmp_path):
    return DevinStore(tmp_path / "t.db")


def _add(store: DevinStore, email: str, **kw):
    store.upsert_account(email, "pwd", "imap.feishu.cn", 993, kw.get("owner", ""), kw.get("note", ""), "test")


def test_store_dashboard_keeps_last_success_and_latest_failure(store):
    _add(store, "a@x.com")
    _add(store, "b@x.com")
    store.add_snapshot(email="a@x.com", ok=True, data={"plan_name": "Max", "weekly_used_pct": 12.0}, source="api")
    store.add_snapshot(email="a@x.com", ok=False, error_code="code_timeout", error_message="boom")
    rows = {r["email"]: r for r in store.dashboard_rows()}
    a = rows["a@x.com"]
    assert a["plan_name"] == "Max" and a["weekly_used_pct"] == 12.0  # 最近一次成功的数据仍展示
    assert a["last_attempt_ok"] is False and a["last_error_code"] == "code_timeout"
    assert rows["b@x.com"]["last_attempt_at"] is None and rows["b@x.com"]["snapshot_id"] is None


def test_store_upsert_status_and_failures(store):
    _add(store, "a@x.com", note="n1")
    store.upsert_account("a@x.com", "new", "h", 1, "", "", "t2")  # 空备注不覆盖
    acc = store.get_account("a@x.com")
    assert acc["imap_password"] == "new" and acc["note"] == "n1"
    assert store.bump_failure("a@x.com") == 1
    store.set_status("a@x.com", "disabled")
    assert store.list_active_accounts() == []
    store.set_status("a@x.com", "active")
    assert store.get_account("a@x.com")["consecutive_failures"] == 0
    with pytest.raises(ValueError):
        store.set_status("a@x.com", "weird")


def test_store_runs_and_stale(store):
    run_id = store.start_run("scheduler", 3)
    assert store.fail_stale_runs() == 1
    run = store.get_run(run_id)
    assert run["status"] == "interrupted"
    rid2 = store.start_run("manual", 1)
    store.finish_run(rid2, status="partial_failed", success=0, failed=1, skipped=0,
                     failure_breakdown={"network": 1}, failures=[{"email": "a", "code": "network"}])
    got = store.get_run(rid2)
    assert got["failure_breakdown"] == {"network": 1} and got["failures_json"][0]["email"] == "a"


# ─── 批次 + 播报 ──────────────────────────────────────────────────

def _fake_collector(outcomes: dict[str, CollectResult]):
    def run(account):
        return outcomes[account["email"]]
    return run


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(devin_runner.time, "sleep", lambda *_: None)
    monkeypatch.setenv("DEVIN_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("DEVIN_CONCURRENCY", "2")


def test_run_collection_counts_breakdown_and_alert(store, monkeypatch):
    for e in ("ok1@x.com", "ok2@x.com", "bad1@x.com", "bad2@x.com", "off@x.com"):
        _add(store, e)
    store.set_status("off@x.com", "disabled")
    outcomes = {
        "ok1@x.com": CollectResult("ok1@x.com", True, data={"plan_name": "Max", "weekly_used_pct": 90.0}, source="api"),
        "ok2@x.com": CollectResult("ok2@x.com", True, data={"plan_name": "Pro", "weekly_used_pct": 10.0}, source="dom"),
        "bad1@x.com": CollectResult("bad1@x.com", False, error_code="code_timeout", error_message="no mail [现场: /tmp/x.png]"),
        "bad2@x.com": CollectResult("bad2@x.com", False, error_code="imap_auth", error_message="auth"),
    }
    sent = []
    monkeypatch.setattr(devin_runner, "send_alert", lambda t, c, level="info": sent.append((t, c, level)))

    summary = devin_runner.run_devin_collection(
        trigger="scheduler", store=store, collector=_fake_collector(outcomes), use_file_lock=False
    )

    assert (summary.total, summary.success, summary.failed, summary.skipped) == (4, 2, 2, 1)
    assert summary.failure_breakdown == {"code_timeout": 1, "imap_auth": 1}
    assert summary.status == "partial_failed"
    assert summary.plan_distribution == {"Max": 1, "Pro": 1}
    assert [h["email"] for h in summary.high_usage_accounts] == ["ok1@x.com"]
    # 落库：成功/失败都有快照，失败不会抹掉成功数据
    rows = {r["email"]: r for r in store.dashboard_rows()}
    assert rows["ok1@x.com"]["weekly_used_pct"] == 90.0
    assert rows["bad1@x.com"]["last_error_code"] == "code_timeout"
    assert store.get_run(summary.run_id)["failed"] == 2
    # 飞书卡片内容
    assert len(sent) == 1
    title, content, level = sent[0]
    assert title == "Devin 每日采集部分失败" and level == "warning"
    assert "account_success=2" in content and "account_failed=2" in content
    assert "邮箱 IMAP 认证失败：1 个" in content and "验证码邮件超时未收到：1 个" in content
    assert "bad1@x.com" in content and "现场" not in content
    assert not devin_runner.is_running()


def test_run_collection_retries_transient_but_not_imap_auth(store):
    _add(store, "t@x.com")
    _add(store, "i@x.com")
    calls: dict[str, int] = {"t@x.com": 0, "i@x.com": 0}

    def collector(account):
        calls[account["email"]] += 1
        if account["email"] == "t@x.com" and calls["t@x.com"] == 1:
            return CollectResult("t@x.com", False, error_code="network", error_message="x")
        if account["email"] == "i@x.com":
            return CollectResult("i@x.com", False, error_code="imap_auth", error_message="x")
        return CollectResult("t@x.com", True, data={"plan_name": "Max"})

    summary = devin_runner.run_devin_collection(
        trigger="manual", store=store, collector=collector, use_file_lock=False, notify=False
    )
    assert calls == {"t@x.com": 2, "i@x.com": 1}
    assert (summary.success, summary.failed) == (1, 1)


def test_manual_run_does_not_notify_by_default_and_empty_scheduler_is_silent(store, monkeypatch):
    sent = []
    monkeypatch.setattr(devin_runner, "send_alert", lambda *a, **k: sent.append(a))
    devin_runner.run_devin_collection(trigger="scheduler", store=store, use_file_lock=False)  # 0 账号
    assert sent == []
    _add(store, "a@x.com")
    devin_runner.run_devin_collection(
        trigger="manual", store=store, use_file_lock=False,
        collector=_fake_collector({"a@x.com": CollectResult("a@x.com", True, data={"plan_name": "Max"})}),
    )
    assert sent == []


def test_build_alert_levels():
    s = devin_runner.RunSummary(run_id="r", trigger="scheduler", total=3, success=3)
    assert devin_runner.build_alert(s)[2] == "success"
    s = devin_runner.RunSummary(run_id="r", trigger="scheduler", total=3, failed=3,
                                failure_breakdown={"network": 3},
                                failures=[{"email": "a", "code": "network"}])
    title, content, level = devin_runner.build_alert(s)
    assert level == "error" and "失败" in title and "网络/代理超时：3 个" in content
    busy = devin_runner.RunSummary(trigger="scheduler", lock_busy=True)
    assert "未拿到全局锁" in devin_runner.build_alert(busy)[1]


def test_alert_card_renders_devin_fields():
    from cam.alerting import _build_feishu_card

    s = devin_runner.RunSummary(run_id="r1", trigger="scheduler", total=2, success=1, failed=1,
                                failure_breakdown={"code_timeout": 1},
                                failures=[{"email": "a@x.com", "code": "code_timeout"}])
    title, content, level = devin_runner.build_alert(s)
    card = _build_feishu_card(title, content, level=level)
    rendered = str(card)
    assert "平台" in rendered and "Devin" in rendered and "账号总数" in rendered and "a@x.com" in rendered
    assert card["header"]["template"] == "orange"


# ─── 导入 ─────────────────────────────────────────────────────────

def test_import_csv_aliases_and_warnings():
    csv_bytes = (
        "Email,imap_pwd,feishu_email,备注\n"
        "A@X.com ,pw1,Owner@x.com,hello\n"
        "bad-email,pw2,,\n"
        "c@x.com,,,\n"
    ).encode("utf-8-sig")
    rows, warnings = parse_accounts_file("a.csv", csv_bytes)
    assert [r.email for r in rows] == ["a@x.com"]
    assert rows[0].owner_email == "owner@x.com" and rows[0].note == "hello"
    assert len(warnings) == 2


def test_import_requires_columns():
    with pytest.raises(DevinImportError):
        parse_accounts_file("a.csv", b"email\nfoo@x.com\n")


def test_template_roundtrip_through_excel_parser():
    rows, warnings = parse_accounts_file("t.xlsx", build_template_xlsx())
    assert len(rows) == 1 and rows[0].imap_port == 993 and warnings == []


# ─── 调度 ─────────────────────────────────────────────────────────

def test_next_run_at_is_early_morning_beijing():
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 11, 23, 1, 0, tzinfo=tz).astimezone(timezone.utc)
    assert next_run_at(now, "02:30", tz).astimezone(tz) == datetime(2026, 11, 23, 2, 30, tzinfo=tz)
    late = datetime(2026, 11, 23, 3, 0, tzinfo=tz).astimezone(timezone.utc)
    assert next_run_at(late, "02:30", tz).astimezone(tz) == datetime(2026, 11, 24, 2, 30, tzinfo=tz)


def test_scheduler_tick_triggers_once(monkeypatch):
    fired = []
    monkeypatch.setattr(DevinScheduler, "_run_once", staticmethod(lambda: fired.append(1)))
    sched = DevinScheduler()
    sched._next_at = NOW
    assert sched.tick(NOW - timedelta(minutes=1)) is False
    assert sched.tick(NOW) is True
    sched._worker.join(2)
    assert fired == [1]
    assert sched.next_at > NOW  # 已顺延到下一天
    assert sched.tick(NOW) is False


# ─── HTTP 接口 ────────────────────────────────────────────────────

@pytest.fixture()
def client(store, monkeypatch):
    monkeypatch.setattr(devin_api, "get_default_devin_store", lambda: store)
    return TestClient(app)


def test_api_import_flow_and_dashboard(client, store):
    csv_bytes = b"email,imap_password,owner_email\nd1@x.com,pw,o@x.com\nd2@x.com,pw,\n"
    r = client.post("/api/devin/upload", files={"file": ("a.csv", csv_bytes, "text/csv")})
    assert r.status_code == 200 and r.json()["count"] == 2
    preview = r.json()["accounts"]

    saved = client.post("/api/devin/accounts/save", json={"accounts": preview})
    assert saved.json()["saved"] == 2
    again = client.post("/api/devin/accounts/save", json={"accounts": preview}).json()
    assert again["skipped"] == 2 and again["saved"] == 0
    assert client.post("/api/devin/accounts/save", json={"accounts": preview, "overwrite": True}).json()["saved"] == 2

    listing = client.get("/api/devin/accounts").json()
    assert listing["count"] == 2 and all("imap_password" not in a for a in listing["accounts"])

    store.add_snapshot(email="d1@x.com", ok=True, data={"plan_name": "Max", "weekly_used_pct": 85.0, "plan_price_usd": 200.0},
                       raw={"api": [], "text": ["hello"]})
    dash = client.get("/api/devin/dashboard").json()
    assert dash["kpis"]["total"] == 2 and dash["kpis"]["with_data"] == 1 and dash["kpis"]["high_usage"] == 1
    assert dash["kpis"]["plan_distribution"] == {"Max": 1}
    assert dash["schedule"]["daily_at"] == "02:30"
    assert "code_timeout" in dash["error_labels"]

    snap_id = next(r for r in dash["rows"] if r["email"] == "d1@x.com")["snapshot_id"]
    assert client.get(f"/api/devin/snapshots/{snap_id}").json()["raw"]["text"] == ["hello"]
    assert client.get("/api/devin/history/d1@x.com").json()["snapshots"][0]["plan_name"] == "Max"

    export = client.get("/api/devin/export.xlsx")
    assert export.status_code == 200 and export.content[:2] == b"PK"


def test_api_status_note_delete_and_errors(client, store):
    _add(store, "d1@x.com")
    assert client.post("/api/devin/accounts/d1@x.com/status", json={"status": "disabled"}).status_code == 200
    assert store.get_account("d1@x.com")["status"] == "disabled"
    assert client.post("/api/devin/accounts/d1@x.com/status", json={"status": "bad"}).status_code == 400
    assert client.post("/api/devin/accounts/d1@x.com/note", json={"note": "hi"}).status_code == 200
    assert store.get_account("d1@x.com")["note"] == "hi"
    assert client.delete("/api/devin/accounts/nope@x.com").status_code == 404
    assert client.delete("/api/devin/accounts/d1@x.com").status_code == 200
    assert client.post("/api/devin/collect", json={"emails": ["ghost@x.com"]}).status_code == 404
    assert client.post("/api/devin/collect-failed").status_code == 422
    bad = client.post("/api/devin/upload", files={"file": ("a.csv", b"foo,bar\n1,2\n", "text/csv")})
    assert bad.status_code == 400
    assert client.get("/api/devin/accounts/template.xlsx").status_code == 200


def test_api_collect_conflict_when_running(client, store, monkeypatch):
    _add(store, "d1@x.com")
    monkeypatch.setattr(devin_api, "is_running", lambda: True)
    assert client.post("/api/devin/collect", json={}).status_code == 409


def test_pages_served_and_platform_menu_present(client):
    devin = client.get("/devin")
    assert devin.status_code == 200 and "data-platform-nav" in devin.text and 'data-active="devin"' in devin.text
    index = client.get("/")
    assert 'data-platform-nav data-active="cursor"' in index.text
    js = client.get("/static/platforms.js").text
    assert "key: 'cursor'" in js and "key: 'devin'" in js


def test_email_client_default_behaviour_unchanged_signature():
    import inspect
    from cam.email_client import fetch_verification_code

    params = inspect.signature(fetch_verification_code).parameters
    assert params["sender_keywords"].default == ("cursor",)
    assert params["extra_search_queries"].default is None


# ─── 账期（入库）─────────────────────────────────────────────────

from cam.devin_collector import InvoiceResult  # noqa: E402
from cam.devin_invoices import build_invoice_xlsx, run_invoice_export  # noqa: E402
from cam.devin_parser import (  # noqa: E402
    cycle_start_from_end,
    extract_invoices_from_api,
    merge_invoices,
    parse_invoices_text,
)


@pytest.mark.parametrize(
    "end,period,expected",
    [
        ("2026-12-13", "month", "2026-11-13"),
        ("2026-03-31", "month", "2026-02-28"),   # 月末截断
        ("2027-01-15", "month", "2026-12-15"),   # 跨年
        ("2027-02-28", "year", "2026-02-28"),
        ("2024-02-29", "year", "2023-02-28"),
        ("bad", "month", ""),
    ],
)
def test_cycle_start_from_end(end, period, expected):
    assert cycle_start_from_end(end, period) == expected


def test_store_cycles_upsert_peak_and_new_cycle(store):
    _add(store, "a@x.com")
    base = {"plan_name": "Max", "plan_price_usd": 200.0, "billing_period": "month", "renews_at": "2026-12-13"}
    store.add_snapshot(email="a@x.com", ok=True, data={**base, "weekly_used_pct": 40.0, "on_demand_used_usd": 1.0})
    store.add_snapshot(email="a@x.com", ok=True, data={**base, "weekly_used_pct": 90.0, "on_demand_used_usd": 3.0})
    store.add_snapshot(email="a@x.com", ok=True, data={**base, "weekly_used_pct": 10.0, "on_demand_used_usd": 2.0})
    store.add_snapshot(email="a@x.com", ok=False, error_code="network")  # 失败不入账期
    cycles = store.list_cycles("a@x.com")
    assert len(cycles) == 1
    c = cycles[0]
    assert (c["cycle_start"], c["cycle_end"]) == ("2026-11-13", "2026-12-13")
    assert c["samples"] == 3
    assert c["weekly_used_pct"] == 10.0 and c["peak_weekly_used_pct"] == 90.0   # 最新值 + 峰值
    assert c["on_demand_used_usd"] == 2.0 and c["peak_on_demand_used_usd"] == 3.0

    # 续费后 renews_at 前移 → 新账期一行，旧账期保留
    store.add_snapshot(email="a@x.com", ok=True, data={**base, "renews_at": "2027-01-13", "weekly_used_pct": 5.0})
    ends = [c["cycle_end"] for c in store.list_cycles("a@x.com")]
    assert ends == ["2027-01-13", "2026-12-13"]

    # Free 套餐没有续费日，不产生账期
    _add(store, "free@x.com")
    store.add_snapshot(email="free@x.com", ok=True, data={"plan_name": "Free"})
    assert store.list_cycles("free@x.com") == []

    row = {r["email"]: r for r in store.dashboard_rows()}["a@x.com"]
    assert row["cycle_end"] == "2027-01-13" and row["cycle_start"] == "2026-12-13"

    store.delete_account("a@x.com", purge_snapshots=True)
    assert store.list_cycles("a@x.com") == []


# ─── 发票（解析 / 直接导出）──────────────────────────────────────

INVOICE_TEXT = """Invoices
Filter by date
Date
Status
Amount
Nov 13, 2026
Paid
$213.20
View invoice
Oct 13, 2026 Paid $200.00 View invoice
"""


def test_parse_invoices_text_matches_screenshot():
    rows = parse_invoices_text(INVOICE_TEXT)
    assert rows == [
        {"invoice_date": "2026-11-13", "status": "Paid", "amount_usd": 213.2},
        {"invoice_date": "2026-10-13", "status": "Paid", "amount_usd": 200.0},
    ]
    assert parse_invoices_text("No invoices yet") == []


def test_extract_and_merge_invoices_prefers_visible_amount():
    api = extract_invoices_from_api([{
        "url": "https://api.devin.ai/billing/invoices",
        "body": {"data": [{
            "id": "in_1", "number": "AB-001", "created": 1794528000,  # 2026-11-13 00:00 UTC
            "hosted_invoice_url": "https://invoice.stripe.com/i/x", "invoice_pdf": "https://pay.stripe.com/x/pdf",
            "amount_paid": 99999, "status": "paid",
        }]},
    }])
    assert api and api[0]["pdf_url"].endswith("/pdf")
    merged = merge_invoices(parse_invoices_text(INVOICE_TEXT), api, ["https://l/1", "https://l/2"])
    first = merged[0]
    assert first["amount_usd"] == 213.2                       # 金额以页面可见为准，不信 API 的 amount_paid
    assert first["number"] == "AB-001" and first["pdf_url"].endswith("/pdf")
    assert first["invoice_url"] == "https://invoice.stripe.com/i/x"
    assert merged[1]["invoice_url"] == "https://l/2"          # 无 API 匹配时回落到页面链接


def test_build_invoice_xlsx_sheets():
    from openpyxl import load_workbook

    data = build_invoice_xlsx(
        [{"email": "a@x.com", "invoice_date": "2026-11-13", "status": "Paid", "amount_usd": 213.2,
          "invoice_url": "https://i/1", "owner_email": "o@x.com"}],
        [{"email": "b@x.com", "label": "验证码邮件超时未收到", "message": "m"}],
    )
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["发票明细", "账号汇总", "读取失败"]
    assert wb["发票明细"]["D2"].value == 213.2
    assert wb["发票明细"]["F2"].hyperlink.target == "https://i/1"
    assert wb["账号汇总"]["C2"].value == 1


def test_run_invoice_export_writes_file_without_touching_db(store, tmp_path, monkeypatch):
    monkeypatch.setenv("DEVIN_EXPORT_DIR", str(tmp_path / "exp"))
    monkeypatch.setenv("DEVIN_CONCURRENCY", "2")
    _add(store, "a@x.com", owner="o@x.com")
    _add(store, "b@x.com")
    store.set_status("b@x.com", "disabled")
    _add(store, "c@x.com")

    def fetcher(account):
        if account["email"] == "c@x.com":
            return InvoiceResult(email="c@x.com", ok=False, error_code="code_timeout", error_message="no mail")
        return InvoiceResult(email=account["email"], ok=True, invoices=[
            {"invoice_date": "2026-11-13", "status": "Paid", "amount_usd": 213.2, "invoice_url": "https://i/1"},
        ])

    summary = run_invoice_export(store=store, fetcher=fetcher)
    assert (summary.total, summary.success, summary.failed) == (2, 1, 1)   # 停用账号不读
    assert summary.invoice_count == 1 and summary.amount_total_usd == 213.2
    assert summary.failures[0]["email"] == "c@x.com" and "验证码" in summary.failures[0]["label"]
    assert (tmp_path / "exp" / summary.file).is_file()
    assert store.list_snapshots("a@x.com") == []                            # 发票不入库


def test_run_invoice_export_refuses_when_collection_running(store):
    assert devin_runner._run_lock.acquire(blocking=False)
    try:
        summary = run_invoice_export(store=store, fetcher=lambda a: InvoiceResult(email=a["email"], ok=True))
        assert summary.lock_busy and not summary.file
    finally:
        devin_runner._run_lock.release()


def test_api_cycles_and_invoice_files(client, store, tmp_path, monkeypatch):
    monkeypatch.setenv("DEVIN_EXPORT_DIR", str(tmp_path / "exp"))
    _add(store, "a@x.com")
    end = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
    store.add_snapshot(email="a@x.com", ok=True, data={"plan_name": "Max", "renews_at": end, "weekly_used_pct": 30.0})
    r = client.get("/api/devin/cycles").json()
    assert r["count"] == 1 and r["cycles"][0]["state"] == "current" and r["cycles"][0]["days_left"] == 5
    assert client.get("/api/devin/dashboard").json()["kpis"]["renewing_soon"] == 1

    # 导出 xlsx 带“账期”工作表
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(client.get("/api/devin/export.xlsx").content))
    assert "账期" in wb.sheetnames

    # 发票：未知账号 404；文件下载只允许规范文件名
    assert client.post("/api/devin/invoices/export", json={"emails": ["nobody@x.com"]}).status_code == 404
    exp = tmp_path / "exp"
    exp.mkdir()
    (exp / "devin-invoices-20261123-010203.xlsx").write_bytes(b"x")
    (exp / "secret.xlsx").write_bytes(b"x")
    listed = client.get("/api/devin/invoices/progress").json()
    assert [f["name"] for f in listed["files"]] == ["devin-invoices-20261123-010203.xlsx"]
    assert client.get("/api/devin/invoices/files/devin-invoices-20261123-010203.xlsx").status_code == 200
    assert client.get("/api/devin/invoices/files/secret.xlsx").status_code == 404
    assert client.get("/api/devin/invoices/files/..%2Fsecret.xlsx").status_code == 404


# ─── 额度解析 / 数据完整性 ───────────────────────────────────────

REAL_USAGE_TEXT = """Usage & limits
Overview
Usage history
Are you an early-stage startup?
Devin for Startups gives eligible companies up to $65k in credits and grants.
Apply now
Current plan
Max
$200/month
Renews on December 13, 2026
Purchase on-demand usage
Configure auto-reload settings
Manage billing
Your included usage
Weekly quota
1% used
Resets in 6 days
On-demand usage
Your on-demand usage
$0.00
Remaining balance
$0.00
No on-demand balance remaining. Sessions will pause when your included quota is exceeded.
Message usage limit
Maximum on-demand usage that Devin can use per message you send
$
20
"""

REAL_PLANS_TEXT = """Plans
Current plan
Max
$200/month
Renews on December 13, 2026
Purchase on-demand usage
Individual plans
Free
Free
Pro
$20/month
Max
$200/month
Current plan
Teams
$80/month
"""


def test_parse_real_usage_page_all_fields():
    from cam.devin_parser import missing_fields

    data = parse_usage_text(REAL_USAGE_TEXT, now=NOW)
    assert data["plan_name"] == "Max" and data["plan_price_usd"] == 200.0
    assert data["renews_at"] == "2026-12-13"
    assert data["weekly_used_pct"] == 1.0 and data["weekly_reset_at"]
    assert data["on_demand_used_usd"] == 0.0 and data["on_demand_balance_usd"] == 0.0
    assert data["message_limit_usd"] == 20.0
    assert missing_fields(data) == []


def test_plans_page_does_not_leak_other_plan_prices_or_purchase_button_money():
    data = parse_usage_text(REAL_PLANS_TEXT, now=NOW)
    assert data["plan_price_usd"] == 200.0            # 不能取到 Pro 的 $20 / Teams 的 $80
    assert "on_demand_used_usd" not in data           # “Purchase on-demand usage”按钮后面的价格不是已用金额

    # 没有 Current plan 标签、只有套餐卡片时：只取与套餐名相邻的价格
    only_cards = "Free\nFree\nPro\n$20/month\nMax\n$200/month\nCurrent plan\n"
    assert parse_usage_text(only_cards, now=NOW).get("plan_price_usd") in (None, 200.0)


def test_missing_fields_by_devin_quota_model():
    from cam.devin_parser import missing_fields

    assert missing_fields({}) == ["套餐"]
    assert missing_fields({"plan_name": "Free"}) == []
    assert missing_fields({"plan_name": "Max"}) == ["账期", "周额度", "按需用量"]
    assert missing_fields({"plan_name": "Max", "renews_at": "2026-12-13", "weekly_used_pct": 0.0,
                           "on_demand_balance_usd": 0.0}) == []


def test_dashboard_marks_incomplete_rows(store):
    _add(store, "a@x.com")
    _add(store, "b@x.com")
    store.add_snapshot(email="a@x.com", ok=True, data={"plan_name": "Max", "plan_price_usd": 20.0})
    store.add_snapshot(email="b@x.com", ok=True, data={
        "plan_name": "Max", "renews_at": "2026-12-13", "weekly_used_pct": 1.0, "on_demand_used_usd": 0.0})
    rows = {r["email"]: r for r in store.dashboard_rows()}
    assert rows["a@x.com"]["missing"] == ["账期", "周额度", "按需用量"]
    assert rows["b@x.com"]["missing"] == []
