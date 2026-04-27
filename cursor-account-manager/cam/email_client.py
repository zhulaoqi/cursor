"""飞书 IMAP 客户端：轮询 Cursor 验证码邮件，提取 6 位数字。

每个账号用自己的邮箱 + IMAP 授权密码登录自己的收件箱（非 catch-all 模式）。
"""

from __future__ import annotations

import email
import imaplib
import os
import re
import socket
import time
from email.message import Message
from typing import Iterable

from .config import CURSOR_VERIFICATION_SENDER, SETTINGS
from .logger import get

log = get("email")


class IMAPAuthError(Exception):
    pass


class VerificationCodeTimeoutError(Exception):
    pass


_CODE_RE = re.compile(r"\b(\d{6})\b")

# 飞书/163/QQ/网易这类国产邮箱 IMAP 都要求登录后发 ID 命令，否则后续操作会阻塞
imaplib.Commands["ID"] = ("AUTH", "SELECTED", "NONAUTH")


def _send_id_command(conn: imaplib.IMAP4_SSL, email_addr: str) -> None:
    try:
        typ, dat = conn._simple_command(
            "ID",
            '("name" "cam" "version" "1.0" "contact" "' + email_addr + '")',
        )
        conn._untagged_response(typ, dat, "ID")
        log.debug("IMAP ID 命令已发送")
    except Exception as e:
        log.debug(f"IMAP ID 命令失败（可忽略）: {e}")


def _connect(host: str, port: int, user: str, password: str) -> imaplib.IMAP4_SSL:
    log.info(f"  → 建立 SSL 连接到 {host}:{port} ...")
    try:
        # 单独给 socket 一个连接超时，避免 DNS 或 TCP 层阻塞
        socket.setdefaulttimeout(20)
        conn = imaplib.IMAP4_SSL(host, port, timeout=30)
        log.info("  → SSL 握手完成")
    except (OSError, TimeoutError) as e:
        raise IMAPAuthError(f"IMAP 连接失败({user}@{host}): {e}") from e
    finally:
        socket.setdefaulttimeout(None)

    _send_id_command(conn, user)

    log.info(f"  → 登录 {user} ...")
    try:
        conn.login(user, password)
        log.info("  → 登录成功")
        return conn
    except imaplib.IMAP4.error as e:
        raise IMAPAuthError(f"IMAP 认证失败({user}@{host}): {e}") from e


def _iter_text_parts(msg: Message) -> Iterable[str]:
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        yield payload.decode(charset, errors="replace")
                    except LookupError:
                        yield payload.decode("utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                yield payload.decode(charset, errors="replace")
            except LookupError:
                yield payload.decode("utf-8", errors="replace")


def _extract_code(raw_email: bytes) -> str:
    msg = email.message_from_bytes(raw_email)
    for text in _iter_text_parts(msg):
        m = _CODE_RE.search(text)
        if m:
            return m.group(1)
    return ""


def _message_date_ts(raw: bytes) -> float:
    """解析邮件 Date 头的时间戳，失败返回 0。"""
    try:
        from email.utils import parsedate_to_datetime
        msg = email.message_from_bytes(raw)
        dt = parsedate_to_datetime(msg["Date"])
        return dt.timestamp()
    except Exception:
        return 0.0


def _get_search_folders() -> list[str]:
    """获取轮询文件夹列表，默认 INBOX + 垃圾箱，兼容不同服务商。"""
    raw = os.environ.get("IMAP_SEARCH_FOLDERS", "INBOX,Junk,Spam")
    items = [x.strip() for x in raw.split(",") if x and x.strip()]
    if not items:
        return ["INBOX"]
    # 去重并保持顺序
    out: list[str] = []
    seen = set()
    for x in items:
        if x not in seen:
            out.append(x)
            seen.add(x)
    if "INBOX" not in seen:
        out.insert(0, "INBOX")
    return out


def _should_accept_by_cutoff(msg_ts: float, cutoff_ts: float, is_new_arrival: bool) -> bool:
    """首轮基线邮件按 cutoff 过滤；新到达邮件放宽 Date 过滤以适配慢投递。"""
    if is_new_arrival:
        return True
    if msg_ts and msg_ts < cutoff_ts:
        return False
    return True


def _effective_timeout(timeout_sec: int | None, host: str) -> int:
    """非飞书等跨区域邮件系统常有分钟级延迟，默认给更长等待窗口。"""
    if timeout_sec is not None:
        return timeout_sec
    base = SETTINGS.verification_code_timeout
    if "feishu" in (host or "").lower():
        return base
    return max(base, 240)


def fetch_verification_code(
    email_addr: str,
    imap_password: str,
    *,
    host: str | None = None,
    port: int | None = None,
    sender: str = CURSOR_VERIFICATION_SENDER,
    timeout_sec: int | None = None,
    poll_interval_sec: int = 5,
    since_ts: float | None = None,
) -> str:
    """
    登录飞书 IMAP，在 timeout_sec 内轮询来自 Cursor 的验证码邮件。

    Args:
        since_ts: 只接受 Date >= 此时间戳的邮件（秒级 unix ts），防止拿到历史旧验证码。
                  默认为调用时刻前 3 分钟。

    Returns: 6 位验证码字符串。超时抛 VerificationCodeTimeoutError。
    """
    host = host or SETTINGS.default_imap_host
    port = port or SETTINGS.default_imap_port
    timeout = _effective_timeout(timeout_sec, host)
    cutoff = since_ts if since_ts is not None else time.time() - 180

    log.info(f"[{email_addr}] 连接 IMAP {host}:{port} ...")
    conn = _connect(host, port, email_addr, imap_password)
    try:
        deadline = time.time() + timeout
        seen_ids: set[tuple[str, bytes]] = set()
        baseline_ids: dict[str, set[bytes]] = {}
        poll_count = 0
        folders = _get_search_folders()

        # 搜索策略：兼容多种发件域名和邮箱服务商的 IMAP 搜索方言
        # Cursor 实际发件人可能是 no-reply@cursor.com 或 no-reply@cursor.sh
        search_queries = [
            f'(FROM "{sender}")',
            '(FROM "cursor.com")',
            '(FROM "cursor.sh")',
            '(HEADER Subject "Cursor")',
            '(HEADER Subject "verification")',
            "(UNSEEN)",  # 最后兜底：所有未读邮件，客户端自己过滤
        ]

        while time.time() < deadline:
            poll_count += 1
            total_candidates = 0

            for folder in folders:
                try:
                    conn.select(folder)
                except imaplib.IMAP4.abort:
                    log.warning(f"[{email_addr}] IMAP 连接中断，重连...")
                    try:
                        conn.logout()
                    except Exception:
                        pass
                    conn = _connect(host, port, email_addr, imap_password)
                    break
                except imaplib.IMAP4.error:
                    # 不同服务商垃圾箱命名不同，选不到直接跳过
                    continue

                # 尝试每个搜索条件，聚合候选集（去重后按新到旧处理）
                all_ids_set: set[bytes] = set()
                for q in search_queries:
                    try:
                        status, data = conn.search(None, q)
                        if status == "OK" and data and data[0]:
                            mids = data[0].split()
                            all_ids_set.update(mids)
                            if poll_count == 1:
                                log.debug(f"  [{folder}] 搜索 {q} → {len(mids)} 封")
                    except imaplib.IMAP4.error as e:
                        log.debug(f"  [{folder}] 搜索 {q} 失败: {e}")

                if not all_ids_set:
                    continue

                all_ids = sorted(all_ids_set, key=lambda x: int(x), reverse=True)
                total_candidates += len(all_ids)
                if poll_count == 1:
                    baseline_ids[folder] = set(all_ids)

                for mid in all_ids:
                    key = (folder, mid)
                    if key in seen_ids:
                        continue
                    seen_ids.add(key)

                    is_new_arrival = mid not in baseline_ids.get(folder, set())

                    try:
                        st, payload = conn.fetch(mid, "(RFC822)")
                    except imaplib.IMAP4.error:
                        continue
                    if st != "OK" or not payload or not payload[0]:
                        continue

                    raw = payload[0][1]
                    msg_ts = _message_date_ts(raw)
                    if not _should_accept_by_cutoff(msg_ts, cutoff, is_new_arrival):
                        continue

                    # 验证真的是 Cursor 的
                    try:
                        msg_obj = email.message_from_bytes(raw)
                        from_hdr = (msg_obj.get("From") or "").lower()
                        subj_hdr = (msg_obj.get("Subject") or "").lower()
                    except Exception:
                        from_hdr, subj_hdr = "", ""

                    if ("cursor" not in from_hdr) and ("cursor" not in subj_hdr):
                        continue

                    code = _extract_code(raw)
                    if code:
                        log.info(f"[{email_addr}] 拿到验证码: {code}（Folder={folder}, From={from_hdr[:60]}）")
                        return code

            if poll_count == 1:
                log.info(
                    f"[{email_addr}] 轮询开始（文件夹={','.join(folders)}，间隔 {poll_interval_sec}s，超时 {timeout}s），"
                    f"首轮候选 {total_candidates} 封"
                )
            elif poll_count % 3 == 0:
                log.info(f"[{email_addr}] 已轮询 {poll_count} 次，暂无新验证邮件，继续等...")
            time.sleep(poll_interval_sec)

        raise VerificationCodeTimeoutError(
            f"[{email_addr}] {timeout}s 内未收到 Cursor 验证邮件（轮询 {poll_count} 次）"
        )
    finally:
        try:
            conn.logout()
        except Exception:
            pass
