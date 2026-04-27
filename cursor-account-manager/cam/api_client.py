"""Cursor 内部 API 客户端。

两个域名两套认证：
  - api2.cursor.sh       → Authorization: Bearer <access_token>
  - cursor.com (web API) → Cookie: WorkosCursorSessionToken=<access_token>

遇到 401 / 403 → 抛 TokenExpiredError，由 token_manager 捕获后刷新。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

import requests

from .config import CURSOR_API2_BASE, CURSOR_WEB_BASE, SETTINGS
from .logger import get
from .models import TokenExpiredError

log = get("api")


DEFAULT_TIMEOUT = 30


def _split_session_token(raw: str) -> tuple[str, str]:
    """把 WorkosCursorSessionToken 拆成 (cookie_value, jwt_only)。

    - cookie_value: URL-encoded 的 `<user_id>%3A%3A<jwt>`（cursor.com 用）
    - jwt_only:     纯 JWT（api2.cursor.sh 的 Bearer 只接受这个）

    兼容 3 种输入：
      a) URL-encoded: user_id%3A%3A<jwt>
      b) URL-decoded: user_id::<jwt>
      c) 只有 JWT 本身（旧 PKCE 路径留下的）
    """
    if not raw:
        return "", ""
    decoded = unquote(raw)
    if "::" in decoded:
        jwt = decoded.split("::", 1)[1]
    else:
        jwt = decoded
    # cookie 值要保持 URL-encoded（cursor.com 要这个形态）
    cookie_val = raw if "%3A%3A" in raw else decoded.replace("::", "%3A%3A")
    return cookie_val, jwt


class CursorClient:
    def __init__(self, access_token: str, *, proxy: Optional[str] = None):
        if not access_token:
            raise ValueError("access_token 为空")
        self.access_token = access_token
        self._cookie_value, self._bearer_jwt = _split_session_token(access_token)
        self.proxy = proxy if proxy is not None else SETTINGS.proxy
        self._session = requests.Session()
        if self.proxy:
            self._session.proxies.update({"http": self.proxy, "https": self.proxy})

    def close(self) -> None:
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ─── 通用请求 ────────────────────────────────────────────────────

    def _request(
        self, method: str, url: str,
        *, headers: Optional[dict] = None,
        json_body: Any = None, stream: bool = False,
    ) -> requests.Response:
        h = dict(headers or {})
        if url.startswith(CURSOR_API2_BASE):
            # api2 的 Bearer 只接受纯 JWT（不能带 user_id%3A%3A 前缀）
            h.setdefault("Authorization", f"Bearer {self._bearer_jwt}")
            h.setdefault("Content-Type", "application/json")
        elif url.startswith(CURSOR_WEB_BASE):
            # cursor.com 的 Cookie 要 URL-encoded 的 <user_id>%3A%3A<jwt>
            h.setdefault("Cookie", f"WorkosCursorSessionToken={self._cookie_value}")
            h.setdefault("Accept", "application/json")
        h.setdefault("User-Agent",
                     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

        resp = self._session.request(
            method, url, headers=h,
            json=json_body,
            timeout=DEFAULT_TIMEOUT,
            stream=stream,
        )

        if resp.status_code in (401, 403):
            raise TokenExpiredError(
                f"{method} {url} → {resp.status_code}: {resp.text[:200]}"
            )
        return resp

    def _post_json(self, path: str, body: Any = None) -> dict:
        url = f"{CURSOR_API2_BASE}{path}"
        resp = self._request("POST", url, json_body=body or {})
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"_raw": resp.text}

    def _get_json(self, url: str) -> dict:
        resp = self._request("GET", url)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"_raw": resp.text}

    # ─── Dashboard APIs (api2.cursor.sh) ────────────────────────────

    def get_current_period_usage(self) -> dict:
        return self._post_json(
            "/aiserver.v1.DashboardService/GetCurrentPeriodUsage",
        )

    def get_plan_info(self) -> dict:
        return self._post_json(
            "/aiserver.v1.DashboardService/GetPlanInfo",
        )

    def get_usage_limit_status(self) -> dict:
        return self._post_json(
            "/aiserver.v1.DashboardService/GetUsageLimitStatusAndActiveGrants",
        )

    def get_usage_events_web(
        self, page: int = 1, page_size: int = 100,
        *, start_ts: Optional[int] = None, end_ts: Optional[int] = None,
    ) -> dict:
        """Web 端点拉取使用明细一页（支持跨账单周期日期过滤）。

        cursor.com/api/dashboard/get-filtered-usage-events
        参数（来自 lixwen/cursor-usage-monitor 开源实现）：
          teamId   : 0（个人账号）
          startDate: 毫秒时间戳字符串
          endDate  : 毫秒时间戳字符串
          page     : 1-based
          pageSize : 每页条数
        """
        import time as _time
        body: dict = {
            "teamId": 0,
            "page": page,
            "pageSize": page_size,
        }
        if start_ts is not None:
            body["startDate"] = str(start_ts * 1000)
        if end_ts is not None:
            body["endDate"] = str(end_ts * 1000)
        else:
            body["endDate"] = str(int(_time.time() * 1000))

        url = f"{CURSOR_WEB_BASE}/api/dashboard/get-filtered-usage-events"
        resp = self._request(
            "POST", url,
            headers={
                "Content-Type": "application/json",
                "Origin": CURSOR_WEB_BASE,
                "Referer": f"{CURSOR_WEB_BASE}/dashboard",
            },
            json_body=body,
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"_raw": resp.text}

    def get_usage_events_grpc(
        self, page_index: int = 0, page_size: int = 100,
    ) -> dict:
        """gRPC 端点拉取使用明细一页（只返回当前账单周期，无日期参数）。"""
        body: dict = {"pageIndex": page_index, "pageSize": page_size}
        return self._post_json(
            "/aiserver.v1.DashboardService/GetFilteredUsageEvents",
            body,
        )

    def export_usage_events_csv(
        self,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        strategy: str = "tokens",
    ) -> str:
        """GET /api/dashboard/export-usage-events-csv?startDate={ms}&endDate={ms}&strategy=tokens
        登录态直接返回 CSV 文本，含完整 token 用量明细。
        start_ts / end_ts 单位为秒（Unix），内部自动转为毫秒。
        """
        import time as _time
        now_ms = int(_time.time() * 1000)
        s_ms = (start_ts * 1000) if start_ts else (now_ms - 30 * 24 * 3600 * 1000)
        e_ms = (end_ts   * 1000) if end_ts   else now_ms
        url = (
            f"{CURSOR_WEB_BASE}/api/dashboard/export-usage-events-csv"
            f"?startDate={s_ms}&endDate={e_ms}&strategy={strategy}"
        )
        resp = self._request("GET", url, headers={"Accept": "text/csv,*/*"})
        resp.raise_for_status()
        text = resp.text
        log.info(
            f"export_usage_events_csv: {resp.status_code}, "
            f"{len(text)} 字节, 前100字: {text[:100]!r}"
        )
        return text

    def iter_all_usage_events(
        self, *, page_size: int = 100, max_pages: int = 200,
        start_ts: Optional[int] = None, end_ts: Optional[int] = None,
    ) -> list[dict]:
        """分页遍历使用明细，返回扁平的 event 列表。

        优先使用 Web 端点（支持跨账单周期日期范围过滤）。
        若 Web 端点失败（4xx/5xx/网络异常），自动降级到 gRPC 端点（仅当前周期）。
        """
        # ── 优先：Web 端点，支持 startDate/endDate 跨周期 ──
        try:
            all_events: list[dict] = []
            total: Optional[int] = None
            for page in range(1, max_pages + 1):
                resp = self.get_usage_events_web(page, page_size,
                                                 start_ts=start_ts, end_ts=end_ts)
                # 若响应体包含错误键（如 {"error": "..."}），视为失败
                if "_raw" in resp or "error" in resp:
                    raise ValueError(f"Web 端点返回异常: {resp}")
                events = resp.get("usageEventsDisplay") or []
                if total is None:
                    try:
                        total = int(resp.get("totalUsageEventsCount") or 0)
                    except (TypeError, ValueError):
                        total = 0
                if not events:
                    break
                all_events.extend(events)
                if total and len(all_events) >= total:
                    break
                if len(events) < page_size:
                    break
            log.info(f"Web 端点拉取使用明细成功: {len(all_events)} 条")
            return all_events

        except Exception as web_err:
            log.warning(f"Web 端点失败 ({web_err})，降级到 gRPC 端点（仅当前账单周期）")

        # ── 降级：gRPC 端点，仅当前账单周期 ──
        all_events = []
        total = None
        for page_index in range(max_pages):
            resp = self.get_usage_events_grpc(page_index, page_size)
            events = resp.get("usageEventsDisplay") or []
            if total is None:
                try:
                    total = int(resp.get("totalUsageEventsCount") or 0)
                except (TypeError, ValueError):
                    total = 0
            if not events:
                break
            all_events.extend(events)
            if total and len(all_events) >= total:
                break
            if len(events) < page_size:
                break
        log.info(f"gRPC 端点拉取使用明细成功（当前周期）: {len(all_events)} 条")
        return all_events

    # ─── Web APIs (cursor.com) ──────────────────────────────────────

    def get_stripe_info(self) -> dict:
        return self._get_json(f"{CURSOR_WEB_BASE}/api/auth/stripe")

    def list_invoices(self) -> list[dict]:
        """拉取账单列表。

        Cursor 的账单端点历史上改过几次路径，按顺序尝试。
        响应可能是：
          A. 发票对象列表 → 直接返回
          B. {"invoices": [...]} 包装格式 → 取 invoices 字段
          C. {"url": "https://billing.stripe.com/..."} 账单门户 URL → 返回空（无法无认证直接下载）
          D. 其他 → 记录原始响应便于诊断
        """
        candidates = [
            f"{CURSOR_WEB_BASE}/api/dashboard/get-invoices",
            f"{CURSOR_WEB_BASE}/api/dashboard/invoices",
            f"{CURSOR_WEB_BASE}/api/invoices",
            f"{CURSOR_WEB_BASE}/api/auth/stripe_invoices",
        ]
        for url in candidates:
            try:
                resp = self._request("GET", url)
                if resp.status_code == 404:
                    log.info(f"list_invoices: {url} → 404 跳过")
                    continue
                if not resp.ok:
                    log.debug(f"list_invoices: {url} → {resp.status_code}（跳过）")
                    continue

                raw_preview = resp.text[:500]
                log.debug(f"list_invoices: {url} → {resp.status_code}, 响应预览: {raw_preview}")

                try:
                    data = resp.json()
                except Exception:
                    log.debug(f"list_invoices: {url} 响应不是 JSON，跳过")
                    continue

                # A. 直接是列表
                if isinstance(data, list):
                    log.info(f"list_invoices: 取到 {len(data)} 条发票（列表格式）")
                    return data

                if isinstance(data, dict):
                    # B. 包装格式 {"invoices":[], "data":[], "items":[], "results":[]}
                    for key in ("invoices", "data", "items", "results"):
                        v = data.get(key)
                        if isinstance(v, list):
                            log.info(f"list_invoices: 从 '{key}' 字段取到 {len(v)} 条发票")
                            return v

                    # C. 账单门户 URL（不含真实发票对象）
                    portal_url = data.get("url") or data.get("portalUrl") or data.get("portal_url")
                    if portal_url and isinstance(portal_url, str) and portal_url.startswith("http"):
                        log.debug(
                            f"list_invoices: 端点返回账单门户 URL（已改用浏览器方式下载）: {portal_url[:80]}"
                        )
                        return []

                    # D. 单个发票对象
                    if "id" in data or "invoiceId" in data or "invoice_pdf" in data:
                        log.info("list_invoices: 响应为单个发票对象，包装成列表")
                        return [data]

                    log.debug(
                        f"list_invoices: {url} 响应格式无法识别（keys={list(data.keys())}）"
                    )
                    return []

            except TokenExpiredError:
                raise
            except Exception as e:
                log.debug(f"list_invoices: {url} 异常: {e}")
                continue

        log.debug("list_invoices: 所有 API 端点均无数据（将用浏览器方式下载账单）")
        return []

    def download_invoice_pdf(self, pdf_url: str, save_path: Path) -> None:
        resp = self._request("GET", pdf_url, stream=True)
        resp.raise_for_status()
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with save_path.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
