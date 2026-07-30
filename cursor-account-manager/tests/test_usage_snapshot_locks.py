"""用量快照跨进程锁测试。"""

from __future__ import annotations

import hashlib
import multiprocessing
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from cam import usage_snapshot_locks as locks


def _hold_account_lock(lock_dir: str, email: str, ready, release, queue) -> None:
    """子进程持有账号锁，直到父进程通知释放。"""
    with locks.usage_account_lock(email, Path(lock_dir), 1) as acquired:
        queue.put(acquired)
        if acquired:
            ready.set()
            release.wait(3)


def _try_account_lock(lock_dir: str, email: str, timeout_sec: float, queue) -> None:
    """子进程尝试获取账号锁并返回结果。"""
    with locks.usage_account_lock(email, Path(lock_dir), timeout_sec) as acquired:
        queue.put(acquired)


def _hold_task_lock(path: str, ready, release, queue) -> None:
    """子进程持有任务锁，直到父进程通知释放。"""
    with locks.usage_task_lock(Path(path), 1) as acquired:
        queue.put(acquired)
        if acquired:
            ready.set()
            release.wait(3)


def _try_task_lock(path: str, timeout_sec: float, queue) -> None:
    """子进程尝试获取任务锁并返回结果。"""
    with locks.usage_task_lock(Path(path), timeout_sec) as acquired:
        queue.put(acquired)


class UsageSnapshotLocksTests(unittest.TestCase):
    """验证账号锁与批次任务锁的互斥语义。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lock_dir = Path(self.temp_dir.name) / "account-locks"
        self.context = multiprocessing.get_context("spawn")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _start_holder(self, target, *args):
        ready = self.context.Event()
        release = self.context.Event()
        queue = self.context.Queue()
        process = self.context.Process(
            target=target,
            args=(*args, ready, release, queue),
        )
        process.start()
        self.assertTrue(ready.wait(3), "持锁子进程未在时限内就绪")
        self.assertTrue(queue.get(timeout=1))
        return process, release

    def _join_process(self, process, release=None) -> None:
        if release is not None:
            release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(2)
            self.fail("子进程未在时限内退出")
        self.assertEqual(process.exitcode, 0)

    def _try_in_process(self, target, *args) -> bool:
        queue = self.context.Queue()
        process = self.context.Process(target=target, args=(*args, queue))
        process.start()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(2)
            self.fail("子进程未在时限内退出")
        self.assertEqual(process.exitcode, 0)
        return queue.get(timeout=1)

    def test_lock_filename_is_normalized_email_sha256_without_plaintext(self):
        email = "  Alice@Example.COM  "
        expected = hashlib.sha256(b"alice@example.com").hexdigest() + ".lock"

        with locks.usage_account_lock(email, self.lock_dir, 0) as acquired:
            self.assertTrue(acquired)

        names = [path.name for path in self.lock_dir.iterdir()]
        self.assertEqual(names, [expected])
        self.assertNotIn("alice", names[0])
        self.assertNotIn("@", names[0])
        self.assertEqual((self.lock_dir / expected).read_bytes(), b"")

    def test_blank_email_and_negative_timeout_are_rejected(self):
        with self.assertRaises(ValueError):
            with locks.usage_account_lock(" \t ", self.lock_dir, 0):
                pass
        with self.assertRaises(ValueError):
            with locks.usage_account_lock("a@example.com", self.lock_dir, -0.1):
                pass

    def test_same_email_allows_only_one_independent_process(self):
        holder, release = self._start_holder(
            _hold_account_lock,
            str(self.lock_dir),
            "a@example.com",
        )
        try:
            acquired = self._try_in_process(
                _try_account_lock,
                str(self.lock_dir),
                " A@EXAMPLE.COM ",
                0.2,
            )
            self.assertFalse(acquired)
        finally:
            self._join_process(holder, release)

    def test_different_emails_can_be_locked_concurrently(self):
        holder, release = self._start_holder(
            _hold_account_lock,
            str(self.lock_dir),
            "a@example.com",
        )
        try:
            acquired = self._try_in_process(
                _try_account_lock,
                str(self.lock_dir),
                "b@example.com",
                0,
            )
            self.assertTrue(acquired)
        finally:
            self._join_process(holder, release)

    def test_zero_timeout_attempts_once_when_lock_is_busy(self):
        holder, release = self._start_holder(
            _hold_account_lock,
            str(self.lock_dir),
            "a@example.com",
        )
        try:
            started = time.monotonic()
            acquired = self._try_in_process(
                _try_account_lock,
                str(self.lock_dir),
                "a@example.com",
                0,
            )
            self.assertFalse(acquired)
            self.assertLess(time.monotonic() - started, 1)
        finally:
            self._join_process(holder, release)

    def test_task_lock_allows_only_one_independent_process(self):
        task_path = Path(self.temp_dir.name) / "nested" / "periodic.lock"
        holder, release = self._start_holder(
            _hold_task_lock,
            str(task_path),
        )
        try:
            acquired = self._try_in_process(
                _try_task_lock,
                str(task_path),
                0.2,
            )
            self.assertFalse(acquired)
        finally:
            self._join_process(holder, release)

    def test_task_lock_creates_parent_and_rejects_directory_path(self):
        task_path = Path(self.temp_dir.name) / "nested" / "pre-reset.lock"
        with locks.usage_task_lock(task_path) as acquired:
            self.assertTrue(acquired)
        self.assertTrue(task_path.exists())

        directory = Path(self.temp_dir.name) / "not-a-file"
        directory.mkdir()
        with self.assertRaises(ValueError):
            with locks.usage_task_lock(directory):
                pass

    def test_exception_in_locked_block_releases_account_lock(self):
        with self.assertRaisesRegex(RuntimeError, "业务异常"):
            with locks.usage_account_lock("a@example.com", self.lock_dir, 0) as acquired:
                self.assertTrue(acquired)
                raise RuntimeError("业务异常")

        with locks.usage_account_lock("a@example.com", self.lock_dir, 0) as acquired:
            self.assertTrue(acquired)

    def test_windows_backend_writes_and_seeks_before_lock_and_unlock(self):
        handle = MagicMock()
        handle.fileno.return_value = 42
        handle.tell.return_value = 0
        msvcrt = MagicMock()

        with (
            patch.object(locks, "fcntl", None),
            patch.object(locks, "msvcrt", msvcrt),
            patch("builtins.open", MagicMock(return_value=handle)),
        ):
            with locks.usage_task_lock(Path(self.temp_dir.name) / "windows.lock") as acquired:
                self.assertTrue(acquired)

        handle.write.assert_called_once_with(b"0")
        self.assertGreaterEqual(handle.seek.call_count, 2)
        self.assertEqual(msvcrt.locking.call_count, 2)


if __name__ == "__main__":
    unittest.main()
