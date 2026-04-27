# proxy_helper.py - 带认证代理转本地无认证代理
# Chrome 不支持 http://user:pass@host:port 格式的代理认证
# 本模块启动一个本地 HTTP 代理，自动注入 Proxy-Authorization 头转发到上游

from __future__ import annotations

import atexit
import base64
import re
import socket
import subprocess
import sys
import textwrap
import time
import os
import tempfile

_proxy_process = None
_tmp_script = None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _parse_proxy(proxy: str) -> tuple[str, int, str | None, str | None]:
    """解析代理 URL，返回 (host, port, user, password)。"""
    proxy = (proxy or "").strip()
    if not proxy:
        return "", 0, None, None

    m = re.match(r"https?://([^:]+):([^@]+)@([^:/\s]+):(\d+)", proxy)
    if m:
        user, password, host, port = m.groups()
        return host, int(port), user, password

    m = re.match(r"https?://([^:/\s]+):(\d+)", proxy)
    if m:
        host, port = m.groups()
        return host, int(port), None, None

    return "", 0, None, None


def start_proxy_tunnel(proxy: str) -> str:
    """若代理带认证，启动本地转发进程，返回 Chrome 可用的代理地址。"""
    global _proxy_process, _tmp_script

    host, port, user, password = _parse_proxy(proxy)
    if not host or not port:
        return ""

    if not user:
        return f"{host}:{port}"

    local_port = _free_port()

    script = textwrap.dedent(f"""\
        import asyncio, base64

        UPSTREAM_HOST = {host!r}
        UPSTREAM_PORT = {port!r}
        AUTH = "Basic " + base64.b64encode(b"{user}:{password}").decode()

        async def relay(reader, writer):
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
            except Exception:
                pass
            finally:
                writer.close()

        async def handle(local_r, local_w):
            try:
                first_line = await asyncio.wait_for(local_r.readline(), timeout=30)
                if not first_line:
                    local_w.close()
                    return

                up_r, up_w = await asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT)

                if first_line.upper().startswith(b"CONNECT"):
                    # HTTPS CONNECT: 发送带认证的 CONNECT 到上游
                    up_w.write(first_line)
                    # 读取并转发本地请求头，插入 Proxy-Authorization
                    auth_sent = False
                    while True:
                        line = await asyncio.wait_for(local_r.readline(), timeout=10)
                        if line in (b"\\r\\n", b"\\n", b""):
                            if not auth_sent:
                                up_w.write(b"Proxy-Authorization: " + AUTH.encode() + b"\\r\\n")
                            up_w.write(b"\\r\\n")
                            break
                        if line.lower().startswith(b"proxy-authorization:"):
                            continue
                        up_w.write(line)
                    await up_w.drain()

                    # 读取上游响应
                    resp_line = await asyncio.wait_for(up_r.readline(), timeout=30)
                    if b"200" in resp_line:
                        local_w.write(b"HTTP/1.1 200 Connection Established\\r\\n\\r\\n")
                        await local_w.drain()
                        # 读完上游响应头
                        while True:
                            hdr = await up_r.readline()
                            if hdr in (b"\\r\\n", b"\\n", b""):
                                break
                        await asyncio.gather(relay(local_r, up_w), relay(up_r, local_w))
                    else:
                        local_w.write(resp_line)
                        await local_w.drain()
                        local_w.close()
                else:
                    # HTTP: 转发请求并插入认证头
                    up_w.write(first_line)
                    while True:
                        line = await asyncio.wait_for(local_r.readline(), timeout=10)
                        if line in (b"\\r\\n", b"\\n", b""):
                            up_w.write(b"Proxy-Authorization: " + AUTH.encode() + b"\\r\\n")
                            up_w.write(b"\\r\\n")
                            break
                        if line.lower().startswith(b"proxy-authorization:"):
                            continue
                        up_w.write(line)
                    await up_w.drain()
                    await asyncio.gather(relay(local_r, up_w), relay(up_r, local_w))
            except Exception:
                pass
            finally:
                local_w.close()

        async def main():
            server = await asyncio.start_server(handle, "127.0.0.1", {local_port})
            async with server:
                await server.serve_forever()

        asyncio.run(main())
    """)

    fd, path = tempfile.mkstemp(suffix=".py", prefix="proxy_fwd_")
    os.write(fd, script.encode())
    os.close(fd)
    _tmp_script = path

    _proxy_process = subprocess.Popen(
        [sys.executable, path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    def _cleanup():
        if _proxy_process:
            try:
                _proxy_process.terminate()
            except Exception:
                pass
        if _tmp_script and os.path.exists(_tmp_script):
            try:
                os.unlink(_tmp_script)
            except Exception:
                pass

    atexit.register(_cleanup)
    time.sleep(1)

    if _proxy_process.poll() is not None:
        stderr = ""
        try:
            stderr = _proxy_process.stderr.read().decode(errors="replace")[:500]
        except Exception:
            pass
        raise RuntimeError(f"代理转发启动失败: {stderr}")

    print(f"[代理] 本地转发已启动: 127.0.0.1:{local_port} -> {host}:{port}")
    return f"127.0.0.1:{local_port}"
