"""统一日志：带时间戳和账号前缀，写 stdout。"""

from __future__ import annotations

import logging
import sys


_initialized = False


def setup(level: str = "INFO") -> None:
    global _initialized
    if _initialized:
        return

    root = logging.getLogger("cam")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(levelname)-5s %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(handler)
    _initialized = True


def get(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(f"cam.{name}")
