from __future__ import annotations

import json
import re
import sqlite3
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .db import utc_now

MANIFEST_CANDIDATES = (
    "pyproject.toml",
    "package.json",
    "requirements.txt",
    "go.mod",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
)

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)")
_GO_MOD_RE = re.compile(r"^\s*(?:require\s+)?([^\s]+)\s+(v?\d+(?:\.\d+)+[^\s]*)")


@dataclass
class VersionInfo:
    manifest_path: Path
    dep_version: str


@dataclass
class ResolvedVersion:
    """The dependency version a consuming project actually locks or declares."""

    manifest_path: Path
    version: str
    dependency_name: str


# Lockfiles resolve exact versions, so they win over declared ranges in a
# direct manifest. The order follows `detect_manifest`, with lockfiles first.
RESOLUTION_ORDER = (
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "pyproject.toml",
    "package.json",
    "requirements.txt",
    "go.mod",
)


_PACKAGE_SEPARATORS = re.compile(r"[-_.]+")


def normalize_name(name: str) -> str:
    """PEP 503 normalization: dots, underscores and runs of hyphens are equal."""
    return _PACKAGE_SEPARATORS.sub("-", name.strip().lower())


def detect_manifest(project_path: Path) -> Path | None:
    for candidate in MANIFEST_CANDIDATES:
        path = project_path / candidate
        if path.is_file():
            return path
    return None


def list_manifests(project_path: Path) -> list[Path]:
    """Every supported manifest present in the directory, in priority order."""
    return [
        path for path in (project_path / name for name in MANIFEST_CANDIDATES) if path.is_file()
    ]


def resolve_project_version(project_path: Path, library_name: str) -> ResolvedVersion | None:
    """Resolve the version the project uses, preferring lockfiles.

    A declared range such as ``>=2.0`` does not identify the installed
    release; a lockfile does. When several manifests mention the dependency,
    the first entry of :data:`RESOLUTION_ORDER` wins.
    """
    project_path = Path(project_path).expanduser()
    if not project_path.is_dir():
        return None
    manifests = {path.name: path for path in list_manifests(project_path)}
    dependency = normalize_name(library_name)
    for name in RESOLUTION_ORDER:
        manifest = manifests.get(name)
        if manifest is None:
            continue
        version = extract_dep_version(manifest, dependency)
        if version:
            return ResolvedVersion(
                manifest_path=manifest,
                version=version,
                dependency_name=dependency,
            )
    return None


def library_version_choices(conn: sqlite3.Connection, name_or_id: str) -> list[dict]:
    """Indexed releases and branches of one library name, with their ids."""
    name = name_or_id.rsplit("/", 1)[-1].split("@", 1)[0]
    rows = conn.execute(
        """
        SELECT id, name, version, channel
        FROM libraries WHERE name = ?
        ORDER BY CASE channel WHEN 'release' THEN 0 ELSE 1 END, id
        """,
        (name,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "version": row["version"],
            "channel": row["channel"],
        }
        for row in rows
    ]


def version_core(text: str | None) -> tuple[int, ...] | None:
    if not text:
        return None
    match = _VERSION_RE.search(text)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _trim_trailing_zeros(core: tuple[int, ...]) -> tuple[int, ...]:
    """Drop trailing zero components: (3, 4, 0) and (3, 4) are the same version."""
    end = len(core)
    while end > 1 and core[end - 1] == 0:
        end -= 1
    return core[:end]


def version_status(doc_version: str | None, dep_version: str | None) -> str:
    """One of: match, mismatch, unknown.

    Numerically equivalent versions match regardless of trailing zero
    components: "3.4" and "3.4.0" are the same release.
    Mismatch severity (same major vs different major) is derivable from the
    versions themselves; the status only reports whether they diverge.
    """
    doc_core = version_core(doc_version)
    dep_core = version_core(dep_version)
    if doc_core is None or dep_core is None:
        return "unknown"
    return (
        "match" if _trim_trailing_zeros(doc_core) == _trim_trailing_zeros(dep_core) else "mismatch"
    )


def extract_dependencies(manifest_path: Path) -> dict[str, str | None]:
    """Declared dependencies of a manifest: normalized name -> version.

    Lockfiles contribute their whole resolved graph, while direct manifests
    contribute only declared dependencies. An unreadable or unknown manifest
    yields an empty mapping instead of raising.
    """
    try:
        if manifest_path.name == "pyproject.toml":
            return _deps_from_pyproject(manifest_path)
        if manifest_path.name == "package.json":
            return _deps_from_package_json(manifest_path)
        if manifest_path.name == "requirements.txt":
            return _deps_from_requirements(manifest_path)
        if manifest_path.name == "go.mod":
            return _deps_from_go_mod(manifest_path)
        if manifest_path.name in ("uv.lock", "poetry.lock"):
            return _deps_from_package_lock(manifest_path)
        if manifest_path.name == "Pipfile.lock":
            return _deps_from_pipfile_lock(manifest_path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, tomllib.TOMLDecodeError):
        return {}
    return {}


def extract_dep_version(manifest_path: Path, library_name: str) -> str | None:
    return extract_dependencies(manifest_path).get(normalize_name(library_name))


def _match_version(spec: str | None) -> str | None:
    if not spec:
        return None
    match = _VERSION_RE.search(spec)
    return match.group(1) if match else None


def _deps_from_pyproject(path: Path) -> dict[str, str | None]:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    project = data.get("project", {})
    requirements = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        requirements.extend(group)
    deps: dict[str, str | None] = {}
    for req in requirements:
        if not isinstance(req, str):
            continue
        match = _REQ_NAME_RE.match(req)
        if match:
            deps[normalize_name(match.group(1))] = _match_version(req[match.end() :])
    for dep, version in _deps_from_poetry_section(data).items():
        deps.setdefault(dep, version)
    return deps


def _deps_from_poetry_section(data: dict) -> dict[str, str | None]:
    poetry = data.get("tool", {}).get("poetry", {})
    dependencies = poetry.get("dependencies", {})
    if not isinstance(dependencies, dict):
        return {}
    deps: dict[str, str | None] = {}
    for dep, spec in dependencies.items():
        spec_value = spec.get("version") if isinstance(spec, dict) else spec
        deps[normalize_name(dep)] = _match_version(str(spec_value)) if spec_value else None
    return deps


def _deps_from_package_json(path: Path) -> dict[str, str | None]:
    data = json.loads(path.read_text(encoding="utf-8"))
    declared: dict[str, str] = {}
    declared.update(data.get("dependencies", {}))
    declared.update(data.get("devDependencies", {}))
    return {normalize_name(dep): _match_version(spec) for dep, spec in declared.items()}


def _deps_from_requirements(path: Path) -> dict[str, str | None]:
    deps: dict[str, str | None] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _REQ_NAME_RE.match(line)
        if match:
            deps[normalize_name(match.group(1))] = _match_version(line[match.end() :])
    return deps


def _deps_from_go_mod(path: Path) -> dict[str, str | None]:
    deps: dict[str, str | None] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith(("module ", "go ")):
            continue
        match = _GO_MOD_RE.match(line)
        if match:
            name = normalize_name(match.group(1).rsplit("/", 1)[-1])
            deps[name] = match.group(2)
    return deps


def _deps_from_package_lock(path: Path) -> dict[str, str | None]:
    """uv.lock and poetry.lock share the [[package]] table shape."""
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    deps: dict[str, str | None] = {}
    for package in data.get("package", []):
        if not isinstance(package, dict):
            continue
        name = normalize_name(str(package.get("name", "")))
        if name:
            version = package.get("version")
            deps[name] = str(version) if version else None
    return deps


def _deps_from_pipfile_lock(path: Path) -> dict[str, str | None]:
    data = json.loads(path.read_text(encoding="utf-8"))
    deps: dict[str, str | None] = {}
    for section in ("default", "develop"):
        for dep, info in data.get(section, {}).items():
            spec = info.get("version") if isinstance(info, dict) else None
            deps[normalize_name(dep)] = _match_version(spec)
    return deps


def link_library(
    conn: sqlite3.Connection,
    library_id: str,
    project_path: Path,
    manifest: Path | None = None,
) -> VersionInfo:
    project_path = Path(project_path).expanduser().resolve()
    if not project_path.is_dir():
        raise ValueError(f"project directory not found: {project_path}")
    row = conn.execute("SELECT name FROM libraries WHERE id = ?", (library_id,)).fetchone()
    lib_name = row["name"] if row is not None else library_id.rsplit("/", 1)[-1].split("@", 1)[0]

    explicit = Path(manifest).expanduser().resolve() if manifest else None
    version: str | None = None
    if explicit is None:
        resolved = resolve_project_version(project_path, lib_name)
        if resolved is not None:
            manifest = resolved.manifest_path
            version = resolved.version
    else:
        manifest = explicit
    if manifest is None:
        manifest = detect_manifest(project_path)
    if manifest is None:
        raise ValueError(
            f"no dependency manifest found in {project_path}; "
            f"looked for: {', '.join(MANIFEST_CANDIDATES)}"
        )
    if not manifest.is_file():
        raise ValueError(f"dependency manifest not found: {manifest}")
    if version is None:
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


def mismatch_payload(
    conn: sqlite3.Connection,
    library_id: str,
    *,
    project_path: Path | str | None = None,
) -> dict | None:
    """version_mismatch warning dict, or None when versions match/no link.

    An explicit project link takes priority. Without one, the version is
    resolved from the consuming project's manifests (the server's working
    directory by default), so an agent sees the drift before trusting the
    snippets.
    """
    library = conn.execute(
        "SELECT name, version FROM libraries WHERE id = ?", (library_id,)
    ).fetchone()
    if library is None:
        return None
    doc_version = library["version"]
    link = get_link(conn, library_id)
    if link is not None:
        dep_version = link["dep_version"]
        manifest_path = Path(link["manifest_path"])
    else:
        resolved = resolve_project_version(
            Path(project_path) if project_path else Path.cwd(),
            library["name"],
        )
        if resolved is None:
            return None
        dep_version = resolved.version
        manifest_path = resolved.manifest_path
    if version_status(doc_version, dep_version) != "mismatch":
        return None

    choices = library_version_choices(conn, library_id)
    suggested = next(
        (choice for choice in choices if version_status(choice["version"], dep_version) == "match"),
        None,
    )
    available = ", ".join(
        f"{choice['version'] or 'unversioned'} ({choice['id']})" for choice in choices
    )
    message = (
        f"version drift: indexed documentation is {doc_version or 'unknown'}, "
        f"but the project depends on {dep_version} "
        f"({manifest_path.name})"
    )
    if suggested is not None and suggested["id"] != library_id:
        message += (
            f"; indexed version {suggested['version']} matches the project — "
            f"use library id {suggested['id']}"
        )
    elif suggested is None:
        message += (
            f"; project version {dep_version} is not indexed "
            f"(available: {available or 'none'}) — "
            "ask the user before using a different version"
        )
    return {
        "library_id": library_id,
        "documentation_version": doc_version,
        "project_version": dep_version,
        "manifest": str(manifest_path),
        "available_versions": choices,
        "suggested_library_id": suggested["id"] if suggested else None,
        "version_not_indexed": suggested is None,
        "message": message,
    }
