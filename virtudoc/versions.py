from __future__ import annotations

import json
import re
import sqlite3
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .db import utc_now

MANIFEST_CANDIDATES = (
    "conan.lock",
    "pyproject.toml",
    "package.json",
    "requirements.txt",
    "go.mod",
    "conanfile.py",
    "conanfile.txt",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
)

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")
_MATCH_VERSION_RE = re.compile(r"v?\d+(?:\.\d+)*")
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)")
_GO_MOD_RE = re.compile(r"^\s*(?:require\s+)?([^\s]+)\s+(v?\d+(?:\.\d+)+[^\s]*)")
_CONAN_KEYS = ("requires", "build_requires", "python_requires")
_CONAN_PY_REQUIRES_RE = re.compile(r"\brequires\s*=\s*")
_CONAN_PY_SELF_REQUIRES_RE = re.compile(r"self\.requires\s*\(([^)]*)\)")
_CONAN_QUOTED_RE = re.compile(r"""["']([^"']+)["']""")


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
    spec: str | None = None


# Lockfiles resolve exact versions, so they win over declared ranges in a
# direct manifest. The order follows `detect_manifest`, with lockfiles first.
RESOLUTION_ORDER = (
    "conan.lock",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "pyproject.toml",
    "package.json",
    "requirements.txt",
    "go.mod",
    "conanfile.py",
    "conanfile.txt",
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


def resolve_project_versions(
    project_path: Path, names: Iterable[str]
) -> dict[str, ResolvedVersion]:
    """Lockfile-first resolution of several dependencies in one manifest pass.

    Every manifest is parsed at most once. The returned mapping is keyed by
    normalized dependency name and holds only the names the project resolves;
    the first entry of :data:`RESOLUTION_ORDER` still wins per name.
    """
    project_path = Path(project_path).expanduser()
    wanted = {normalize_name(name) for name in names}
    if not wanted or not project_path.is_dir():
        return {}
    manifests = {path.name: path for path in list_manifests(project_path)}
    resolved: dict[str, ResolvedVersion] = {}
    for name in RESOLUTION_ORDER:
        manifest = manifests.get(name)
        if manifest is None:
            continue
        dependencies = extract_dependency_specs(manifest)
        for dependency in wanted - resolved.keys():
            spec = dependencies.get(dependency)
            if not spec:
                continue
            version = _match_version(spec)
            if version:
                resolved[dependency] = ResolvedVersion(
                    manifest_path=manifest,
                    version=version,
                    dependency_name=dependency,
                    spec=spec,
                )
        if len(resolved) == len(wanted):
            break
    return resolved


def resolve_project_version(project_path: Path, library_name: str) -> ResolvedVersion | None:
    """Resolve the version the project uses, preferring lockfiles.

    A declared range such as ``>=2.0`` does not identify the installed
    release; a lockfile does. When several manifests mention the dependency,
    the first entry of :data:`RESOLUTION_ORDER` wins.
    """
    dependency = normalize_name(library_name)
    return resolve_project_versions(project_path, [dependency]).get(dependency)


def resolve_local_dependency_version(
    project_dir: Path, library_name: str
) -> tuple[str | None, str | None]:
    """Version the project pins for a dependency, with its source manifest name.

    Returns ``(version, manifest_name)`` or ``(None, None)`` when the project
    does not mention the dependency. Lockfiles win over declared ranges.
    """
    resolved = resolve_project_version(project_dir, library_name)
    if resolved is None:
        return None, None
    return resolved.version, resolved.manifest_path.name


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


_SPEC_OPERATORS = ("~=", "==", "!=", ">=", "<=", ">", "<")
_SPEC_VERSION_RE = re.compile(r"^v?\d+(?:\.\d+)*(?:[A-Za-z0-9._+!-]*)$")


def _compare_cores(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    left = _trim_trailing_zeros(left)
    right = _trim_trailing_zeros(right)
    if left == right:
        return 0
    return 1 if left > right else -1


def _parse_spec_clause(clause: str) -> tuple[str, tuple[int, ...]] | None:
    operator = "=="
    raw = clause
    for candidate in _SPEC_OPERATORS:
        if clause.startswith(candidate):
            operator = candidate
            raw = clause[len(candidate) :].strip()
            break
    if not _SPEC_VERSION_RE.match(raw):
        return None
    core = version_core(raw)
    if core is None:
        return None
    return operator, core


def _parse_spec(spec: str | None) -> list[tuple[str, tuple[int, ...]]] | None:
    if not spec:
        return None
    text = spec.split(";", 1)[0].strip()
    if text.startswith("["):
        closing = text.find("]")
        if closing == -1:
            return None
        text = text[closing + 1 :].strip()
    if not text:
        return None
    clauses = []
    for clause in text.split(","):
        parsed = _parse_spec_clause(clause.strip())
        if parsed is None:
            return None
        clauses.append(parsed)
    return clauses


def _compatible(doc_core: tuple[int, ...], operand: tuple[int, ...]) -> bool:
    if len(operand) < 2:
        return False
    if _compare_cores(doc_core, operand) < 0:
        return False
    prefix = operand[:-1]
    doc = _trim_trailing_zeros(doc_core)
    padded = doc + (0,) * max(0, len(prefix) - len(doc))
    return padded[: len(prefix)] == prefix


def _satisfies(doc_core: tuple[int, ...], operator: str, operand: tuple[int, ...]) -> bool:
    if operator == "~=":
        return _compatible(doc_core, operand)
    comparison = _compare_cores(doc_core, operand)
    outcomes = {
        "==": comparison == 0,
        "!=": comparison != 0,
        ">=": comparison >= 0,
        ">": comparison > 0,
        "<=": comparison <= 0,
        "<": comparison < 0,
    }
    return outcomes[operator]


def range_status(spec: str | None, doc_version: str | None) -> str:
    """One of: match, mismatch, unknown for a declared range against the docs.

    Exact pins (``==2.0`` or a bare ``2.0``) compare as :func:`version_status`
    does. A constraint that cannot be evaluated (``*``, ``latest``, a git URL,
    a space-separated conan range) is unknown and produces no warning.
    """
    doc_core = version_core(doc_version)
    clauses = _parse_spec(spec)
    if doc_core is None or clauses is None:
        return "unknown"
    if all(_satisfies(doc_core, operator, operand) for operator, operand in clauses):
        return "match"
    return "mismatch"


def dependency_status(doc_version: str | None, spec: str | None, dep_version: str | None) -> str:
    """One of: match, mismatch, unknown for docs against a declared dependency.

    A declared range is evaluated against the documentation version; an exact
    pin or a missing spec falls back to :func:`version_status`.
    """
    if spec:
        return range_status(spec, doc_version)
    return version_status(doc_version, dep_version)


def extract_dependencies(manifest_path: Path) -> dict[str, str | None]:
    """Declared dependencies of a manifest: normalized name -> version.

    Lockfiles contribute their whole resolved graph, while direct manifests
    contribute only declared dependencies. An unreadable or unknown manifest
    yields an empty mapping instead of raising.
    """
    return {
        name: _match_version(spec) for name, spec in extract_dependency_specs(manifest_path).items()
    }


def extract_dependency_specs(manifest_path: Path) -> dict[str, str | None]:
    """Raw declared constraint per dependency: ``>=2.0,<3`` or ``==1.4.0``.

    Unlike :func:`extract_dependencies`, the operator survives, so a range can
    be checked against a documentation version instead of being read as a pin.
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
        if manifest_path.name == "conan.lock":
            return _deps_from_conan_lock(manifest_path)
        if manifest_path.name == "conanfile.py":
            return _deps_from_conanfile_py(manifest_path)
        if manifest_path.name == "conanfile.txt":
            return _deps_from_conanfile_txt(manifest_path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, tomllib.TOMLDecodeError):
        return {}
    return {}


def extract_dep_version(manifest_path: Path, library_name: str) -> str | None:
    return extract_dependencies(manifest_path).get(normalize_name(library_name))


def extract_dep_spec(manifest_path: Path, library_name: str) -> str | None:
    """Raw declared constraint of one dependency, operator included."""
    return extract_dependency_specs(manifest_path).get(normalize_name(library_name))


def _match_version(spec: str | None) -> str | None:
    if not spec:
        return None
    match = _MATCH_VERSION_RE.search(spec)
    return match.group(0) if match else None


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
            deps[normalize_name(match.group(1))] = req[match.end() :].strip()
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
        deps[normalize_name(dep)] = str(spec_value) if spec_value else None
    return deps


def _deps_from_package_json(path: Path) -> dict[str, str | None]:
    data = json.loads(path.read_text(encoding="utf-8"))
    declared: dict[str, str] = {}
    declared.update(data.get("dependencies", {}))
    declared.update(data.get("devDependencies", {}))
    return {normalize_name(dep): spec for dep, spec in declared.items()}


def _deps_from_requirements(path: Path) -> dict[str, str | None]:
    deps: dict[str, str | None] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _REQ_NAME_RE.match(line)
        if match:
            deps[normalize_name(match.group(1))] = line[match.end() :].strip()
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
            deps[normalize_name(dep)] = spec
    return deps


def _parse_conan_ref(ref: str) -> tuple[str, str] | None:
    """Split ``name/version@user/channel#rrev%timestamp`` into name and version."""
    ref = ref.strip()
    name, separator, rest = ref.partition("/")
    if not separator or not name:
        return None
    spec = re.split(r"[@#%]", rest, maxsplit=1)[0].strip()
    if not spec:
        return None
    return normalize_name(name), spec


def _iter_conan_refs(data: object) -> Iterable[str]:
    if isinstance(data, dict):
        for key, value in data.items():
            if key in _CONAN_KEYS and isinstance(value, list):
                yield from (item for item in value if isinstance(item, str))
            else:
                yield from _iter_conan_refs(value)
    elif isinstance(data, list):
        for item in data:
            yield from _iter_conan_refs(item)


def _deps_from_conan_lock(path: Path) -> dict[str, str | None]:
    """Conan 2.x flat ref lists and conan 1.x ``graph_lock`` nodes."""
    data = json.loads(path.read_text(encoding="utf-8"))
    refs = list(_iter_conan_refs(data))
    nodes = data.get("graph_lock", {}).get("nodes", {}) if isinstance(data, dict) else {}
    if isinstance(nodes, dict):
        for node in nodes.values():
            if isinstance(node, dict) and "path" not in node and isinstance(node.get("ref"), str):
                refs.append(node["ref"])
    deps: dict[str, str | None] = {}
    for ref in refs:
        parsed = _parse_conan_ref(ref)
        if parsed is not None:
            deps[parsed[0]] = parsed[1]
    return deps


def _deps_from_conanfile_py(path: Path) -> dict[str, str | None]:
    """The ``requires`` attribute (conan 1.x) and ``self.requires()`` calls (2.x)."""
    body = path.read_text(encoding="utf-8", errors="replace")
    refs: list[str] = []
    for match in _CONAN_PY_REQUIRES_RE.finditer(body):
        rest = body[match.end() :].lstrip()
        if rest[:1] in ("(", "["):
            closing = rest.find(")" if rest[0] == "(" else "]")
            rest = rest[1:closing] if closing != -1 else rest[1:]
        else:
            rest = rest.splitlines()[0] if rest.splitlines() else ""
        refs.extend(_CONAN_QUOTED_RE.findall(rest))
    for match in _CONAN_PY_SELF_REQUIRES_RE.finditer(body):
        refs.extend(_CONAN_QUOTED_RE.findall(match.group(1)))
    deps: dict[str, str | None] = {}
    for ref in refs:
        parsed = _parse_conan_ref(ref)
        if parsed is not None:
            deps[parsed[0]] = parsed[1]
    return deps


def _deps_from_conanfile_txt(path: Path) -> dict[str, str | None]:
    deps: dict[str, str | None] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "[")):
            continue
        parsed = _parse_conan_ref(line)
        if parsed is not None:
            deps[parsed[0]] = parsed[1]
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
    spec: str | None = None
    if link is not None:
        dep_version = link["dep_version"]
        manifest_path = Path(link["manifest_path"])
        spec = extract_dep_spec(manifest_path, library["name"])
    else:
        resolved = resolve_project_version(
            Path(project_path) if project_path else Path.cwd(),
            library["name"],
        )
        if resolved is None:
            return None
        dep_version = resolved.version
        manifest_path = resolved.manifest_path
        spec = resolved.spec
    if dependency_status(doc_version, spec, dep_version) != "mismatch":
        return None

    choices = library_version_choices(conn, library_id)
    suggested = next(
        (
            choice
            for choice in choices
            if dependency_status(choice["version"], spec, dep_version) == "match"
        ),
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
