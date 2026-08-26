"""告警通知。"""

from __future__ import annotations

from datetime import datetime
import json

import requests

from .config import SETTINGS
from .logger import get

log = get("alert")


def _card_template(level: str) -> str:
    level_name = (level or "info").lower()
    if level_name in {"error", "critical", "fatal"}:
        return "red"
    if level_name in {"on_demand", "attention"}:
        return "purple"
    if level_name in {"warning", "warn"}:
        return "orange"
    if level_name in {"success", "ok"}:
        return "green"
    return "blue"


def _level_label(level: str) -> str:
    level_name = (level or "info").lower()
    if level_name in {"success", "ok"}:
        return "成功"
    if level_name in {"error", "critical", "fatal"}:
        return "失败"
    if level_name in {"on_demand", "attention"}:
        return "需关注"
    if level_name in {"warning", "warn"}:
        return "预警"
    return "通知"


def _parse_kv_content(content: str) -> tuple[dict[str, str], list[str]]:
    fields: dict[str, str] = {}
    details: list[str] = []
    for line in (content or "").splitlines():
        text = line.strip()
        if not text:
            continue
        if "=" in text:
            key, value = text.split("=", 1)
            key = key.strip()
            if key:
                fields[key] = value.strip()
                continue
        details.append(text)
    return fields, details


def _field_label(key: str) -> str:
    labels = {
        "trigger_type": "触发方式",
        "run_id": "任务编号",
        "biz_date": "业务日期",
        "date": "触发日期",
        "status": "任务状态",
        "stage": "失败阶段",
        "account_success": "成功账号",
        "account_failed": "失败账号",
        "account_skipped": "跳过账号",
        "lock_busy": "锁忙碌",
        "circuit_blocked": "熔断拦截",
        "on_demand_open": "按需已开",
        "on_demand_historical": "曾有按需",
        "ods_rows": "ODS 行数",
        "reason": "失败原因",
        "error": "异常信息",
        "errors": "异常摘要",
    }
    return labels.get(key, key)


def _format_card_value(key: str, value: str) -> str:
    if key == "trigger_type":
        return {
            "scheduler": "调度执行",
            "manual": "手动执行",
            "retry": "失败重试",
            "replay": "自定义补偿",
            "daily": "每日同步",
            "usage_periodic": "用量日常采集",
        }.get(value, value)
    if key == "status":
        return {
            "success": "成功",
            "partial_failed": "部分失败",
            "failed": "失败",
            "running": "执行中",
        }.get(value, value)
    return value


def _field_item(key: str, value: str, *, is_short: bool = True) -> dict:
    return {
        "is_short": is_short,
        "text": {
            "tag": "lark_md",
            "content": f"**{_field_label(key)}：** {_format_card_value(key, value) or '-'}",
        },
    }


def _build_feishu_card(title: str, content: str, *, level: str) -> dict:
    level_text = _level_label(level)
    sent_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fields, details = _parse_kv_content(content)
    basic_keys = ("trigger_type", "biz_date", "date", "run_id", "status", "stage")
    metric_keys = (
        "account_success",
        "account_failed",
        "account_skipped",
        "circuit_blocked",
        "lock_busy",
        "on_demand_open",
        "on_demand_historical",
        "ods_rows",
    )
    detail_keys = ("reason", "error", "errors")

    basic_fields = [_field_item(k, fields[k]) for k in basic_keys if fields.get(k)]
    metric_fields = [_field_item(k, fields[k]) for k in metric_keys if fields.get(k)]
    detail_lines = details + [
        f"**{_field_label(k)}：** {_format_card_value(k, fields[k])}"
        for k in detail_keys
        if fields.get(k)
    ]

    elements: list[dict] = [
        {
            "tag": "div",
            "fields": [
                {
                    "is_short": True,
                    "text": {"tag": "lark_md", "content": f"**同步结果：** {level_text}"},
                },
                {
                    "is_short": True,
                    "text": {"tag": "lark_md", "content": f"**发送时间：** {sent_at}"},
                },
            ],
        },
    ]
    if basic_fields:
        elements.extend([{"tag": "hr"}, {"tag": "div", "fields": basic_fields}])
    if metric_fields:
        elements.extend(
            [
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content": "**执行结果**"}},
                {"tag": "div", "fields": metric_fields},
            ]
        )
    if detail_lines:
        elements.extend(
            [
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(detail_lines)[:12000]}},
            ]
        )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": _card_template(level),
            "title": {
                "tag": "plain_text",
                "content": title,
            },
        },
        "elements": elements,
    }


def send_alert(title: str, content: str, *, level: str = "info") -> None:
    """发送告警到飞书用户邮箱对应账号。"""
    if not SETTINGS.alert_bot_enable:
        log.info(f"告警未启用，跳过发送: title={title}")
        return

    recipients = [
        x.strip()
        for x in (SETTINGS.alert_to_emails or "").split(",")
        if x.strip()
    ]
    message = f"[{level.upper()}] {title}\n{content}"
    card = _build_feishu_card(title, content, level=level)
    if not recipients:
        log.warning(f"告警已启用但未配置 ALERT_TO_EMAILS: {message}")
        return

    if SETTINGS.alert_bot_provider.lower() != "feishu":
        log.warning(f"不支持的告警 provider={SETTINGS.alert_bot_provider}: {message}")
        return
    if not SETTINGS.alert_bot_client_id or not SETTINGS.alert_bot_secret:
        log.warning(f"飞书告警缺少 app_id/app_secret，无法发送: {message}")
        return

    try:
        token_resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={
                "app_id": SETTINGS.alert_bot_client_id,
                "app_secret": SETTINGS.alert_bot_secret,
            },
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()
        if token_data.get("code") not in (0, None):
            log.warning(f"飞书 token 获取失败: {token_data}")
            return
        token = token_data.get("tenant_access_token")
        if not token:
            log.warning(f"飞书 token 响应缺少 tenant_access_token: {token_data}")
            return

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        sent = 0
        for email in recipients:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=email",
                json={
                    "receive_id": email,
                    "msg_type": "interactive",
                    "content": json.dumps(card, ensure_ascii=False),
                },
                headers=headers,
                timeout=10,
            )
            if resp.status_code >= 400:
                log.warning(f"飞书告警发送失败 email={email} http={resp.status_code}: {resp.text[:300]}")
                continue
            data = resp.json()
            if data.get("code") != 0:
                log.warning(f"飞书告警发送失败 email={email}: {data}")
                continue
            sent += 1
        if sent:
            log.info(f"飞书告警发送成功 sent={sent} recipients={','.join(recipients)} title={title}")
        else:
            log.warning(f"飞书告警未成功发送给任何接收人: {message}")
    except Exception as e:
        log.warning(f"告警发送异常: {type(e).__name__}: {e}")

