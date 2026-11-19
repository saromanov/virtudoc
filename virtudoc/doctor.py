from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, data_dir
from .fetcher import PIN_FILE, read_pinned_branch
from .indexer.indexer import load_index_failures
from .versions import extract_dep_version, version_status

GIT_TIMEOUT = 10


@dataclass
class CheckResult:
    problems: int = 0
    lines: list[str] = field(default_factory=list)

    def ok(self, line: str) -> None:
        self.lines.append(f"  ok   {line}")

    def warn(self, line: str) -> None:
        self.problems += 1
        self.lines.append(f"  WARN {line}")

    def info(self, line: str) -> None:
        self.lines.append(f"       {line}")


def _warn_unstable_source(result: CheckResult, lib_id: str, source: Path) -> None:
    """Flag git checkouts whose content can flap between indexing runs."""
    if shutil.which("git") is None or not (source / ".git").exists():
        return

    def git(*args: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", "-C", str(source), *args],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except (OSError, subprocess.TimeoutExpired):
            # Doctor must never hang on a network filesystem or a prompt.
            return None
        return proc.stdout.strip() if proc.returncode == 0 else None

    branch = git("symbolic-ref", "--short", "-q", "HEAD")
    pinned = read_pinned_branch(source)
    if branch is None:
        if git("rev-parse", "--is-inside-work-tree") == "true":
            result.warn(f"{lib_id}: source is a git checkout in detached HEAD state")
        return
    if pinned and branch != pinned:
        result.warn(
            f"{lib_id}: fetch cache pinned to branch '{pinned}' but the checkout "
            f"is on '{branch}' — serve resets it locally before serving"
        )
    default = git("symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD")
    if default and not (pinned and branch == pinned):
        default_name = default.removeprefix("origin/")
        if branch != default_name:
            result.warn(
                f"{lib_id}: source on branch '{branch}' (default '{default_name}') — "
                f"the index follows this checkout; "
                f"'virtudoc fetch --branch {default_name}' gives a stable team baseline"
            )
    if git("status", "--porcelain", "--", ".", f":(exclude){PIN_FILE}"):
        result.warn(
            f"{lib_id}: source has uncommitted changes — indexed docs may include work in progress"
        )


def run_doctor(conn: sqlite3.Connection, cfg: Config) -> CheckResult:
    result = CheckResult()
    base = data_dir()
    result.info(f"data directory: {base}")

    if not cfg.db_path.is_file():
        result.warn(f"database file not found: {cfg.db_path}")
        return result
    result.ok(f"database: {cfg.db_path}")

    try:
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        result.ok(f"schema version: {version['value'] if version else 'unknown'}")
    except sqlite3.Error as exc:
        result.warn(f"cannot read meta table: {exc}")
        return result

    libraries = conn.execute(
        """
        SELECT l.*,
               (SELECT COUNT(*) FROM documents d WHERE d.library_id = l.id) AS doc_count,
               (SELECT COUNT(*) FROM chunks c WHERE c.library_id = l.id) AS chunk_count,
               (SELECT COUNT(*) FROM notes n WHERE n.library_id = l.id) AS note_count
        FROM libraries l ORDER BY l.id
        """
    ).fetchall()
    if not libraries:
        result.info("no libraries registered")
    for lib in libraries:
        source = Path(lib["source_path"])
        if source.is_dir():
            result.ok(
                f"{lib['id']}: source present, {lib['doc_count']} documents, "
                f"{lib['chunk_count']} chunks, {lib['note_count']} notes, "
                f"docs version: {lib['version'] or 'unknown'}"
            )
            failures = load_index_failures(conn, lib["id"])
            if failures:
                shown = ", ".join(sorted(failures)[:3])
                if len(failures) > 3:
                    shown += f", ... ({len(failures) - 3} more)"
                result.warn(f"{lib['id']}: files failed to index: {shown}")
            _warn_unstable_source(result, lib["id"], source)
        else:
            result.warn(f"{lib['id']}: source directory missing: {source}")

    links = conn.execute(
        """
        SELECT pl.*, l.version AS doc_version, l.name AS lib_name
        FROM project_links pl JOIN libraries l ON l.id = pl.library_id
        ORDER BY pl.library_id
        """
    ).fetchall()
    if not links:
        result.info("no project links")
    for link in links:
        manifest = Path(link["manifest_path"])
        if not manifest.is_file():
            result.warn(f"{link['library_id']}: manifest missing: {manifest}")
            continue
        current = extract_dep_version(manifest, link["lib_name"])
        status = version_status(link["doc_version"], current)
        if current is None:
            result.warn(f"{link['library_id']}: dependency not found in {manifest.name} anymore")
        elif status == "mismatch":
            result.warn(
                f"{link['library_id']}: version drift — docs {link['doc_version']}, "
                f"project {current} ({manifest.name}); stored: {link['dep_version']}"
            )
        else:
            result.ok(f"{link['library_id']}: versions match ({link['doc_version']} == {current})")

    try:
        orphan_chunks = conn.execute(
            """
            SELECT COUNT(*) FROM chunks c
            LEFT JOIN documents d ON d.id = c.document_id
            WHERE d.id IS NULL
            """
        ).fetchone()[0]
        # External-content FTS tables hide orphan index rows from plain SELECTs,
        # so verify the index structure instead of counting rows.
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')")
        conn.execute("INSERT INTO chunks_tri(chunks_tri) VALUES('integrity-check')")
        if orphan_chunks:
            result.warn(f"integrity: {orphan_chunks} orphan chunks")
        else:
            result.ok("index integrity: no orphans, FTS index consistent")
    except sqlite3.Error as exc:
        result.warn(f"integrity check failed: {exc}")

    if result.problems:
        result.info(f"{result.problems} problem(s) found")
    else:
        result.info("no problems found")
    return result
