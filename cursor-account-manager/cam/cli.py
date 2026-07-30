"""CLI 入口：python -m cam <cmd>"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import click

from . import exporter, fetcher
from .account_store import filter_accounts, load_accounts
from .config import SETTINGS
from .logger import get, setup
from .models import Account, AccountSnapshot, TokenAcquisitionError
from .bi_sync import retry_failed_accounts, run_daily_sync
from .scheduler import run_scheduler_loop, run_scheduler_once_for_yesterday
from .token_manager import get_default_manager
from .token_store import get_default_store
from .usage_snapshot_refresh import (
    get_usage_runtime,
    run_usage_periodic,
    run_usage_pre_reset_due,
)

log = get("cli")


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="DEBUG 日志")
def cli(verbose: bool) -> None:
    """Cursor Account Manager — 批量管理 Cursor 账号。"""
    setup("DEBUG" if verbose else "INFO")


def _pick_accounts(all_flag: bool, email: tuple[str, ...]) -> list[Account]:
    accounts = load_accounts()
    if all_flag:
        return accounts
    if email:
        filtered = filter_accounts(accounts, email)
        if not filtered:
            raise click.UsageError(f"CSV 里找不到指定的 email: {list(email)}")
        return filtered
    raise click.UsageError("必须指定 --all 或 --email")


def _json_default(value: object) -> object:
    """将服务层的时间和枚举结果转换为可审计 JSON。"""
    if isinstance(value, datetime):
        return value.isoformat()
    enum_value = getattr(value, "value", None)
    if enum_value is not None:
        return enum_value
    raise TypeError(f"无法转换为 JSON: {type(value).__name__}")


def _echo_json(result: object) -> None:
    """统一输出中文 JSON，保留服务层返回的全部字段。"""
    payload = asdict(result) if hasattr(result, "__dataclass_fields__") else result
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


def _parse_cycle_start(value: str) -> datetime:
    """解析 repair 命令需要的 ISO8601 UTC 账期开始时间。"""
    try:
        cycle_start = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise click.BadParameter("必须是 ISO8601 时间") from exc
    if cycle_start.tzinfo is None or cycle_start.utcoffset().total_seconds() != 0:
        raise click.BadParameter("必须是带 UTC 时区的 ISO8601 时间")
    return cycle_start


# ═══ usage（用量快照运维）══════════════════════════════════════════

@cli.command("usage-snapshot")
@click.option("--all", "all_flag", is_flag=True, help="采集所有可监控账号")
@click.option("--email", "email", multiple=True, help="仅采集指定邮箱（可重复）")
@click.option(
    "--type", "snapshot_type", type=click.Choice(["periodic"]),
    required=True, help="快照类型（当前仅支持 periodic）",
)
def cmd_usage_snapshot(
    all_flag: bool, email: tuple[str, ...], snapshot_type: str
) -> None:
    """执行 periodic 用量快照采集。"""
    if all_flag and email:
        raise click.UsageError("--all 与 --email 不能同时使用")
    if not all_flag and not email:
        raise click.UsageError("必须指定 --all 或至少一个 --email")
    # 类型由 Click 限定为 periodic，CLI 只负责将账号范围委派给服务层。
    _ = snapshot_type
    _echo_json(run_usage_periodic(emails=None if all_flag else email))


@cli.command("usage-pre-reset-due")
@click.option("--dry-run", is_flag=True, help="只预览即将采集的账号，不写入数据")
def cmd_usage_pre_reset_due(dry_run: bool) -> None:
    """采集进入 pre-reset 窗口的账号，或预览待处理账号。"""
    _echo_json(run_usage_pre_reset_due(dry_run=dry_run))


@cli.command("usage-finalize")
@click.option("--email", required=True, help="需要修复结算的账号邮箱")
@click.option("--cycle-start", required=True, help="账期开始 ISO8601 UTC 时间")
@click.option("--actor", required=True, help="执行修复的操作者")
@click.option("--reason", required=True, help="修复原因")
def cmd_usage_finalize(
    email: str, cycle_start: str, actor: str, reason: str
) -> None:
    """显式修复指定账号账期的最终结算状态。"""
    result = get_usage_runtime().store.repair_finalize_cycle(
        email,
        _parse_cycle_start(cycle_start),
        actor=actor,
        reason=reason,
    )
    _echo_json(result)


def _print_summary(title: str, ok: list[str], fail: list[tuple[str, str]]) -> None:
    click.echo()
    click.echo("═" * 60)
    click.echo(f"{title}   成功 {len(ok)} / 失败 {len(fail)}")
    click.echo("═" * 60)
    if ok:
        click.echo("✓ 成功:")
        for e in ok:
            click.echo(f"    {e}")
    if fail:
        click.echo("✗ 失败:")
        for e, err in fail:
            click.echo(f"    {e}: {err[:120]}")


# ═══ login ════════════════════════════════════════════════════════

@cli.command("login")
@click.option("--all", "all_flag", is_flag=True, help="对 CSV 中所有账号执行浏览器登录")
@click.option("--email", "email", multiple=True, help="只登录指定邮箱（可重复）")
@click.option("--force", is_flag=True, help="忽略现有 token，强制浏览器重登")
@click.option("--fresh", is_flag=True, help="重置账号状态 + 清掉浏览器 profile，像全新账号一样登录")
def cmd_login(all_flag: bool, email: tuple[str, ...], force: bool, fresh: bool) -> None:
    """浏览器登录（串行），写入/更新 tokens.db。"""
    accounts = _pick_accounts(all_flag, email)
    mgr = get_default_manager()
    store = get_default_store()

    if fresh:
        from . import browser_login as _bl
        for acc in accounts:
            store.reset(acc.email)
            _bl._clear_user_data(acc.email)
            click.echo(f"[fresh] 已重置 {acc.email} 状态 + 浏览器 profile")

    ok, fail = [], []
    for acc in accounts:
        try:
            if force or fresh:
                mgr.force_relogin(acc)
            else:
                mgr.get_valid_token(acc)
            ok.append(acc.email)
        except TokenAcquisitionError as e:
            fail.append((acc.email, str(e)))
        except Exception as e:
            fail.append((acc.email, f"{type(e).__name__}: {e}"))
    _print_summary("login", ok, fail)


# ═══ fetch ════════════════════════════════════════════════════════

WHAT_ALIAS = {
    "usage": "usage",
    "plan": "plan",
    "limit": "usage_limit",
    "events": "usage_events",
    "stripe": "stripe",
    "all": "__all__",
}


@cli.command("fetch")
@click.option("--all", "all_flag", is_flag=True, help="拉 CSV 中所有账号")
@click.option("--email", "email", multiple=True)
@click.option(
    "--what", default="all",
    help="逗号分隔: usage,plan,limit,events,stripe,all",
)
@click.option("--concurrency", type=int, default=None)
@click.option(
    "--out-dir", type=click.Path(path_type=Path), default=None,
    help="JSON 输出目录（默认 data/exports/raw/）",
)
def cmd_fetch(
    all_flag: bool, email: tuple[str, ...],
    what: str, concurrency: int | None, out_dir: Path | None,
) -> None:
    """拉取账号数据，自动管理 token，JSON 写盘。"""
    accounts = _pick_accounts(all_flag, email)

    wanted: list[str] = []
    for x in (p.strip() for p in what.split(",") if p.strip()):
        real = WHAT_ALIAS.get(x.lower())
        if real is None:
            raise click.UsageError(f"未知 --what: {x}（可选 {list(WHAT_ALIAS.keys())}）")
        if real == "__all__":
            wanted = list(fetcher.DEFAULT_WHAT)
            break
        wanted.append(real)

    log.info(f"拉取 {len(accounts)} 个账号，类型={wanted}，并发={concurrency or SETTINGS.api_concurrency}")
    snaps = fetcher.fetch_many(accounts, what=wanted, concurrency=concurrency)

    target = out_dir or (SETTINGS.exports_dir / "raw")
    exporter.export_json(snaps, target)

    ok = [s.email for s in snaps if not s.errors]
    fail = [(s.email, ",".join(s.errors.keys())) for s in snaps if s.errors]
    _print_summary("fetch", ok, fail)


# ═══ export ═══════════════════════════════════════════════════════

@cli.command("export")
@click.option(
    "--format", "fmt",
    type=click.Choice(["json", "csv", "xlsx"]), default="xlsx",
    help="导出格式（默认 xlsx，多 sheet：账号概览/使用明细/发票）",
)
@click.option(
    "--from-dir", type=click.Path(path_type=Path), default=None,
    help="读取已有 JSON 目录；不指定则现拉取",
)
@click.option("--out", "out", type=click.Path(path_type=Path), required=True)
@click.option("--all", "all_flag", is_flag=True)
@click.option("--email", "email", multiple=True)
def cmd_export(fmt: str, from_dir: Path | None, out: Path, all_flag: bool, email: tuple[str, ...]) -> None:
    """从已有 JSON 或现拉取，生成 JSON/CSV/XLSX。"""
    if from_dir:
        snaps = _load_snapshots_from_dir(from_dir)
        log.info(f"从 {from_dir} 加载 {len(snaps)} 个快照")
    else:
        accounts = _pick_accounts(all_flag, email)
        snaps = fetcher.fetch_many(accounts)

    if fmt == "json":
        exporter.export_json(snaps, out)
    elif fmt == "csv":
        exporter.export_csv(snaps, out)
    else:
        exporter.export_xlsx(snaps, out)


def _load_snapshots_from_dir(d: Path) -> list[AccountSnapshot]:
    snaps: list[AccountSnapshot] = []
    for p in sorted(Path(d).glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            snaps.append(AccountSnapshot(
                email=data.get("email", p.stem),
                fetched_at=int(data.get("fetched_at", 0)),
                usage=data.get("usage") or {},
                plan=data.get("plan") or {},
                usage_limit=data.get("usage_limit") or {},
                usage_events=data.get("usage_events") or [],
                stripe=data.get("stripe") or {},
                invoices=data.get("invoices") or [],
                errors=data.get("errors") or {},
            ))
        except Exception as e:
            log.warning(f"跳过 {p}: {e}")
    return snaps


# ═══ dump（一键全量导出）═════════════════════════════════════════

@cli.command("dump")
@click.option("--all", "all_flag", is_flag=True, help="导出 CSV 中所有账号")
@click.option("--email", "email", multiple=True, help="指定账号（可重复）")
@click.option(
    "--out-dir", type=click.Path(path_type=Path), default=None,
    help="输出根目录（默认 data/exports/accounts/）",
)
@click.option("--concurrency", type=int, default=None, help="API 并发度")
@click.option("--no-invoices", is_flag=True, help="跳过发票 PDF 下载")
@click.option("--no-raw", is_flag=True, help="不写 raw.json（原始 JSON）")
@click.option("--no-summary", is_flag=True, help="不生成 _summary.xlsx 全账号汇总")
def cmd_dump(
    all_flag: bool, email: tuple[str, ...],
    out_dir: Path | None, concurrency: int | None,
    no_invoices: bool, no_raw: bool, no_summary: bool,
) -> None:
    """一键导出：每账号一个子目录（Excel + 发票 PDF + raw JSON）+ 全账号汇总。

    目录结构：
        data/exports/accounts/
          {email}/{email}.xlsx         ← 账号概览 / 使用明细 / 发票 清单
          {email}/invoices/*.pdf       ← 所有发票 PDF
          {email}/raw.json             ← 原始 JSON
          _summary.xlsx                ← 全账号汇总（横向比较）
    """
    accounts = _pick_accounts(all_flag, email)
    log.info(f"一键导出 {len(accounts)} 个账号，并发={concurrency or SETTINGS.api_concurrency}")

    snaps = fetcher.fetch_many(accounts, concurrency=concurrency)

    out_root = out_dir or (SETTINGS.exports_dir / "accounts")
    result = exporter.export_per_account(
        accounts, snaps, out_root,
        with_raw=not no_raw,
        with_invoices=not no_invoices,
        with_summary=not no_summary,
    )

    ok: list[str] = []
    fail: list[tuple[str, str]] = []
    click.echo()
    click.echo("═" * 70)
    click.echo(f"{'账号':38s}  {'使用事件':>7s}  {'发票':>5s}  输出目录")
    click.echo("─" * 70)
    for snap in snaps:
        info = result.get(snap.email, {})
        events_n = len(snap.usage_events or [])
        inv_n = info.get("invoices", 0) if info else 0
        rel_dir = Path(info.get("dir", "")).relative_to(out_root.parent) if info else ""
        line = (
            f"{snap.email:38s}  {events_n:>7d}  {inv_n:>5d}  {rel_dir}"
        )
        click.echo(line)
        if snap.errors:
            fail.append((snap.email, ",".join(snap.errors.keys())))
        else:
            ok.append(snap.email)

    click.echo("═" * 70)
    click.echo(f"输出根目录: {out_root}")
    if not no_summary and snaps:
        click.echo(f"全账号汇总: {out_root / '_summary.xlsx'}")

    _print_summary("dump", ok, fail)

# ═══ status ═══════════════════════════════════════════════════════

@cli.command("status")
def cmd_status() -> None:
    """列出所有 token 记录的状态。"""
    store = get_default_store()
    records = store.list_all()
    if not records:
        click.echo("tokens.db 为空")
        return

    now = int(time.time())
    click.echo(f"{'email':40s}  {'status':10s}  {'exp':20s}  {'fails':>5s}")
    click.echo("─" * 85)
    for r in records:
        if r.expires_at > 0:
            dt = datetime.fromtimestamp(r.expires_at).strftime("%Y-%m-%d %H:%M")
            if r.expires_at <= now:
                dt += " ⚠过期"
        else:
            dt = "-"
        click.echo(f"{r.email:40s}  {r.status:10s}  {dt:20s}  {r.consecutive_failures:>5d}")


# ═══ reset ════════════════════════════════════════════════════════

@cli.command("reset")
@click.option("--email", "email", multiple=True, help="要重置的邮箱（可多个）")
@click.option("--all", "all_flag", is_flag=True, help="重置 CSV 中所有账号")
@click.option("--profile", is_flag=True, help="同时删除浏览器 profile 目录")
@click.confirmation_option(prompt="确认清空这些账号的 token？")
def cmd_reset(email: tuple[str, ...], all_flag: bool, profile: bool) -> None:
    """清除账号的 token 记录与 disabled 状态，下次使用时将重新浏览器登录。"""
    if not email and not all_flag:
        raise click.UsageError("必须指定 --email 或 --all")
    if all_flag:
        email = tuple(a.email for a in load_accounts())

    store = get_default_store()
    if profile:
        from . import browser_login as _bl
    for e in email:
        store.reset(e)
        msg = f"已清除 {e}"
        if profile:
            _bl._clear_user_data(e)
            msg += "（含 profile）"
        click.echo(msg)


@cli.command("test-imap")
@click.option("--email", required=True, help="邮箱地址")
@click.option("--timeout", default=60, help="等待验证码最多多少秒（默认 60）")
def cmd_test_imap(email: str, timeout: int) -> None:
    """仅测试 IMAP 连接 + 轮询验证码（不启动浏览器），用于排查邮箱问题。"""
    from . import email_client
    accounts = load_accounts()
    acc = next((a for a in accounts if a.email.lower() == email.lower()), None)
    if not acc:
        raise click.UsageError(f"CSV 里找不到 {email}")
    click.echo(f"测试 {email} → IMAP {acc.imap_host}:{acc.imap_port}")
    try:
        code = email_client.fetch_verification_code(
            acc.email, acc.imap_password,
            host=acc.imap_host, port=acc.imap_port,
            timeout_sec=timeout,
        )
        click.echo(f"✓ 成功拿到验证码: {code}")
    except Exception as e:
        click.echo(f"✗ 失败: {type(e).__name__}: {e}")


# ═══ web（二期 Web UI）═══════════════════════════════════════════

@cli.command("web")
@click.option("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
@click.option("--port", default=8765, type=int, help="监听端口（默认 8765）")
@click.option("--reload", is_flag=True, help="开发模式：代码变更自动重载")
def cmd_web(host: str, port: int, reload: bool) -> None:
    """启动 Web 服务（二期 UI）。

    启动后用浏览器访问 http://localhost:<port>/
    """
    click.echo(f"启动 Web 服务: http://{host if host != '0.0.0.0' else 'localhost'}:{port}/")
    from .web_server import serve
    serve(host=host, port=port, reload=reload)


def main() -> None:
    cli()


# ═══ sync-daily / sync-retry（BI 日同步）════════════════════════════

@cli.command("sync-daily")
@click.option("--biz-date", default="", help="业务日期 YYYY-MM-DD；默认昨天（北京时间）")
@click.option("--trigger", default="manual", help="触发类型（manual/scheduler/retry）")
@click.option("--email", "email", multiple=True, help="仅同步指定账号（可重复）")
def cmd_sync_daily(biz_date: str, trigger: str, email: tuple[str, ...]) -> None:
    """执行每日明细同步（CSV/API 拉取 -> StarRocks ODS）。"""
    result = run_daily_sync(
        biz_date=biz_date or None,
        trigger_type=trigger or "manual",
        emails=email or None,
    )
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@cli.command("sync-retry")
@click.option("--run-id", required=True, help="失败重跑的来源 run_id")
@click.option("--biz-date", default="", help="可选：覆盖重跑日期 YYYY-MM-DD")
def cmd_sync_retry(run_id: str, biz_date: str) -> None:
    """按 run_id 重跑失败账号。"""
    result = retry_failed_accounts(run_id=run_id, biz_date=biz_date or None)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@cli.command("sync-scheduler-once")
def cmd_sync_scheduler_once() -> None:
    """按调度触发方式执行一次（默认同步昨天）。"""
    result = run_scheduler_once_for_yesterday()
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@cli.command("sync-scheduler-loop")
@click.option("--poll-interval", default=30, type=int, help="轮询间隔秒数（默认30）")
def cmd_sync_scheduler_loop(poll_interval: int) -> None:
    """启动本地常驻调度循环（MVP）。"""
    click.echo("启动同步调度循环（Ctrl+C 停止）")
    run_scheduler_loop(poll_interval_sec=poll_interval)


if __name__ == "__main__":
    main()
