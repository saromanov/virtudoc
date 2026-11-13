from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .parsers import SUPPORTED_EXTENSIONS

MAX_FILE_SIZE = 5 * 1024 * 1024

IGNORE_FILE = ".virtudocignore"


@dataclass
class WalkedFile:
    path: Path
    rel_path: str


@dataclass
class WalkResult:
    files: list[WalkedFile] = field(default_factory=list)
    skipped_large: list[str] = field(default_factory=list)


def _compile_pattern(pattern: str) -> re.Pattern[str] | None:
    dir_only = pattern.endswith("/")
    if dir_only:
        pattern = pattern[:-1]
    anchored = pattern.startswith("/")
    if anchored:
        pattern = pattern[1:]

    regex = ""
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            if pattern[i : i + 2] == "**":
                if pattern[i + 2 : i + 3] == "/":
                    regex += "(?:.*/)?"
                    i += 3
                    continue
                regex += ".*"
                i += 2
                continue
            regex += "[^/]*"
            i += 1
            continue
        if char == "?":
            regex += "[^/]"
            i += 1
            continue
        regex += re.escape(char)
        i += 1

    if not regex:
        return None
    if anchored or "/" in pattern:
        regex = "^" + regex
    else:
        regex = "(^|/)" + regex
    regex += "/" if dir_only else "($|/)"
    return re.compile(regex)


class IgnoreRules:
    def __init__(self, patterns: list[tuple[bool, re.Pattern[str]]]) -> None:
        self.patterns = patterns

    @classmethod
    def from_file(cls, path: Path) -> IgnoreRules:
        patterns: list[tuple[bool, re.Pattern[str]]] = []
        if not path.is_file():
            return cls(patterns)
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.rstrip()
            if not line or line.startswith("#"):
                continue
            negated = line.startswith("!")
            if negated:
                line = line[1:]
            compiled = _compile_pattern(line)
            if compiled is not None:
                patterns.append((negated, compiled))
        return cls(patterns)

    def ignored(self, rel_path: str, is_dir: bool) -> bool:
        candidates = [rel_path + "/"] if is_dir else [rel_path]
        parts = rel_path.split("/")
        candidates.extend("/".join(parts[:i]) + "/" for i in range(1, len(parts)))
        ignored = False
        for negated, regex in self.patterns:
            if any(regex.search(candidate) for candidate in candidates):
                ignored = not negated
        return ignored


def _rel(root: Path, source: Path, name: str) -> str:
    rel = Path(root, name).relative_to(source)
    return rel.as_posix()


def walk_source(source: Path) -> WalkResult:
    result = WalkResult()
    rules = IgnoreRules.from_file(source / IGNORE_FILE)
    for root, dirnames, filenames in os.walk(source):
        kept_dirs = []
        for dirname in sorted(dirnames):
            if dirname.startswith("."):
                continue
            rel = _rel(root, source, dirname)
            if rules.ignored(rel, is_dir=True):
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            if Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            rel_path = _rel(root, source, filename)
            if rules.ignored(rel_path, is_dir=False):
                continue
            path = Path(root, filename)
            try:
                if path.stat().st_size > MAX_FILE_SIZE:
                    result.skipped_large.append(rel_path)
                    continue
            except OSError:
                continue
            result.files.append(WalkedFile(path=path, rel_path=rel_path))
    result.files.sort(key=lambda walked: walked.rel_path)
    result.skipped_large.sort()
    return result
