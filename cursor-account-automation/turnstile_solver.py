# turnstile_solver.py - 外部 Turnstile 求解器
#
# 当浏览器自身的 Turnstile 无感验证失败时（返回 fail token），
# 通过 CapSolver API 获取一个由真实浏览器生成的有效 token，
# 注入页面后提交。
#
# CapSolver 费用：~$0.001/次（约 ¥7/1000次）
# 注册送免费额度：https://dashboard.capsolver.com

from __future__ import annotations

import os
import time
import requests


CAPSOLVER_API = "https://api.capsolver.com"


def _get_api_key() -> str:
    key = os.environ.get("CAPSOLVER_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "未配置 CAPSOLVER_API_KEY。\n"
            "请在 .env 文件中添加：CAPSOLVER_API_KEY=CAP-xxx\n"
            "注册地址：https://dashboard.capsolver.com"
        )
    return key


def get_turnstile_params(tab) -> dict:
    """从页面中提取 Turnstile 参数。

    按 CapSolver 官方方式提取：
    - websiteKey
    - metadata.action
    - metadata.cdata
    """
    try:
        return tab.run_js("""
            const result = {
                sitekey: '',
                action: '',
                cdata: '',
                source: '',
            };

            const setResult = (sitekey, action, cdata, source) => {
                if (!sitekey || result.sitekey) return;
                result.sitekey = String(sitekey || '').trim();
                result.action = String(action || '').trim();
                result.cdata = String(cdata || '').trim();
                result.source = source;
            };

            // 0. 读取非侵入式 MutationObserver 捕获的 sitekey
            try {
                const captured = window.__capturedTurnstile;
                if (captured && captured.sitekey && captured.sitekey.length > 10) {
                    setResult(captured.sitekey, '', '', captured.source || 'observer');
                }
            } catch (e) {}

            // 1. 标准 data-* 属性
            const els = document.querySelectorAll('[data-sitekey], .cf-turnstile, #cf-turnstile, [id^="cf-chl-widget"]');
            for (const el of els) {
                const sitekey = el.getAttribute('data-sitekey');
                const action = el.getAttribute('data-action');
                const cdata = el.getAttribute('data-cdata');
                if (sitekey && sitekey.length > 10) {
                    setResult(sitekey, action, cdata, 'data-attributes');
                    break;
                }
            }

            // 2. iframe src 查询参数
            if (!result.sitekey) {
                const iframes = document.querySelectorAll('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]');
                for (const iframe of iframes) {
                    try {
                        const u = new URL(iframe.src, location.href);
                        const sitekey = u.searchParams.get('k') || u.searchParams.get('sitekey');
                        const action = u.searchParams.get('action') || '';
                        const cdata = u.searchParams.get('cData') || u.searchParams.get('data') || '';
                        if (sitekey && sitekey.length > 10) {
                            setResult(sitekey, action, cdata, 'iframe-src');
                            break;
                        }
                    } catch (e) {}
                }
            }

            // 3. Cloudflare 内部配置对象 ___turnstile_cfg
            if (!result.sitekey) {
                try {
                    const cfg = window.___turnstile_cfg;
                    const walk = (obj) => {
                        if (!obj || typeof obj !== 'object') return null;
                        if (obj.sitekey && String(obj.sitekey).length > 10) {
                            return {
                                sitekey: obj.sitekey,
                                action: obj.action || '',
                                cdata: obj.cData || obj.cdata || '',
                            };
                        }
                        for (const value of Object.values(obj)) {
                            const hit = walk(value);
                            if (hit) return hit;
                        }
                        return null;
                    };
                    const hit = walk(cfg);
                    if (hit) {
                        setResult(hit.sitekey, hit.action, hit.cdata, '___turnstile_cfg');
                    }
                } catch (e) {}
            }

            // 4. 脚本文本中的 render 配置
            if (!result.sitekey) {
                const scripts = Array.from(document.scripts || []);
                const patterns = [
                    /captcha_public_key_invisible["'\\s:=]+(0x4[A-Za-z0-9_-]+)/i,
                    /captchaPublicKeyInvisible["'\\s:=]+(0x4[A-Za-z0-9_-]+)/i,
                    /captcha_public_key["'\\s:=]+(0x4[A-Za-z0-9_-]+)/i,
                    /captchaPublicKey["'\\s:=]+(0x4[A-Za-z0-9_-]+)/i,
                    /sitekey["'\\s:]+(0x4[A-Za-z0-9_-]+)/i,
                    /["']sitekey["']\\s*,\\s*["'](0x4[A-Za-z0-9_-]+)["']/i,
                ];
                for (const script of scripts) {
                    const text = script.textContent || '';
                    for (const pattern of patterns) {
                        const m = text.match(pattern);
                        if (m && m[1]) {
                            const isInvisible =
                                /captcha_public_key_invisible|captchaPublicKeyInvisible/i.test(pattern.toString());
                            const actionMatch = text.match(/action["'\\s:]+([A-Za-z0-9_-]+)/i);
                            const cdataMatch = text.match(/cData["'\\s:]+([A-Za-z0-9_-]+)/i);
                            setResult(
                                m[1],
                                actionMatch ? actionMatch[1] : '',
                                cdataMatch ? cdataMatch[1] : '',
                                isInvisible ? 'script-text-invisible' : 'script-text'
                            );
                            break;
                        }
                    }
                    if (result.sitekey) break;
                }
            }

            return result;
        """) or ""
    except Exception:
        return {}


def solve_turnstile(
    website_url: str,
    website_key: str,
    action: str = "",
    cdata: str = "",
    timeout: int = 60,
) -> str:
    """调用 CapSolver API 求解 Turnstile，返回有效 token。

    Args:
        website_url: Turnstile 所在页面 URL
        website_key: Turnstile sitekey
        timeout: 最大等待秒数

    Returns:
        有效的 Turnstile token 字符串

    Raises:
        RuntimeError: API 调用失败或超时
    """
    api_key = _get_api_key()

    task = {
        "type": "AntiTurnstileTaskProxyLess",
        "websiteURL": website_url,
        "websiteKey": website_key,
    }
    metadata = {}
    if action:
        metadata["action"] = action
    if cdata:
        metadata["cdata"] = cdata
    if metadata:
        task["metadata"] = metadata

    # 创建任务
    resp = requests.post(f"{CAPSOLVER_API}/createTask", json={
        "clientKey": api_key,
        "task": task,
    }, timeout=15)

    data = resp.json()
    if data.get("errorId", 0) != 0:
        raise RuntimeError(f"CapSolver createTask 失败: {data.get('errorDescription', data)}")

    task_id = data.get("taskId")
    if not task_id:
        raise RuntimeError(f"CapSolver 未返回 taskId: {data}")

    print(f"[CapSolver] 任务已创建: {task_id[:20]}...")

    # 轮询结果
    deadline = time.time() + timeout
    poll_interval = 1.5

    while time.time() < deadline:
        time.sleep(poll_interval)

        resp = requests.post(f"{CAPSOLVER_API}/getTaskResult", json={
            "clientKey": api_key,
            "taskId": task_id,
        }, timeout=15)

        data = resp.json()
        status = data.get("status", "")

        if status == "ready":
            token = data.get("solution", {}).get("token", "")
            if token:
                print(f"[CapSolver] 求解成功, token len={len(token)}")
                return token
            raise RuntimeError(f"CapSolver 返回空 token: {data}")

        if status == "failed":
            raise RuntimeError(f"CapSolver 求解失败: {data.get('errorDescription', data)}")

        # processing，继续等待

    raise RuntimeError(f"CapSolver {timeout}s 超时")


def inject_turnstile_token(tab, token: str) -> bool:
    """将外部获取的 Turnstile token 注入页面。

    多层注入确保覆盖所有 Clerk 读取路径：
    1. hidden input（表单提交时读取）
    2. turnstile.getResponse() 覆盖（Clerk JS API 读取）
    3. data-callback 回调触发
    """
    try:
        result = tab.run_js(f"""
            const token = {repr(token)};
            let injected = false;

            // 1. 注入到所有 hidden inputs
            const sels = [
                'input[name="cf-turnstile-response"]',
                '[name="cf-chl-turnstile-response"]',
            ];
            for (const sel of sels) {{
                const el = document.querySelector(sel);
                if (el) {{
                    el.value = token;
                    injected = true;
                }}
            }}

            // 2. 注入到 widget 内部 hidden inputs
            const widgets = document.querySelectorAll(
                '[id^="cf-chl-widget"], .cf-turnstile, #cf-turnstile'
            );
            for (const w of widgets) {{
                const inputs = w.querySelectorAll('input[type="hidden"]');
                for (const inp of inputs) {{
                    inp.value = token;
                    injected = true;
                }}
            }}

            // 3. 覆盖 turnstile.getResponse() — Clerk 用 JS API 读取 token
            try {{
                if (window.turnstile) {{
                    window.turnstile.getResponse = function() {{ return token; }};
                    injected = true;
                }}
            }} catch(e) {{}}

            // 4. 触发 data-callback
            try {{
                const cbs = document.querySelectorAll('[data-callback]');
                for (const el of cbs) {{
                    const cbName = el.getAttribute('data-callback');
                    if (cbName && window[cbName]) {{
                        window[cbName](token);
                        injected = true;
                    }}
                }}
            }} catch(e) {{}}

            // 5. 如果没找到任何注入点，创建 hidden input
            if (!injected) {{
                const inp = document.createElement('input');
                inp.type = 'hidden';
                inp.name = 'cf-turnstile-response';
                inp.value = token;
                const form = document.querySelector('form');
                if (form) {{
                    form.appendChild(inp);
                    injected = true;
                }}
            }}

            return injected;
        """)
        print(f"[CapSolver] token 已注入页面 (injected={result})")
        return True
    except Exception as e:
        print(f"[CapSolver] token 注入失败: {e}")
        return False


def solve_and_inject(tab, timeout: int = 60) -> bool:
    """完整流程：提取 sitekey → 求解 → 注入。

    Returns: True = 成功注入, False = 失败
    """
    url = tab.url or ""
    print(f"[CapSolver] 开始求解 Turnstile: {url}")

    try:
        diag = tab.run_js("""
            const ct = window.__capturedTurnstile || {};
            const hasTurnstile = !!window.turnstile;
            const iframeCount = document.querySelectorAll('iframe').length;
            const challengeIframes = Array.from(document.querySelectorAll('iframe'))
                .filter(f => f.src && f.src.includes('challenge'))
                .map(f => f.src.substring(0, 120));
            return {
                capturedKey: ct.sitekey || '',
                capturedSource: ct.source || '',
                iframeSrc: ct.iframeSrc || '',
                hasTurnstile, iframeCount, challengeIframes,
            };
        """) or {}
        captured_key = diag.get("capturedKey", "")
        print(
            f"[CapSolver] observer: key={'yes(' + captured_key[:20] + '...)' if captured_key else 'no'}"
            f", source={diag.get('capturedSource') or 'none'}"
            f", turnstile={'yes' if diag.get('hasTurnstile') else 'no'}"
            f", iframes={diag.get('iframeCount', 0)}"
        )
        for src in (diag.get("challengeIframes") or [])[:3]:
            print(f"[CapSolver] challenge iframe: {src}")
    except Exception:
        pass

    params = get_turnstile_params(tab) or {}
    sitekey = (params.get("sitekey") or "").strip()
    action = (params.get("action") or "").strip()
    cdata = (params.get("cdata") or "").strip()
    source = (params.get("source") or "").strip()

    print(
        "[CapSolver] 参数:"
        f" source={source or 'unknown'}"
        f", sitekey={sitekey[:24] + '...' if sitekey else '(missing)'}"
        f", action={action or '(empty)'}"
        f", cdata_len={len(cdata)}"
    )

    if not sitekey:
        print("[CapSolver] 未能从当前页面提取到真实 sitekey，终止求解")
        return False

    try:
        token = solve_turnstile(
            url,
            sitekey,
            action=action,
            cdata=cdata,
            timeout=timeout,
        )
        return inject_turnstile_token(tab, token)
    except RuntimeError as e:
        print(f"[CapSolver] 失败: {e}")
        return False
