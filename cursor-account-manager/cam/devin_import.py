"""Devin 账号导入：CSV / Excel 解析 + 模板生成（与 Cursor 导入互相独立）。

必填列：email、imap_password（别名 imap_pwd / password / 邮箱授权码）
可选列：imap_host、imap_port、owner_email（别名 feishu_email / 飞书邮箱 / owner / 负责人）、note（别名 备注）
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Any, Iterable

_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\ufeff\u2060]")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

TEMPLATE_HEADERS = ["email", "imap_password", "imap_host", "imap_port", "owner_email", "note"]
TEMPLATE_EXAMPLE = [
    "cursor456@eclicktech.com.cn",
    "YourImapPassword",
    "imap.feishu.cn",
    993,
    "owner@example.com",
    "Devin Max 账号",
]

_ALIASES = {
    "email": ("email", "邮箱", "账号"),
    "imap_password": ("imap_password", "imap_pwd", "password", "邮箱授权码", "授权码", "密码"),
    "imap_host": ("imap_host", "host"),
    "imap_port": ("imap_port", "port"),
    "owner_email": ("owner_email", "feishu_email", "飞书邮箱", "owner", "负责人"),
    "note": ("note", "备注", "remark"),
}


@dataclass
class ImportRow:
    email: str
    imap_password: str
    imap_host: str | None = None
    imap_port: int | None = None
    owner_email: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "imap_password": self.imap_password,
            "imap_host": self.imap_host,
            "imap_port": self.imap_port,
            "owner_email": self.owner_email,
            "note": self.note,
        }


class DevinImportError(ValueError):
    """导入文件格式错误（给前端展示）。"""


def normalize_email(value: Any) -> str:
    if value is None:
        return ""
    s = _ZERO_WIDTH.sub("", str(value)).strip().lower()
    return re.sub(r"\s+", "", s)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.lower() == "none" else s


def _pick(d: dict[str, Any], key: str) -> str:
    for alias in _ALIASES[key]:
        if alias in d and _cell(d[alias]):
            return _cell(d[alias])
    return ""


def _rows_from_dicts(dicts: Iterable[dict[str, Any]], *, line_offset: int = 2) -> tuple[list[ImportRow], list[str]]:
    rows: list[ImportRow] = []
    warnings: list[str] = []
    for idx, d in enumerate(dicts, start=line_offset):
        email = normalize_email(_pick(d, "email"))
        password = _pick(d, "imap_password")
        if not email and not password:
            continue
        if not _EMAIL_RE.match(email):
            warnings.append(f"第 {idx} 行邮箱格式不正确，已跳过: {email or '(空)'}")
            continue
        if not password:
            warnings.append(f"第 {idx} 行缺少 imap_password，已跳过: {email}")
            continue
        port_raw = _pick(d, "imap_port")
        port = int(float(port_raw)) if re.fullmatch(r"\d+(?:\.0+)?", port_raw or "") else None
        owner = normalize_email(_pick(d, "owner_email"))
        rows.append(
            ImportRow(
                email=email,
                imap_password=password,
                imap_host=_pick(d, "imap_host") or None,
                imap_port=port,
                owner_email=owner,
                note=_pick(d, "note")[:500],
            )
        )
    return rows, warnings


def _check_headers(headers: set[str]) -> None:
    has_email = any(a in headers for a in _ALIASES["email"])
    has_pwd = any(a in headers for a in _ALIASES["imap_password"])
    if not has_email:
        raise DevinImportError("缺少必需列: email")
    if not has_pwd:
        raise DevinImportError("缺少必需列: imap_password")


def parse_csv(data: bytes) -> tuple[list[ImportRow], list[str]]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = {str(h).strip().lower() for h in (reader.fieldnames or []) if h is not None}
    _check_headers(headers)
    dicts = [
        {(str(k).strip().lower() if k is not None else ""): v for k, v in r.items()}
        for r in reader
    ]
    return _rows_from_dicts(dicts)


def parse_excel(data: bytes) -> tuple[list[ImportRow], list[str]]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), data_only=True)
    best: tuple[list[ImportRow], list[str]] = ([], [])
    header_error: DevinImportError | None = None
    for ws in wb.worksheets:
        headers: list[str] = []
        dicts: list[dict[str, Any]] = []
        for row in ws.iter_rows(values_only=True):
            if all(c is None or str(c).strip() == "" for c in row):
                continue
            if not headers:
                headers = [str(c).strip().lower() if c is not None else "" for c in row]
                try:
                    _check_headers(set(headers))
                except DevinImportError as exc:
                    header_error = exc
                    break
                continue
            dicts.append(dict(zip(headers, row)))
        if headers and header_error is None:
            parsed = _rows_from_dicts(dicts)
            if len(parsed[0]) > len(best[0]):
                best = parsed
        elif header_error is not None and not best[0]:
            continue
    if not best[0] and header_error is not None:
        raise header_error
    return best


def parse_accounts_file(filename: str, data: bytes) -> tuple[list[ImportRow], list[str]]:
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        return parse_excel(data)
    if name.endswith((".csv", ".txt")):
        return parse_csv(data)
    try:
        return parse_csv(data)
    except Exception:
        return parse_excel(data)


def build_template_xlsx() -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "devin_accounts"
    ws.append(TEMPLATE_HEADERS)
    ws.append(TEMPLATE_EXAMPLE)
    fill = PatternFill(fill_type="solid", fgColor="EAF2FF")
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = fill
    for idx, width in enumerate((32, 24, 20, 10, 30, 28), start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
