from __future__ import annotations

import json
import re
import sqlite3
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .db import utc_now

MANIFEST_CANDIDATES = ("pyproject.toml", "package.json", "requirements.txt", "go.mod")

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)")
_GO_MOD_RE = re.compile(r"^\s*(?:require\s+)?([^\s]+)\s+(v?\d+(?:\.\d+)+[^\s]*)")


@dataclass
class VersionInfo:
    manifest_path: Path
    dep_version: str


def normalize_name(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def detect_manifest(project_path: Path) -> Path | None:
    for candidate in MANIFEST_CANDIDATES:
        path = project_path / candidate
        if path.is_file():
            return path
    return None


def version_core(text: str | None) -> tuple[int, ...] | None:
    if not text:
        return None
    match = _VERSION_RE.search(text)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def version_status(doc_version: str | None, dep_version: str | None) -> str:
    """One of: match, mismatch, unknown.

    Mismatch severity (same major vs different major) is derivable from the
    versions themselves; the status only reports whether they diverge.
    """
    doc_core = version_core(doc_version)
    dep_core = version_core(dep_version)
    if doc_core is None or dep_core is None:
        return "unknown"
    return "match" if doc_core == dep_core else "mismatch"


def extract_dep_version(manifest_path: Path, library_name: str) -> str | None:
    name = normalize_name(library_name)
    try:
        if manifest_path.name == "pyproject.toml":
            return _from_pyproject(manifest_path, name)
        if manifest_path.name == "package.json":
            return _from_package_json(manifest_path, name)
        if manifest_path.name == "requirements.txt":
            return _from_requirements(manifest_path, name)
        if manifest_path.name == "go.mod":
            return _from_go_mod(manifest_path, name)
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError):
        return None
    return None


def _match_version(spec: str | None) -> str | None:
    if not spec:
        return None
    match = _VERSION_RE.search(spec)
    return match.group(1) if match else None


def _from_pyproject(path: Path, name: str) -> str | None:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    project = data.get("project", {})
    requirements = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        requirements.extend(group)
    for req in requirements:
        if not isinstance(req, str):
            continue
        match = _REQ_NAME_RE.match(req)
        if match and normalize_name(match.group(1)) == name:
            return _match_version(req[match.end() :])
    return None


def _from_package_json(path: Path, name: str) -> str | None:
    data = json.loads(path.read_text(encoding="utf-8"))
    deps: dict[str, str] = {}
    deps.update(data.get("dependencies", {}))
    deps.update(data.get("devDependencies", {}))
    for dep, spec in deps.items():
        if normalize_name(dep) == name:
            return _match_version(spec)
    return None


def _from_requirements(path: Path, name: str) -> str | None:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _REQ_NAME_RE.match(line)
        if match and normalize_name(match.group(1)) == name:
            return _match_version(line[match.end() :])
    return None


def _from_go_mod(path: Path, name: str) -> str | None:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _GO_MOD_RE.match(line)
        if match and normalize_name(match.group(1).rsplit("/", 1)[-1]) == name:
            return match.group(2)
    return None


def link_library(
    conn: sqlite3.Connection,
    library_id: str,
    project_path: Path,
) -> VersionInfo:
    project_path = Path(project_path).expanduser().resolve()
    if not project_path.is_dir():
        raise ValueError(f"project directory not found: {project_path}")
    manifest = detect_manifest(project_path)
    if manifest is None:
        raise ValueError(
            f"no dependency manifest found in {project_path}; "
            f"looked for: {', '.join(MANIFEST_CANDIDATES)}"
        )
    lib_name = library_id.rsplit("/", 1)[-1]
    version = extract_dep_version(manifest, lib_name)
    if version is None:
        raise ValueError(
            f"dependency '{lib_name}' not found in {manifest.name}; "
            "the library name must match the package name in the manifest"
        )
    conn.execute("DELETE FROM project_links WHERE library_id = ?", (library_id,))
    conn.execute(
        """
        INSERT INTO project_links (library_id, manifest_path, dep_version, linked_at)
        VALUES (?, ?, ?, ?)
        """,
        (library_id, str(manifest), version, utc_now()),
    )
    conn.commit()
    return VersionInfo(manifest_path=manifest, dep_version=version)


def unlink_library(conn: sqlite3.Connection, library_id: str) -> bool:
    cur = conn.execute("DELETE FROM project_links WHERE library_id = ?", (library_id,))
    conn.commit()
    return cur.rowcount > 0


def get_link(conn: sqlite3.Connection, library_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM project_links WHERE library_id = ?", (library_id,)
    ).fetchone()


def mismatch_payload(conn: sqlite3.Connection, library_id: str) -> dict | None:
    """version_mismatch warning dict, or None when versions match/no link."""
    link = get_link(conn, library_id)
    if link is None:
        return None
    doc_version = conn.execute(
        "SELECT version FROM libraries WHERE id = ?", (library_id,)
    ).fetchone()
    doc_version = doc_version["version"] if doc_version else None
    status = version_status(doc_version, link["dep_version"])
    if status != "mismatch":
        return None
    return {
        "library_id": library_id,
        "documentation_version": doc_version,
        "project_version": link["dep_version"],
        "manifest": link["manifest_path"],
        "message": (
            f"version drift: indexed documentation is {doc_version or 'unknown'}, "
            f"but the project depends on {link['dep_version']} "
            f"({Path(link['manifest_path']).name})"
        ),
    }
