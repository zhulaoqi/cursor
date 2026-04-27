"""Turnstile 外部求解器：CapSolver（优先）+ 2Captcha（兜底）。

两家服务只需配置其中一个即可。本模块只负责"拿 sitekey + 调 API 拿 token"，
token 注入由调用方（browser_login）执行。
"""

from __future__ import annotations

import time
from typing import Optional

import requests

from .config import SETTINGS
from .logger import get

log = get("turnstile")


CAPSOLVER_CREATE = "https://api.capsolver.com/createTask"
CAPSOLVER_RESULT = "https://api.capsolver.com/getTaskResult"
TWOCAPTCHA_IN = "https://2captcha.com/in.php"
TWOCAPTCHA_RES = "https://2captcha.com/res.php"


class TurnstileSolveError(Exception):
    pass


def has_external_solver() -> bool:
    return bool(SETTINGS.capsolver_api_key or SETTINGS.twocaptcha_api_key)


def solve(website_url: str, sitekey: str, timeout: int = 120) -> str:
    """同步求解 Turnstile，返回 token。两家服务按优先级依次尝试。"""
    if not sitekey:
        raise TurnstileSolveError("sitekey 为空，无法求解")

    errors = []
    if SETTINGS.capsolver_api_key:
        try:
            return _solve_capsolver(website_url, sitekey, timeout)
        except Exception as e:
            errors.append(f"CapSolver: {e}")
            log.warning(f"CapSolver 失败: {e}")

    if SETTINGS.twocaptcha_api_key:
        try:
            return _solve_twocaptcha(website_url, sitekey, timeout)
        except Exception as e:
            errors.append(f"2Captcha: {e}")
            log.warning(f"2Captcha 失败: {e}")

    if not errors:
        raise TurnstileSolveError("未配置任何外部求解器 API Key")
    raise TurnstileSolveError("全部求解器失败: " + " | ".join(errors))


def _solve_capsolver(website_url: str, sitekey: str, timeout: int) -> str:
    api_key = SETTINGS.capsolver_api_key
    resp = requests.post(
        CAPSOLVER_CREATE,
        json={
            "clientKey": api_key,
            "task": {
                "type": "AntiTurnstileTaskProxyLess",
                "websiteURL": website_url,
                "websiteKey": sitekey,
            },
        },
        timeout=30,
    )
    data = resp.json()
    if data.get("errorId"):
        raise TurnstileSolveError(f"CapSolver createTask: {data}")
    task_id = data["taskId"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        r = requests.post(
            CAPSOLVER_RESULT,
            json={"clientKey": api_key, "taskId": task_id},
            timeout=30,
        )
        d = r.json()
        if d.get("errorId"):
            raise TurnstileSolveError(f"CapSolver getTaskResult: {d}")
        if d.get("status") == "ready":
            token = (d.get("solution") or {}).get("token") or ""
            if not token:
                raise TurnstileSolveError(f"CapSolver 返回空 token: {d}")
            return token

    raise TurnstileSolveError(f"CapSolver 超时（{timeout}s）")


def _solve_twocaptcha(website_url: str, sitekey: str, timeout: int) -> str:
    api_key = SETTINGS.twocaptcha_api_key
    resp = requests.post(TWOCAPTCHA_IN, data={
        "key": api_key,
        "method": "turnstile",
        "sitekey": sitekey,
        "pageurl": website_url,
        "json": 1,
    }, timeout=30)
    data = resp.json()
    if data.get("status") != 1:
        raise TurnstileSolveError(f"2Captcha submit: {data}")
    cap_id = data["request"]

    deadline = time.time() + timeout
    time.sleep(10)
    while time.time() < deadline:
        r = requests.get(TWOCAPTCHA_RES, params={
            "key": api_key, "action": "get", "id": cap_id, "json": 1,
        }, timeout=30)
        d = r.json()
        if d.get("status") == 1:
            return d["request"]
        if d.get("request") != "CAPCHA_NOT_READY":
            raise TurnstileSolveError(f"2Captcha: {d}")
        time.sleep(5)

    raise TurnstileSolveError(f"2Captcha 超时（{timeout}s）")
