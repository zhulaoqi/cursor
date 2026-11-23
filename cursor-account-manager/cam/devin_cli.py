"""Devin 运维命令：python -m cam devin-*（导入 / 采集 / 状态 / 探针 / 通知自检）。"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from .devin_config import devin_settings
from .devin_errors import error_label
from .devin_import import normalize_email, parse_accounts_file
from .devin_runner import RunSummary, build_alert, run_devin_collection
from .devin_scheduler import next_run_at
from .devin_store import get_default_devin_store


def _echo_json(data) -> None:
    click.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _fmt_ts(ts) -> str:
    return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M") if ts else "-"


def register(cli: click.Group) -> None:
    @cli.command("devin-import")
    @click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
    @click.option("--overwrite", is_flag=True, help="覆盖已存在账号（更新授权码/负责人/备注）")
    def cmd_devin_import(file: Path, overwrite: bool) -> None:
        """从 CSV/Excel 批量导入 Devin 账号（与 Cursor 账号库独立）。"""
        from .config import SETTINGS

        rows, warnings = parse_accounts_file(file.name, file.read_bytes())
        store = get_default_devin_store()
        existing = {a["email"] for a in store.list_accounts()}
        saved = skipped = 0
        for r in rows:
            if r.email in existing and not overwrite:
                skipped += 1
                continue
            store.upsert_account(
                r.email, r.imap_password, r.imap_host or SETTINGS.default_imap_host,
                r.imap_port or SETTINGS.default_imap_port, r.owner_email, r.note, "cli",
            )
            saved += 1
        for w in warnings:
            click.echo(f"! {w}")
        click.echo(f"导入完成：解析 {len(rows)}，保存 {saved}，跳过 {skipped}")

    @cli.command("devin-status")
    @click.option("--failed", "only_failed", is_flag=True, help="只看最近一次失败的账号")
    def cmd_devin_status(only_failed: bool) -> None:
        """查看 Devin 账号库与最近一次采集结果。"""
        rows = get_default_devin_store().dashboard_rows()
        click.echo(f"{'账号':<36}{'套餐':<8}{'周已用%':>8}  {'按需$':>7}  {'数据时间':<12}状态")
        shown = 0
        for r in rows:
            failed = r["last_attempt_ok"] is False
            if only_failed and not failed:
                continue
            shown += 1
            state = "停用" if r["status"] != "active" else ("未采集" if r["last_attempt_at"] is None else ("失败: " + error_label(r["last_error_code"]) if failed else "成功"))
            pct = "-" if r["weekly_used_pct"] is None else f"{r['weekly_used_pct']:g}"
            od = "-" if r["on_demand_used_usd"] is None else f"{r['on_demand_used_usd']:.2f}"
            click.echo(f"{r['email']:<36}{(r['plan_name'] or '-'):<8}{pct:>8}  {od:>7}  {_fmt_ts(r['data_collected_at']):<12}{state}")
        click.echo(f"—— 共 {shown}/{len(rows)} 个账号")

    @cli.command("devin-collect")
    @click.option("--email", "emails", multiple=True, help="只采集指定账号（可重复）；缺省=全部启用账号")
    @click.option("--trigger", type=click.Choice(["manual", "scheduler"]), default="manual",
                  help="scheduler=按定时任务口径（默认发飞书通知）；manual=手动（默认不通知）")
    @click.option("--notify/--no-notify", default=None, help="强制开/关飞书通知")
    def cmd_devin_collect(emails: tuple[str, ...], trigger: str, notify: bool | None) -> None:
        """立即采集 Devin 套餐与用量（可交给 cron / 计划任务调用）。"""
        summary = run_devin_collection(trigger=trigger, emails=list(emails) or None, notify=notify)
        _echo_json({
            "run_id": summary.run_id, "status": summary.status, "total": summary.total,
            "success": summary.success, "failed": summary.failed, "skipped": summary.skipped,
            "lock_busy": summary.lock_busy, "duration_sec": summary.duration_sec,
            "failure_breakdown": {error_label(k): v for k, v in summary.failure_breakdown.items()},
            "failures": summary.failures,
        })
        sys.exit(0 if summary.status == "success" else 1)

    @cli.command("devin-probe")
    @click.argument("email")
    @click.option("--headed", is_flag=True, help="有头模式，肉眼观察登录过程（需桌面环境）")
    @click.option("--save/--no-save", default=False, help="把结果写入库（默认只打印，不落库）")
    def cmd_devin_probe(email: str, headed: bool, save: bool) -> None:
        """单账号探针：走完整登录+采集，打印解析结果和原始数据；失败会落截图/页面文本。"""
        from .devin_collector import collect_account
        from .devin_runner import collect_one

        norm = normalize_email(email)
        store = get_default_devin_store()
        account = store.get_account(norm)
        if not account:
            raise click.UsageError(f"{norm} 不在 Devin 账号库，请先 devin-import")
        if headed:
            os.environ["DEVIN_HEADLESS"] = "false"
        click.echo(f"探针 {norm}  headless={devin_settings().headless}  artifacts={devin_settings().artifacts_dir}")
        if save:
            res = collect_one(account, store, run_id="probe", max_attempts=1)
        else:
            res = collect_account(account)
        click.echo(f"结果: {'成功' if res.ok else '失败'}  耗时 {res.duration_ms / 1000:.1f}s  走登录={res.login_used}  来源={res.source or '-'}")
        if res.ok:
            _echo_json(res.data)
            click.echo("—— 原始数据（截断）——")
            click.echo(json.dumps(res.raw, ensure_ascii=False, indent=2, default=str)[:4000])
        else:
            click.echo(f"错误码: {res.error_code}（{error_label(res.error_code)}）\n详情: {res.error_message}")
            sys.exit(1)

    @cli.command("devin-notify-test")
    @click.option("--send/--dry-run", default=False, help="--send 真正发飞书；默认只打印卡片内容")
    def cmd_devin_notify_test(send: bool) -> None:
        """用模拟数据生成一张 Devin 播报卡片，自检飞书通道与排版。"""
        from .alerting import send_alert

        summary = RunSummary(
            run_id="selftest", trigger="scheduler", total=10, success=7, failed=3, skipped=1, duration_sec=734,
            failure_breakdown={"code_timeout": 2, "imap_auth": 1},
            failures=[
                {"email": "demo1@example.com", "code": "code_timeout"},
                {"email": "demo2@example.com", "code": "code_timeout"},
                {"email": "demo3@example.com", "code": "imap_auth"},
            ],
            plan_distribution={"Max": 5, "Pro": 2},
            high_usage_accounts=[{"email": "demo9@example.com", "weekly_used_pct": 92.5}],
        )
        title, content, level = build_alert(summary)
        click.echo(f"[{level}] {title}\n{content}")
        if send:
            send_alert(title, content, level=level)
            click.echo("已调用 send_alert（是否真的送达取决于 ALERT_BOT_ENABLE / ALERT_TO_EMAILS）")

    @cli.command("devin-cycles")
    @click.option("--email", default="", help="只看指定账号")
    @click.option("--limit", default=50, type=int, show_default=True)
    def cmd_devin_cycles(email: str, limit: int) -> None:
        """查看已入库的账期（每账号每个计费周期一行）。"""
        rows = get_default_devin_store().list_cycles(normalize_email(email) if email else "", limit)
        click.echo(f"{'账号':<34}{'账期起':<12}{'账期止':<12}{'套餐':<7}{'周峰值%':>8}{'按需峰值$':>10}  最近采集")
        for r in rows:
            peak_w = "-" if r["peak_weekly_used_pct"] is None else f"{r['peak_weekly_used_pct']:g}"
            peak_o = "-" if r["peak_on_demand_used_usd"] is None else f"{r['peak_on_demand_used_usd']:.2f}"
            click.echo(
                f"{r['email']:<34}{r['cycle_start']:<12}{r['cycle_end']:<12}{(r['plan_name'] or '-'):<7}"
                f"{peak_w:>8}{peak_o:>10}  {_fmt_ts(r['last_seen_at'])}"
            )
        click.echo(f"—— 共 {len(rows)} 条")

    @cli.command("devin-invoices")
    @click.option("--email", "emails", multiple=True, help="只导出指定账号（可重复）；缺省=全部启用账号")
    def cmd_devin_invoices(emails: tuple[str, ...]) -> None:
        """实时读取发票并直接导出 Excel（不入库）。文件在 DEVIN_EXPORT_DIR。"""
        from .devin_invoices import run_invoice_export

        summary = run_invoice_export(emails=[normalize_email(e) for e in emails] or None)
        if summary.lock_busy:
            click.echo("已有采集/导出任务在执行（未拿到锁），请稍后重试")
            sys.exit(2)
        out = devin_settings().export_dir / summary.file if summary.file else None
        _echo_json({
            "file": str(out) if out else "", "accounts": summary.total, "success": summary.success,
            "failed": summary.failed, "invoices": summary.invoice_count,
            "amount_total_usd": summary.amount_total_usd,
            "failures": [{"email": f["email"], "reason": f["label"]} for f in summary.failures],
        })
        sys.exit(0 if summary.failed == 0 else 1)

    @cli.command("devin-next-run")
    def cmd_devin_next_run() -> None:
        """打印下一次定时采集时间。"""
        cfg = devin_settings()
        nxt = next_run_at(datetime.now(timezone.utc), cfg.daily_at)
        click.echo(f"DEVIN_SCHEDULE_ENABLE={cfg.schedule_enable}  每日 {cfg.daily_at}  下次: {nxt.astimezone().isoformat()}")
