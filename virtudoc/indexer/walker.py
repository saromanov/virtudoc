from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .parsers import SUPPORTED_EXTENSIONS

MAX_FILE_SIZE = 5 * 1024 * 1024


@dataclass
class WalkedFile:
    path: Path
    rel_path: str


@dataclass
class WalkResult:
    files: list[WalkedFile] = field(default_factory=list)
    skipped_large: list[str] = field(default_factory=list)


def walk_source(source: Path) -> WalkResult:
    result = WalkResult()
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(source).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        rel_path = "/".join(rel_parts)
        try:
            if path.stat().st_size > MAX_FILE_SIZE:
                result.skipped_large.append(rel_path)
                continue
        except OSError:
            continue
        result.files.append(WalkedFile(path=path, rel_path=rel_path))
    return result
