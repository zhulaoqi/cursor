# cursor_auth.py - Cursor Token 获取 & 本地数据库更新
# 学习自 wf-cursor-auto-free

import base64
import hashlib
import json
import os
import sqlite3
import sys
import time
import uuid

import requests


# ═══════════════════════════════════════════════════════════════════════
# Token 获取（通过 loginDeepControl + auth poll）
# ═══════════════════════════════════════════════════════════════════════

def _generate_auth_params() -> dict:
    """生成 PKCE 认证参数（code_verifier / challenge / uuid）。"""
    t = os.urandom(32)

    def tb(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    s = tb(t)
    n = tb(hashlib.sha256(s.encode()).digest())
    r = str(uuid.uuid4())

    return {"s": s, "n": n, "r": r}


def _poll_for_login_result(
    auth_uuid: str, verifier: str,
    max_attempts: int = 30, interval: int = 2,
) -> tuple:
    """轮询 Cursor auth poll API 获取 token。

    Returns: (auth_id, access_token, refresh_token) 或三个 None
    """
    poll_url = f"https://api2.cursor.sh/auth/poll?uuid={auth_uuid}&verifier={verifier}"
    headers = {"Content-Type": "application/json"}

    for attempt in range(max_attempts):
        try:
            resp = requests.get(poll_url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if all(k in data for k in ("authId", "accessToken", "refreshToken")):
                    return data["authId"], data["accessToken"], data["refreshToken"]
            elif resp.status_code == 404:
                pass  # 登录尚未完成
        except Exception as e:
            print(f"[Auth Poll] 请求失败: {e}")

        time.sleep(interval)

    return None, None, None


def get_cursor_session_token(tab, max_attempts: int = 3) -> tuple:
    """注册/登录完成后，获取 Cursor 的 access_token 和 refresh_token。

    流程：
    1. 生成 PKCE 参数
    2. 导航到 loginDeepControl 页面
    3. 等待页面加载并点击确认
    4. 轮询 auth poll API 获取 token

    Returns: (access_token, refresh_token) 或 (None, None)
    """
    params = _generate_auth_params()
    url = (
        f"https://www.cursor.com/cn/loginDeepControl"
        f"?challenge={params['n']}&uuid={params['r']}&mode=login"
    )

    print(f"[Token] 导航到 loginDeepControl ...")
    tab.get(url)

    # 等待页面加载（旧确认页或新的 Continue / Return）
    for attempt in range(max_attempts):
        try:
            if tab.ele("You're currently logged in as:", timeout=5):
                break
        except Exception:
            pass
        try:
            body = (tab.ele("tag:body", timeout=2).text or "") if tab.ele("tag:body", timeout=2) else ""
            if "Continue to sign in" in body or "Return to Cursor" in body or "All set" in body:
                break
        except Exception:
            pass
        time.sleep(2)

    time.sleep(2)

    for text in ("Continue to sign in", "Return to Cursor", "Yes, Log In", "Yes"):
        try:
            btn = tab.ele(f"text:{text}", timeout=2)
            if btn:
                btn.click()
                print(f"[Token] 已点击 {text}")
                time.sleep(1)
        except Exception:
            continue

    # 点击确认按钮
    try:
        tab.run_js("""
            try {
                const button = document.querySelectorAll(".min-h-screen")[1]
                    .querySelectorAll(".gap-4")[1]
                    .querySelectorAll("button")[1];
                if (button) { button.click(); return true; }
                return false;
            } catch (e) { return false; }
        """)
    except Exception as e:
        print(f"[Token] 点击确认按钮失败: {e}")

    # 轮询获取 token
    print("[Token] 轮询 auth poll ...")
    _, access_token, refresh_token = _poll_for_login_result(params["r"], params["s"])

    if access_token and refresh_token:
        print("[Token] 获取成功")
    else:
        print("[Token] 获取失败")

    return access_token, refresh_token


# ═══════════════════════════════════════════════════════════════════════
# Cursor 本地数据库写入
# ═══════════════════════════════════════════════════════════════════════

def _get_cursor_db_path() -> str:
    if sys.platform == "win32":
        appdata = os.getenv("APPDATA")
        if not appdata:
            raise EnvironmentError("APPDATA 环境变量未设置")
        return os.path.join(appdata, "Cursor", "User", "globalStorage", "state.vscdb")
    elif sys.platform == "darwin":
        return os.path.abspath(os.path.expanduser(
            "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb"
        ))
    elif sys.platform == "linux":
        return os.path.abspath(os.path.expanduser(
            "~/.config/Cursor/User/globalStorage/state.vscdb"
        ))
    else:
        raise NotImplementedError(f"不支持的操作系统: {sys.platform}")


def update_cursor_auth(
    email: str,
    access_token: str,
    refresh_token: str,
) -> bool:
    """将注册获得的 Token 写入 Cursor 本地 SQLite 数据库。

    写入后 Cursor 客户端重启即可直接登录，无需再走浏览器登录流程。
    """
    db_path = _get_cursor_db_path()
    if not os.path.isfile(db_path):
        print(f"[CursorAuth] 数据库不存在: {db_path}")
        return False

    updates = [
        ("cursorAuth/cachedSignUpType", "Auth_0"),
        ("cursorAuth/cachedEmail", email),
        ("cursorAuth/accessToken", access_token),
        ("cursorAuth/refreshToken", refresh_token),
    ]

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        for key, value in updates:
            cursor.execute("SELECT COUNT(*) FROM itemTable WHERE key = ?", (key,))
            if cursor.fetchone()[0] == 0:
                cursor.execute("INSERT INTO itemTable (key, value) VALUES (?, ?)", (key, value))
            else:
                cursor.execute("UPDATE itemTable SET value = ? WHERE key = ?", (value, key))
            print(f"[CursorAuth] 更新 {key.split('/')[-1]}")

        conn.commit()
        conn.close()
        print("[CursorAuth] Token 已写入 Cursor 数据库")
        return True

    except Exception as e:
        print(f"[CursorAuth] 写入失败: {e}")
        return False
