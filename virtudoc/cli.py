from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from .config import DATA_DIR_ENV, Config, data_dir
from .db import open_db
from .doctor import run_doctor
from .indexer.chunker import Chunker
from .indexer.indexer import (
    IndexLockError,
    LibraryExistsError,
    LibraryNotFoundError,
    add_and_index_library,
    delete_library,
    get_library,
    index_library,
    list_libraries,
    reindex_library,
    update_all,
)
from .logging_setup import setup_logging
from .notes import add_note, list_notes, relevant_notes, remove_note
from .onboarding import format_report, link_matches, scan_project
from .search.engine import library_outline, search
from .versions import MANIFEST_CANDIDATES, link_library, unlink_library


def _chunker_from_config(cfg: Config) -> Chunker:
    return Chunker(
        target_tokens=cfg.get_int("chunk.target_tokens"),
        max_tokens=cfg.get_int("chunk.max_tokens"),
        overlap_tokens=cfg.get_int("chunk.overlap_tokens"),
    )


def _open(cfg: Config) -> closing[sqlite3.Connection]:
    # The sqlite3 connection context manager only commits; closing() releases
    # the connection so repeated CLI calls in one process do not leak handles.
    return closing(open_db(cfg.db_path))


def _print_skipped(skipped_files: list[str]) -> None:
    for rel_path in skipped_files:
        print(f"  ! skipped (over 5 MB): {rel_path}", file=sys.stderr)


def cmd_add(args: argparse.Namespace, cfg: Config) -> int:
    with _open(cfg) as conn:
        try:
            lib_id, stats = add_and_index_library(
                conn,
                Path(args.path),
                args.name,
                chunker=_chunker_from_config(cfg),
                version=args.version,
                description=args.description,
                replace=args.replace,
            )
        except (LibraryExistsError, ValueError, IndexLockError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(f"added library {lib_id}")
    print(stats.summary())
    _print_skipped(stats.skipped_files)
    return 0 if stats.errors == 0 else 1


def cmd_init(args: argparse.Namespace, cfg: Config) -> int:
    project = Path(args.path).expanduser()
    if not project.is_dir():
        print(f"error: project directory not found: {project}", file=sys.stderr)
        return 1
    with _open(cfg) as conn:
        scan = scan_project(conn, project)
        if not scan.manifests:
            print(
                f"error: no dependency manifest found in {project.expanduser().resolve()}",
                file=sys.stderr,
            )
            print(f"looked for: {', '.join(MANIFEST_CANDIDATES)}", file=sys.stderr)
            return 1
        linked, errors = link_matches(conn, scan) if args.link else ([], [])
    for line in format_report(scan, linked, args.link):
        print(line)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 1 if errors else 0


def cmd_update(args: argparse.Namespace, cfg: Config) -> int:
    chunker = _chunker_from_config(cfg)
    with _open(cfg) as conn:
        try:
            if args.name:
                lib = get_library(conn, args.name)
                stats = index_library(conn, lib["id"], chunker)
                print(f"{lib['id']}: {stats.summary()}")
                _print_skipped(stats.skipped_files)
                return 0 if stats.errors == 0 else 1
            results = update_all(conn, chunker)
        except (LibraryNotFoundError, IndexLockError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if not results:
        print("no libraries registered")
        return 0
    for lib_id, stats in results.items():
        print(f"{lib_id}: {stats.summary()}")
        _print_skipped(stats.skipped_files)
    chunk_keys = [
        key
        for key in ("chunk.target_tokens", "chunk.max_tokens", "chunk.overlap_tokens")
        if cfg.values.get(key) != Config().values[key]
    ]
    if chunk_keys:
        print(
            "note: changed chunk.* settings only affect new indexing; "
            f"rebuild the index with: virtudoc reindex {args.name or '<name>'}"
        )
    return 0


def cmd_reindex(args: argparse.Namespace, cfg: Config) -> int:
    chunker = _chunker_from_config(cfg)
    with _open(cfg) as conn:
        try:
            lib = get_library(conn, args.name)
            stats = reindex_library(conn, lib["id"], chunker)
        except (LibraryNotFoundError, IndexLockError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(f"{lib['id']}: {stats.summary()}")
    _print_skipped(stats.skipped_files)
    return 0 if stats.errors == 0 else 1


def cmd_list(args: argparse.Namespace, cfg: Config) -> int:
    with _open(cfg) as conn:
        rows = list_libraries(conn)
    if not rows:
        print("no libraries registered")
        return 0
    header = f"{'ID':<28} {'Documents':>9} {'Chunks':>8} {'Size':>10} {'Updated':<20} Source"
    print(header)
    for row in rows:
        size = row["index_bytes"]
        size_str = f"{size / 1024:.0f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"
        print(
            f"{row['id']:<28} {row['doc_count']:>9} {row['chunk_count']:>8} "
            f"{size_str:>10} {row['updated_at']:<20} {row['source_path']}"
        )
    return 0


def cmd_remove(args: argparse.Namespace, cfg: Config) -> int:
    lib_id = f"local/{args.name}"
    if not args.yes:
        answer = input(f"Remove library {lib_id} and all its data? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("cancelled")
            return 0
    with _open(cfg) as conn:
        try:
            get_library(conn, args.name)
        except LibraryNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        delete_library(conn, args.name)
    print(f"removed library {lib_id}")
    return 0


def cmd_search(args: argparse.Namespace, cfg: Config) -> int:
    with _open(cfg) as conn:
        library_id = None
        if args.name:
            try:
                library_id = get_library(conn, args.name)["id"]
            except LibraryNotFoundError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        if args.outline:
            if not library_id:
                print("error: --outline requires --name", file=sys.stderr)
                return 1
            tree = library_outline(conn, library_id, section=args.section)
            print(
                f"outline of {library_id}" + (f" (section: {args.section})" if args.section else "")
            )
            _print_outline(tree)
            return 0
        result = search(
            conn,
            args.query,
            library_id=library_id,
            token_budget=args.tokens or cfg.get_int("search.default_tokens"),
            max_results=cfg.get_int("search.max_results"),
            page=args.page,
            section=args.section,
            window_tokens=cfg.get_int("search.snippet_window_tokens"),
        )
        notes = []
        if library_id:
            notes = relevant_notes(
                conn,
                library_id,
                args.query,
                budget_tokens=cfg.get_int("notes.max_tokens"),
            )
    if notes:
        print("=== project notes ===")
        for note in notes:
            print(f"[note #{note.id} | {note.created_at}]\n{note.text}\n")
    if not result.snippets:
        if result.total_matches:
            print(
                f"no fragments on page {result.page}; total_matches: {result.total_matches}, "
                f"has_more: {str(result.has_more).lower()}"
            )
        else:
            print("nothing found; try rephrasing the query with keywords")
        return 0
    for snippet in result.snippets:
        heading = snippet.heading_path or "(no section)"
        print(f"\n## [{snippet.score:.2f}] {heading} — {snippet.source} (~{snippet.tokens} tokens)")
        print(snippet.text)
    print(
        f"\n---\nfragments: {result.page_chunks}, total_matches: {result.total_matches}, "
        f"has_more: {str(result.has_more).lower()}, tokens: {result.total_tokens}"
    )
    if result.has_more:
        print(f"next page: {result.page + 1}")
    return 0


def cmd_serve(args: argparse.Namespace, cfg: Config) -> int:
    from .mcp_server import run_serve

    return run_serve(cfg, http=args.http)


def _print_outline(nodes, indent: int = 0) -> None:
    for node in nodes:
        print(
            f"{'  ' * indent}- {node.section} ({node.chunk_count} chunks, ~{node.token_est} tokens)"
        )
        _print_outline(node.children, indent + 1)


def cmd_note_add(args: argparse.Namespace, cfg: Config) -> int:
    with _open(cfg) as conn:
        try:
            lib = get_library(conn, args.name)
        except LibraryNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        note_id = add_note(conn, lib["id"], args.text)
    print(f"added note #{note_id} to {lib['id']}")
    return 0


def cmd_note_list(args: argparse.Namespace, cfg: Config) -> int:
    with _open(cfg) as conn:
        try:
            lib = get_library(conn, args.name)
        except LibraryNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        notes = list_notes(conn, lib["id"])
    if not notes:
        print(f"no notes for {lib['id']}")
        return 0
    for note in notes:
        print(f"#{note.id}\t{note.created_at}\t{note.text}")
    return 0


def cmd_note_remove(args: argparse.Namespace, cfg: Config) -> int:
    with _open(cfg) as conn:
        try:
            lib = get_library(conn, args.name)
        except LibraryNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        removed = remove_note(conn, lib["id"], args.id)
    if not removed:
        print(f"error: note #{args.id} not found in {lib['id']}", file=sys.stderr)
        return 1
    print(f"removed note #{args.id} from {lib['id']}")
    return 0


def cmd_link(args: argparse.Namespace, cfg: Config) -> int:
    with _open(cfg) as conn:
        try:
            lib = get_library(conn, args.name)
        except LibraryNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        try:
            info = link_library(conn, lib["id"], Path(args.project))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(f"linked {lib['id']} -> {info.manifest_path} (project version: {info.dep_version})")
    return 0


def cmd_unlink(args: argparse.Namespace, cfg: Config) -> int:
    with _open(cfg) as conn:
        try:
            lib = get_library(conn, args.name)
        except LibraryNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        removed = unlink_library(conn, lib["id"])
    if not removed:
        print(f"{lib['id']} has no project link")
        return 0
    print(f"unlinked {lib['id']}")
    return 0


def _server_command() -> tuple[str, list[str]]:
    """Executable and base arguments an MCP client needs to start the server.

    MCP clients execute `command` directly without a shell, so the
    `-m virtudoc` fallback must be passed through `args` rather than
    appended to the command string.
    """
    argv0 = Path(sys.argv[0]).resolve() if sys.argv[0] else None
    if argv0 and argv0.name == "virtudoc" and argv0.is_file():
        return str(argv0), []
    resolved = shutil.which("virtudoc")
    if resolved:
        return str(Path(resolved).resolve()), []
    return sys.executable, ["-m", "virtudoc"]


def cmd_mcp_config(args: argparse.Namespace, cfg: Config) -> int:
    import json

    command, base_args = _server_command()
    server_entry: dict[str, object] = {
        "command": command,
        "args": [*base_args, "serve"],
    }
    env = {"VIRTUDOC_HOME": str(data_dir())} if os.environ.get(DATA_DIR_ENV) else {}
    if env:
        server_entry["env"] = env
    print(
        "Paste into the MCP client config (Claude Code, Cursor, etc.):",
        file=sys.stderr,
    )
    print(json.dumps({"mcpServers": {"virtudoc": server_entry}}, indent=2))
    return 0


def cmd_doctor(args: argparse.Namespace, cfg: Config) -> int:
    with _open(cfg) as conn:
        result = run_doctor(conn, cfg)
    print("virtudoc doctor")
    for line in result.lines:
        print(line)
    return 1 if result.problems else 0


def cmd_fetch(args: argparse.Namespace, cfg: Config) -> int:
    from .fetcher import FetchError, fetch_source

    configured_branch = cfg.values.get("fetch.branch")
    branch = args.branch or (str(configured_branch) if configured_branch else None)
    try:
        source = fetch_source(
            args.repo,
            args.name,
            branch=branch,
            max_extract_bytes=cfg.get_int("fetch.max_extract_bytes"),
            max_extract_files=cfg.get_int("fetch.max_extract_files"),
        )
    except (FetchError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    with _open(cfg) as conn:
        try:
            lib_id, stats = add_and_index_library(
                conn,
                source,
                args.name,
                chunker=_chunker_from_config(cfg),
                version=args.version,
                description=args.repo,
                replace=args.replace,
            )
        except (LibraryExistsError, ValueError, IndexLockError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(f"fetched {args.repo} into {source}")
    print(f"added library {lib_id}")
    print(stats.summary())
    _print_skipped(stats.skipped_files)
    return 0 if stats.errors == 0 else 1


def cmd_export(args: argparse.Namespace, cfg: Config) -> int:
    import json

    with _open(cfg) as conn:
        rows = list_libraries(conn)
    lines = [
        "# VirtuDoc libraries — restore with: virtudoc import",
        "# Paths are absolute; adjust after moving between machines if needed.",
        "",
    ]
    for row in rows:
        lines.append("[[libraries]]")
        lines.append(f"name = {json.dumps(row['name'], ensure_ascii=False)}")
        lines.append(f"path = {json.dumps(row['source_path'], ensure_ascii=False)}")
        if row["version"]:
            lines.append(f"version = {json.dumps(row['version'], ensure_ascii=False)}")
        lines.append("")
    out = Path(args.path) if args.path else data_dir() / "libraries.toml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"exported {len(rows)} libraries to {out}")
    return 0


def cmd_import(args: argparse.Namespace, cfg: Config) -> int:
    import tomllib

    path = Path(args.path) if args.path else data_dir() / "libraries.toml"
    if not path.is_file():
        print(f"error: setup file not found: {path}", file=sys.stderr)
        return 1
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        print(f"error: cannot parse {path}: {exc}", file=sys.stderr)
        return 1
    libraries = data.get("libraries", [])
    if not libraries:
        print(f"no libraries in {path}")
        return 0
    chunker = _chunker_from_config(cfg)
    imported, failed = 0, 0
    with _open(cfg) as conn:
        for lib in libraries:
            name = str(lib.get("name", ""))
            source = lib.get("path")
            if not name or not source:
                print(f"error: entry without name/path: {lib!r}", file=sys.stderr)
                failed += 1
                continue
            try:
                lib_id, stats = add_and_index_library(
                    conn,
                    Path(str(source)),
                    name,
                    chunker=chunker,
                    version=str(lib["version"]) if lib.get("version") else None,
                    replace=True,
                )
            except (LibraryExistsError, ValueError, OSError, IndexLockError) as exc:
                print(f"error: {name}: {exc}", file=sys.stderr)
                failed += 1
                continue
            imported += 1
            print(f"{lib_id}: {stats.summary()}")
    print(f"imported: {imported}, failed: {failed}")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="virtudoc",
        description="Local documentation server for AI agents",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add", help="register a source and run the first indexing")
    p.add_argument("path", help="path to the documentation directory")
    p.add_argument("--name", required=True, help="library name")
    p.add_argument("--version", help="documentation version")
    p.add_argument("--description", help="library description")
    p.add_argument("--replace", action="store_true", help="replace an existing library")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser(
        "init",
        help="scan a project's dependency manifests and report matching libraries",
    )
    p.add_argument("path", nargs="?", default=".", help="project directory (default: current)")
    p.add_argument(
        "--link",
        action="store_true",
        help="link matched libraries to the project (off by default)",
    )
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("update", help="incremental reindexing")
    p.add_argument("--name", help="library name (omitted — all libraries)")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser(
        "reindex",
        help="full rebuild of one library, applying the current chunk.* settings",
    )
    p.add_argument("name", help="library name")
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser("list", help="list libraries with index statistics")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("remove", help="remove a library and its data")
    p.add_argument("name", help="library name")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("search", help="search the index")
    p.add_argument("query", help="search query")
    p.add_argument("--name", help="restrict the search to one library")
    p.add_argument("--tokens", type=int, help="token budget for the output")
    p.add_argument("--page", type=int, default=1, help="page of the ranked results (default: 1)")
    p.add_argument(
        "--section", help="restrict the output to a section branch (e.g. 'API -> Middleware')"
    )
    p.add_argument(
        "--outline",
        action="store_true",
        help="print the section tree instead of fragments (requires --name)",
    )
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("serve", help="start the MCP server")
    p.add_argument(
        "--http",
        nargs="?",
        const=True,
        default=False,
        type=int,
        metavar="PORT",
        help="use streamable HTTP on 127.0.0.1 (default port: config server.http_port)",
    )
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("note", help="project notes attached to a library")
    note_sub = p.add_subparsers(dest="note_command", required=True)

    n = note_sub.add_parser("add", help="attach a note to a library")
    n.add_argument("name", help="library name")
    n.add_argument("--text", required=True, help="note text")
    n.set_defaults(func=cmd_note_add)

    n = note_sub.add_parser("list", help="list notes of a library")
    n.add_argument("name", help="library name")
    n.set_defaults(func=cmd_note_list)

    n = note_sub.add_parser("remove", help="remove a note from a library")
    n.add_argument("name", help="library name")
    n.add_argument("--id", type=int, required=True, help="note id")
    n.set_defaults(func=cmd_note_remove)

    p = sub.add_parser("link", help="link a library to a project dependency manifest")
    p.add_argument("name", help="library name")
    p.add_argument("--project", required=True, help="path to the consuming project")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("unlink", help="remove the project link of a library")
    p.add_argument("name", help="library name")
    p.set_defaults(func=cmd_unlink)

    p = sub.add_parser("doctor", help="check configuration, sources, index integrity")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser(
        "mcp-config",
        help="print a ready MCP client config snippet with the absolute server path",
    )
    p.set_defaults(func=cmd_mcp_config)

    p = sub.add_parser(
        "fetch",
        help="download documentation from a URL into the local cache and index it "
        "(the only command that uses the network)",
    )
    p.add_argument("--repo", required=True, help="git URL or archive URL (.zip, .tar.gz)")
    p.add_argument("--name", required=True, help="library name")
    p.add_argument("--version", help="documentation version")
    p.add_argument(
        "--branch",
        help="git branch to fetch (default: config fetch.branch, else the repo default branch)",
    )
    p.add_argument("--replace", action="store_true", help="replace an existing library")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("export", help="dump the library list to a TOML setup file")
    p.add_argument("path", nargs="?", help="output file (default: ~/.virtudoc/libraries.toml)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import", help="register and index libraries from a TOML setup file")
    p.add_argument("path", nargs="?", help="setup file (default: ~/.virtudoc/libraries.toml)")
    p.set_defaults(func=cmd_import)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = Config.load()
    setup_logging(data_dir(), cfg.get_str("log.level"))
    return int(args.func(args, cfg))


if __name__ == "__main__":
    raise SystemExit(main())
