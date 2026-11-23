"""Devin 采集失败原因：统一错误码 + 中文标签，用于看板与飞书播报聚合。"""

from __future__ import annotations

from .email_client import IMAPAuthError, VerificationCodeTimeoutError

# code -> 中文说明（播报与看板展示）
ERROR_LABELS: dict[str, str] = {
    "imap_auth": "邮箱 IMAP 认证失败",
    "code_timeout": "验证码邮件超时未收到",
    "code_invalid": "验证码被拒绝/已过期",
    "account_not_found": "Devin 账号不存在或被拒绝登录",
    "login_failed": "登录未完成（停留在登录/验证页）",
    "page_parse": "页面已打开但未解析到用量/套餐",
    "network": "网络/代理超时",
    "browser": "浏览器启动或崩溃",
    "timeout": "单账号采集超时",
    "unknown": "未知错误",
}


class DevinError(Exception):
    """带错误码的 Devin 采集异常。"""

    def __init__(self, code: str, message: str = ""):
        self.code = code if code in ERROR_LABELS else "unknown"
        self.message = message or ERROR_LABELS[self.code]
        super().__init__(self.message)


def error_label(code: str) -> str:
    return ERROR_LABELS.get(code or "", ERROR_LABELS["unknown"])


def classify_exception(exc: BaseException) -> tuple[str, str]:
    """异常 → (错误码, 简短信息)。"""
    if isinstance(exc, DevinError):
        return exc.code, exc.message
    if isinstance(exc, IMAPAuthError):
        return "imap_auth", str(exc)
    if isinstance(exc, VerificationCodeTimeoutError):
        return "code_timeout", str(exc)
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    if "timeout" in low or "timed out" in low:
        if "err_proxy" in low or "net::" in low or "navigation" in low:
            return "network", text[:500]
        return "timeout", text[:500]
    if any(k in low for k in ("net::", "err_proxy", "err_connection", "err_name", "connection reset")):
        return "network", text[:500]
    if any(k in low for k in ("target closed", "browser has been closed", "executable doesn't exist", "chrome")):
        return "browser", text[:500]
    return "unknown", text[:500]
