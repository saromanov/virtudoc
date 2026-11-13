from __future__ import annotations

import logging
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("virtudoc")
    if root.handlers:
        return
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.FileHandler(log_dir / "virtudoc.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(handler)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"virtudoc.{name}")
