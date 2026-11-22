from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .indexer.indexer import list_libraries
from .versions import (
    extract_dependencies,
    link_library,
    list_manifests,
    normalize_name,
    resolve_project_version,
    version_status,
)

UNMATCHED_HINT_LIMIT = 10


@dataclass
class DependencyEntry:
    name: str
    version: str | None
    manifest: Path


@dataclass
class LibraryMatch:
    library_id: str
    library_name: str
    doc_version: str | None
    dependency: DependencyEntry
    status: str  # match | mismatch | unknown


@dataclass
class ProjectScan:
    project_path: Path
    manifests: list[Path] = field(default_factory=list)
    dependencies: list[DependencyEntry] = field(default_factory=list)
    matches: list[LibraryMatch] = field(default_factory=list)
    unmatched: list[DependencyEntry] = field(default_factory=list)


@dataclass
class LinkResult:
    library_id: str
    manifest_path: Path
    dep_version: str


def scan_project(conn: sqlite3.Connection, project_path: Path) -> ProjectScan:
    """Match the project's dependencies against registered libraries.

    Read-only: links are never touched here. Direct manifests take priority
    over lockfiles when the same dependency appears in several manifests.
    """
    project_path = Path(project_path).expanduser().resolve()
    scan = ProjectScan(project_path=project_path, manifests=list_manifests(project_path))
    seen: dict[str, DependencyEntry] = {}
    for manifest in scan.manifests:
        for name, version in extract_dependencies(manifest).items():
            seen.setdefault(name, DependencyEntry(name=name, version=version, manifest=manifest))
    scan.dependencies = list(seen.values())

    libraries: dict[str, list] = {}
    for row in list_libraries(conn):
        libraries.setdefault(normalize_name(row["name"]), []).append(row)
    for dependency in scan.dependencies:
        candidates = libraries.get(dependency.name)
        if not candidates:
            scan.unmatched.append(dependency)
            continue
        library = next(
            (
                row
                for row in candidates
                if version_status(row["version"], dependency.version) == "match"
            ),
            sorted(candidates, key=lambda row: (row["channel"] != "release", row["id"]))[0],
        )
        scan.matches.append(
            LibraryMatch(
                library_id=library["id"],
                library_name=library["name"],
                doc_version=library["version"],
                dependency=dependency,
                status=version_status(library["version"], dependency.version),
            )
        )
    return scan


def link_matches(conn: sqlite3.Connection, scan: ProjectScan) -> tuple[list[LinkResult], list[str]]:
    """Opt-in linking of every matched library to the manifest it was found in.

    The lockfile-first resolution decides the recorded manifest and version;
    the scan's manifest is only a fallback when no manifest resolves it.
    """
    linked: list[LinkResult] = []
    errors: list[str] = []
    for match in scan.matches:
        resolved = resolve_project_version(scan.project_path, match.library_name)
        manifest = resolved.manifest_path if resolved is not None else match.dependency.manifest
        try:
            info = link_library(
                conn,
                match.library_id,
                scan.project_path,
                manifest=manifest,
            )
        except ValueError as exc:
            errors.append(f"{match.library_id}: {exc}")
            continue
        linked.append(
            LinkResult(
                library_id=match.library_id,
                manifest_path=info.manifest_path,
                dep_version=info.dep_version,
            )
        )
    return linked, errors


def format_report(scan: ProjectScan, linked: list[LinkResult], link_requested: bool) -> list[str]:
    lines = [f"virtudoc init — project: {scan.project_path}"]
    if scan.manifests:
        lines.append(f"manifests scanned: {', '.join(path.name for path in scan.manifests)}")
    else:
        lines.append("manifests scanned: none")
    lines.append(f"dependencies found: {len(scan.dependencies)}")
    lines.append("")

    if scan.matches:
        lines.append("matched libraries:")
        for match in scan.matches:
            dep_version = match.dependency.version or "unknown"
            doc_version = match.doc_version or "unknown"
            lines.append(
                f"  {match.library_id}  docs {doc_version}  project {dep_version}  "
                f"{match.status}  ({match.dependency.manifest.name})"
            )
    else:
        lines.append("matched libraries: none")

    lines.append("")
    if scan.unmatched:
        shown = scan.unmatched[:UNMATCHED_HINT_LIMIT]
        lines.append(f"unmatched dependencies ({len(scan.unmatched)}):")
        for dependency in shown:
            version = dependency.version or "unknown"
            lines.append(f"  {dependency.name} {version} ({dependency.manifest.name})")
        remaining = len(scan.unmatched) - len(shown)
        if remaining > 0:
            lines.append(f"  ... and {remaining} more")
        lines.append(
            "  hint: register documentation for a dependency with "
            "`virtudoc add <docs-path> --name <dependency> --version <version>`"
        )
    else:
        lines.append("unmatched dependencies: none")

    if not scan.matches:
        lines.append("")
        lines.append(
            "no registered library matches this project; index a dependency's docs "
            "with `virtudoc add` and run `virtudoc init` again"
        )

    lines.append("")
    if link_requested:
        if linked:
            lines.append("linked:")
            for link in linked:
                lines.append(f"  {link.library_id} -> {link.manifest_path} ({link.dep_version})")
        else:
            lines.append("linked: none")
    else:
        lines.append("links: unchanged (pass --link to link matched libraries)")
    return lines
