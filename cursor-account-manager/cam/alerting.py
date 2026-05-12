"""告警通知（MVP）。"""

from __future__ import annotations

import requests

from .config import SETTINGS
from .logger import get

log = get("alert")


def send_alert(title: str, content: str, *, level: str = "info") -> None:
    """发送告警；当前按接收人邮箱列表记录并预留外部网关。"""
    if not SETTINGS.alert_bot_enable:
        return

    recipients = [
        x.strip()
        for x in (SETTINGS.alert_to_emails or "").split(",")
        if x.strip()
    ]
    message = f"[{level.upper()}] {title}\n{content}"
    if not recipients:
        log.warning(f"告警已启用但未配置 ALERT_TO_EMAILS: {message}")
        return

    # 当前项目未内置 SMTP，实现上通过外部告警网关转发到邮箱；
    # 未配置网关时先记录接收人与消息，避免静默丢告警。
    log.info(f"告警目标邮箱: {','.join(recipients)}")

    webhook = ""  # 预留：后续若接入统一告警网关，可改为读取环境变量
    if not webhook:
        log.warning(f"未配置告警网关，已记录待发送告警: {message}")
        return

    payload = {
        "provider": SETTINGS.alert_bot_provider,
        "client_id": SETTINGS.alert_bot_client_id,
        "secret": SETTINGS.alert_bot_secret,
        "recipients": recipients,
        "title": title,
        "level": level,
        "text": content,
    }
    try:
        resp = requests.post(webhook, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
        if resp.status_code >= 400:
            log.warning(f"告警发送失败({resp.status_code}): {resp.text[:300]}")
    except Exception as e:
        log.warning(f"告警发送异常: {type(e).__name__}: {e}")

