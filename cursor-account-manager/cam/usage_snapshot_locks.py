"""Cursor 用量快照的跨进程锁。"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from math import isfinite
from pathlib import Path
import time
from typing import Iterator

try:
    import fcntl  # POSIX 专用。
except ImportError:  # pragma: no cover - Windows 平台。
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt  # Windows 专用。
except ImportError:  # pragma: no cover - POSIX 平台。
    msvcrt = None  # type: ignore[assignment]


_POLL_INTERVAL_SEC = 0.01


def _normalize_email(email: str) -> str:
    """规范化锁键所用邮箱，拒绝空白或非字符串输入。"""
    if not isinstance(email, str):
        raise ValueError("email 必须是非空字符串")
    normalized = email.strip().lower()
    if not normalized:
        raise ValueError("email 去除空白后不能为空")
    return normalized


def _validate_timeout(timeout_sec: float) -> float:
    """校验锁等待时长。"""
    if isinstance(timeout_sec, bool):
        raise ValueError("timeout_sec 必须是非负有限数字")
    try:
        timeout = float(timeout_sec)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout_sec 必须是非负有限数字") from exc
    if not isfinite(timeout) or timeout < 0:
        raise ValueError("timeout_sec 必须是非负有限数字")
    return timeout


def _validate_task_path(path: str | Path) -> Path:
    """校验任务锁路径是可创建的普通文件路径。"""
    try:
        lock_path = Path(path)
    except TypeError as exc:
        raise ValueError("path 必须是普通文件路径") from exc
    if not str(lock_path) or lock_path == Path(".") or lock_path.is_dir():
        raise ValueError("path 必须是普通文件路径")
    return lock_path


def _validate_lock_dir(lock_dir: str | Path) -> Path:
    """校验账号锁目录路径。"""
    try:
        directory = Path(lock_dir)
    except TypeError as exc:
        raise ValueError("lock_dir 必须是目录路径") from exc
    if directory.exists() and not directory.is_dir():
        raise ValueError("lock_dir 必须是目录路径")
    return directory


def _try_acquire(handle) -> bool:
    """以非阻塞方式尝试取得文件互斥锁。"""
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - 仅在不支持的平台发生。
            raise RuntimeError("当前平台不支持跨进程文件锁")
    except OSError:
        return False
    return True


def _release(handle) -> None:
    """释放已取得的文件互斥锁。"""
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextmanager
def _file_lock(lock_path: Path, timeout_sec: float) -> Iterator[bool]:
    """获取文件锁；超时时返回 False，退出时无条件释放资源。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    locked = False
    handle = open(lock_path, "a+b")
    try:
        if msvcrt is not None and fcntl is None:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()

        deadline = time.monotonic() + timeout_sec
        while True:
            if _try_acquire(handle):
                locked = True
                break
            if timeout_sec == 0 or time.monotonic() >= deadline:
                break
            time.sleep(min(_POLL_INTERVAL_SEC, max(0, deadline - time.monotonic())))
        yield locked
    finally:
        if locked:
            _release(handle)
        handle.close()


@contextmanager
def usage_account_lock(
    email: str,
    lock_dir: str | Path,
    timeout_sec: float,
) -> Iterator[bool]:
    """按规范化邮箱获取跨进程账号锁，不在锁文件写入邮箱。"""
    normalized_email = _normalize_email(email)
    timeout = _validate_timeout(timeout_sec)
    lock_directory = _validate_lock_dir(lock_dir)
    filename = hashlib.sha256(normalized_email.encode("utf-8")).hexdigest() + ".lock"
    with _file_lock(lock_directory / filename, timeout) as locked:
        yield locked


@contextmanager
def usage_task_lock(
    path: str | Path,
    timeout_sec: float = 0,
) -> Iterator[bool]:
    """获取 periodic 或 pre-reset 批次任务的跨进程锁。"""
    lock_path = _validate_task_path(path)
    timeout = _validate_timeout(timeout_sec)
    with _file_lock(lock_path, timeout) as locked:
        yield locked
