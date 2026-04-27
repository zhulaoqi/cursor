# email_client.py - IMAP 客户端

import imaplib
import email
import re
import time
from email.header import decode_header


class IMAPAuthError(Exception):
    pass


class VerificationCodeTimeoutError(Exception):
    pass


def connect_imap(host: str, port: int, user: str, password: str) -> imaplib.IMAP4_SSL:
    """建立 SSL IMAP 连接并完成认证，失败时抛出 IMAPAuthError。"""
    try:
        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(user, password)
        return conn
    except imaplib.IMAP4.error as e:
        raise IMAPAuthError(f"IMAP 认证失败: {e}") from e


def extract_code_from_email(raw_email: bytes) -> str:
    """用正则 r'\\b(\\d{6})\\b' 从邮件正文中提取验证码。"""
    msg = email.message_from_bytes(raw_email)

    body_parts = []
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body_parts.append(payload.decode(charset, errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body_parts.append(payload.decode(charset, errors="replace"))

    body = "\n".join(body_parts)
    match = re.search(r'\b(\d{6})\b', body)
    if match:
        return match.group(1)
    return ""


def poll_for_verification_code(
    conn: imaplib.IMAP4_SSL,
    sender_filter: str = "no-reply@cursor.com",
    timeout_sec: int = 120,
    poll_interval_sec: int = 5,
) -> str:
    """
    每 poll_interval_sec 秒轮询一次 INBOX，
    在 timeout_sec 内找到来自 sender_filter 的最新邮件，
    提取并返回 6 位数字验证码。
    超时抛出 VerificationCodeTimeoutError。
    """
    deadline = time.time() + timeout_sec

    while time.time() < deadline:
        conn.select("INBOX")
        status, data = conn.search(None, f'(FROM "{sender_filter}")')
        if status == "OK" and data and data[0]:
            msg_ids = data[0].split()
            if msg_ids:
                # 取最新一封（最后一个 ID）
                latest_id = msg_ids[-1]
                status, msg_data = conn.fetch(latest_id, "(RFC822)")
                if status == "OK" and msg_data and msg_data[0]:
                    raw_email = msg_data[0][1]
                    code = extract_code_from_email(raw_email)
                    if code:
                        return code

        time.sleep(poll_interval_sec)

    raise VerificationCodeTimeoutError(
        f"在 {timeout_sec} 秒内未收到来自 {sender_filter} 的验证邮件"
    )
