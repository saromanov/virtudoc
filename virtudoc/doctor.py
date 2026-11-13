from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, data_dir
from .versions import extract_dep_version, version_status


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
        orphan_fts = conn.execute(
            "SELECT COUNT(*) FROM chunks_fts WHERE rowid NOT IN (SELECT id FROM chunks)"
        ).fetchone()[0]
        if orphan_chunks or orphan_fts:
            result.warn(f"integrity: {orphan_chunks} orphan chunks, {orphan_fts} orphan fts rows")
        else:
            result.ok("index integrity: no orphans")
    except sqlite3.Error as exc:
        result.warn(f"integrity check failed: {exc}")

    if result.problems:
        result.info(f"{result.problems} problem(s) found")
    else:
        result.info("no problems found")
    return result
