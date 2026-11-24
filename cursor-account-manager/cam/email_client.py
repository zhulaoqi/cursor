"""飞书 IMAP 客户端：轮询 Cursor 验证码邮件，提取 6 位数字。

默认每个账号用自己的邮箱 + IMAP 授权密码登录自己的收件箱。

``@eclicktech.com.cn`` 做了邮箱组后，验证码都进组长邮箱（默认 kinch.zhu@eclicktech.com.cn）。
这时用组长邮箱登录，再按邮件「收件人」判断是哪个账号在登录。
先只对 ``IMAP_GROUP_MAILBOX_ACCOUNTS`` 里列出的账号生效，避免没改授权码的账号被定时任务一起带挂。
"""

from __future__ import annotations

import email
import imaplib
import os
import re
import socket
import time
from email.message import Message
from email.utils import getaddresses

from .config import CURSOR_VERIFICATION_SENDER, SETTINGS
from .logger import get

log = get("email")


class IMAPAuthError(Exception):
    pass


class VerificationCodeTimeoutError(Exception):
    pass


_CODE_RE = re.compile(r"\b(\d{6})\b")
# 验证码正文紧挨着「验证码」出现。中间可以隔开 HTML 标签和空白，但不能隔开另一个六位数。
_CODE_AFTER_LABEL_RE = re.compile(
    r"(?:一次性验证码|验证码|verification code|one-time code|login code)"
    r"(?:(?!\d{6}).){0,300}(\d{6})",
    re.I | re.S,
)
_HEX_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{3,8}\b")
_STYLE_BLOCK_RE = re.compile(r"(?is)<(script|style)\b[^>]*>.*?</\1>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_SIMPLE_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# 邮箱组：域名 → 真正收信的组长邮箱。验证码进这一个收件箱，靠收件人区分账号。
_DEFAULT_GROUP_MAILBOXES = "eclicktech.com.cn=kinch.zhu@eclicktech.com.cn"
# 先只切这两个账号做登录测试。全部授权码改完后，设成 *@eclicktech.com.cn。
_DEFAULT_GROUP_ACCOUNTS = "cursor119@eclicktech.com.cn,cursor132@eclicktech.com.cn"
# 飞书邮箱组一般保留原始收件人在 To；其余头是转发/网关改写时的兜底。
_RECIPIENT_HEADERS = (
    "To", "Cc", "Delivered-To", "X-Original-To", "Envelope-To", "X-Envelope-To", "Resent-To",
)

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


def _visible_text(text: str, is_html: bool) -> str:
    """去掉样式和颜色值。Cursor 邮件里的灰色 ``#414141`` 会被 ``\\d{6}`` 误当成验证码。"""
    if is_html:
        text = _STYLE_BLOCK_RE.sub(" ", text)
        text = _HEX_COLOR_RE.sub(" ", text)
        text = _TAG_RE.sub(" ", text)
    else:
        text = _HEX_COLOR_RE.sub(" ", text)
    return text


def _code_from_text(text: str) -> str:
    labeled = _CODE_AFTER_LABEL_RE.search(text)
    if labeled:
        return labeled.group(1)
    found = _CODE_RE.search(text)
    return found.group(1) if found else ""


def _extract_code(raw_email: bytes) -> str:
    msg = email.message_from_bytes(raw_email)
    plain: list[str] = []
    html: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except LookupError:
                decoded = payload.decode("utf-8", errors="replace")
            (plain if ctype == "text/plain" else html).append(decoded)
    else:
        ctype = msg.get_content_type()
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        try:
            decoded = payload.decode(charset, errors="replace")
        except LookupError:
            decoded = payload.decode("utf-8", errors="replace")
        (html if ctype == "text/html" else plain).append(decoded)

    # 纯文本优先：HTML 里的颜色、宽度经常是六位数字
    for text in plain:
        code = _code_from_text(_visible_text(text, is_html=False))
        if code:
            return code
    for text in html:
        code = _code_from_text(_visible_text(text, is_html=True))
        if code:
            return code
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


def group_mailboxes() -> dict[str, str]:
    """``IMAP_GROUP_MAILBOXES=域名=组长邮箱``，多个用逗号分隔。"""
    raw = os.environ.get("IMAP_GROUP_MAILBOXES", _DEFAULT_GROUP_MAILBOXES)
    out: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if "=" not in item:
            continue
        domain, mailbox = item.split("=", 1)
        domain, mailbox = domain.strip().lower(), mailbox.strip().lower()
        if domain and _SIMPLE_EMAIL_RE.match(mailbox):
            out[domain] = mailbox
    return out


def group_mailbox_accounts() -> set[str]:
    """哪些账号改走邮箱组。``*`` 表示全部已配置域名，``*@域名`` 表示该域名全部。"""
    raw = os.environ.get("IMAP_GROUP_MAILBOX_ACCOUNTS", _DEFAULT_GROUP_ACCOUNTS)
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def group_mailbox_for(account_email: str) -> str | None:
    """这个账号的验证码是否改从组长邮箱收。不是则返回 None（仍登录自己的邮箱）。"""
    email_addr = (account_email or "").strip().lower()
    if "@" not in email_addr:
        return None
    domain = email_addr.split("@", 1)[1]
    mailbox = group_mailboxes().get(domain)
    if not mailbox or mailbox == email_addr:
        return None
    allowed = group_mailbox_accounts()
    if "*" in allowed or email_addr in allowed or f"*@{domain}" in allowed or domain in allowed:
        return mailbox
    return None


def _resolve_mailbox(
    account_email: str,
    mailbox_user: str | None,
    recipient: str | None,
) -> tuple[str, str | None]:
    """返回 (IMAP 登录用户, 必须匹配的收件人)。收件人为空表示不按收件人过滤。"""
    if mailbox_user is not None or recipient is not None:
        login = (mailbox_user or account_email).strip()
        want = recipient.strip().lower() if recipient else None
        return login, want or None
    group = group_mailbox_for(account_email)
    if group:
        return group, account_email.strip().lower()
    return account_email, None


def _addressed_to(msg: Message, recipient: str) -> bool:
    """邮件收件人里是否包含正在登录的账号。对不上就跳过，避免拿成别人的验证码。"""
    want = recipient.strip().lower()
    if not want:
        return False
    values: list[str] = []
    for header in _RECIPIENT_HEADERS:
        values.extend(msg.get_all(header) or [])
    for _name, addr in getaddresses(values):
        if addr and addr.strip().lower() == want:
            return True
    # 个别网关把地址写成不带尖括号的原文，getaddresses 拆不开时再扫一遍原文
    blob = " ".join(values).lower()
    return want in blob


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
    sender_keywords: tuple[str, ...] = ("cursor",),
    extra_search_queries: tuple[str, ...] | None = None,
    product_label: str = "Cursor",
    mailbox_user: str | None = None,
    recipient: str | None = None,
) -> str:
    """
    登录飞书 IMAP，在 timeout_sec 内轮询来自 Cursor（或 sender_keywords 指定平台）的验证码邮件。

    Args:
        since_ts: 只接受 Date >= 此时间戳的邮件（秒级 unix ts），防止拿到历史旧验证码。
                  默认为调用时刻前 3 分钟。
        mailbox_user: IMAP 登录用户。邮箱组场景是组长邮箱；默认按账号域名自动判断。
        recipient: 只接受收件人包含该地址的邮件。邮箱组场景是正在登录的账号。

    Returns: 6 位验证码字符串。超时抛 VerificationCodeTimeoutError。
    """
    host = host or SETTINGS.default_imap_host
    port = port or SETTINGS.default_imap_port
    timeout = _effective_timeout(timeout_sec, host)
    cutoff = since_ts if since_ts is not None else time.time() - 180
    login_user, want_recipient = _resolve_mailbox(email_addr, mailbox_user, recipient)

    if want_recipient and login_user.lower() != email_addr.strip().lower():
        log.info(f"[{email_addr}] 验证码从邮箱组 {login_user} 收取，按收件人匹配")
    log.info(f"[{email_addr}] 连接 IMAP {host}:{port} 用户 {login_user} ...")
    conn = _connect(host, port, login_user, imap_password)
    try:
        deadline = time.time() + timeout
        seen_ids: set[tuple[str, bytes]] = set()
        baseline_ids: dict[str, set[bytes]] = {}
        poll_count = 0
        folders = _get_search_folders()

        # 搜索策略：兼容多种发件域名和邮箱服务商的 IMAP 搜索方言
        # Cursor 实际发件人可能是 no-reply@cursor.com 或 no-reply@cursor.sh
        if extra_search_queries is None:
            search_queries = [
                f'(FROM "{sender}")',
                '(FROM "cursor.com")',
                '(FROM "cursor.sh")',
                '(HEADER Subject "Cursor")',
                '(HEADER Subject "verification")',
                "(UNSEEN)",  # 最后兜底：所有未读邮件，客户端自己过滤
            ]
        else:
            search_queries = list(extra_search_queries)
        # 邮箱组收件箱里有很多人的验证码。服务端支持 TO 搜索时只拉这个账号的，避免扫整箱又拿错人。
        if want_recipient and _SIMPLE_EMAIL_RE.match(want_recipient):
            targeted = [f'(TO "{want_recipient}")', f'(HEADER TO "{want_recipient}")']
            search_queries = targeted + search_queries
        keywords = tuple(k.lower() for k in sender_keywords if k)

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
                    conn = _connect(host, port, login_user, imap_password)
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

                    if not any(k in from_hdr or k in subj_hdr for k in keywords):
                        continue
                    if want_recipient and not _addressed_to(msg_obj, want_recipient):
                        log.debug(
                            f"[{email_addr}] 跳过收件人不匹配的邮件 "
                            f"To={msg_obj.get('To', '')[:80]}"
                        )
                        continue

                    code = _extract_code(raw)
                    if code:
                        who = f", To={want_recipient}" if want_recipient else ""
                        log.info(
                            f"[{email_addr}] 拿到验证码: {code}"
                            f"（Folder={folder}, From={from_hdr[:60]}{who}）"
                        )
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
            f"[{email_addr}] {timeout}s 内未收到 {product_label} 验证邮件（轮询 {poll_count} 次）"
        )
    finally:
        try:
            conn.logout()
        except Exception:
            pass
