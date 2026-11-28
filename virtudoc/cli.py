from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from .agents_hint import ensure_agents_hint
from .config import DATA_DIR_ENV, Config, data_dir
from .coverage import extract_identifiers
from .db import UnsupportedSQLiteError, open_db
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
from .notes import (
    ANCHOR_LIBRARY,
    ANCHOR_NONE,
    SymbolRemovedError,
    add_note,
    edit_note,
    list_notes,
    note_anchors,
    note_payload,
    note_statuses,
    note_symbols,
    pinned_tokens,
    relevant_notes,
    remove_note,
    review_note,
    select_notes,
    set_pinned,
)
from .onboarding import format_report, link_matches, scan_project
from .search.engine import library_outline, search
from .tokens import estimate_tokens
from .versions import (
    MANIFEST_CANDIDATES,
    link_library,
    resolve_local_dependency_version,
    unlink_library,
)


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


def _print_agents_hint(project: Path) -> bool:
    """Write the agents hint, reporting failures on stderr; True on success."""
    try:
        path, changed = ensure_agents_hint(project)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return False
    state = "written to" if changed else "already present in"
    print(f"agents hint {state} {path}")
    return True


def cmd_add(args: argparse.Namespace, cfg: Config) -> int:
    channel = "branch" if args.branch else args.channel
    version, version_source = args.version, None
    # Branch adds carry no release version: a lockfile in the working directory
    # must not turn `local/<name>@branch` into `local/<name>@<version>-branch`.
    if version is None and channel == "release":
        version, version_source = resolve_local_dependency_version(Path.cwd(), args.name)
    with _open(cfg) as conn:
        try:
            lib_id, stats = add_and_index_library(
                conn,
                Path(args.path),
                args.name,
                chunker=_chunker_from_config(cfg),
                version=version,
                description=args.description,
                replace=args.replace,
                channel=channel,
            )
        except (LibraryExistsError, ValueError, IndexLockError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(f"added library {lib_id}")
    if version_source is not None:
        print(f"version pinned from {version_source}: {version}")
    print(stats.summary())
    _print_skipped(stats.skipped_files)
    if args.agents_hint:
        if not _print_agents_hint(Path.cwd()):
            return 1
    else:
        print(
            "hint: pass --agents-hint to add the virtudoc rule to AGENTS.md",
            file=sys.stderr,
        )
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
    if args.agents_hint and not _print_agents_hint(project):
        return 1
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
        if stats.error:
            print(f"error: {lib_id}: {stats.error}", file=sys.stderr)
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
    return 1 if any(stats.errors for stats in results.values()) else 0


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
    header = (
        f"{'ID':<28} {'Version':<10} {'Channel':<8} {'Documents':>9} {'Chunks':>8} "
        f"{'Size':>10} {'Updated':<20} Source"
    )
    print(header)
    for row in rows:
        size = row["index_bytes"]
        size_str = f"{size / 1024:.0f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"
        print(
            f"{row['id']:<28} {row['version'] or '-':<10} {row['channel']:<8} "
            f"{row['doc_count']:>9} {row['chunk_count']:>8} "
            f"{size_str:>10} {row['updated_at']:<20} {row['source_path']}"
        )
    return 0


def cmd_remove(args: argparse.Namespace, cfg: Config) -> int:
    with _open(cfg) as conn:
        try:
            lib = get_library(conn, args.name)
        except LibraryNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        lib_id = lib["id"]
        if not args.yes:
            try:
                answer = input(f"Remove library {lib_id} and all its data? [y/N] ")
            except EOFError:
                print("cancelled (non-interactive shell: pass --yes to remove)")
                return 0
            if answer.strip().lower() not in ("y", "yes"):
                print("cancelled")
                return 0
        delete_library(conn, lib_id)
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
        budget = args.tokens if args.tokens is not None else cfg.get_int("search.default_tokens")
        if budget <= 0:
            print("error: token budget must be a positive number", file=sys.stderr)
            return 1
        result = search(
            conn,
            args.query,
            library_id=library_id,
            token_budget=budget,
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
                pinned_budget_tokens=cfg.get_int("notes.pinned_max_tokens"),
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


def _note_library(conn: sqlite3.Connection, name: str, version: str | None = None):
    """Resolve the variant a note command operates on."""
    lib = get_library(conn, name)
    if version is None:
        return lib
    row = conn.execute(
        "SELECT * FROM libraries WHERE name = ? AND version = ?", (lib["name"], version)
    ).fetchone()
    if row is None:
        raise LibraryNotFoundError(f"library {lib['name']} has no indexed version {version}")
    return row


def _check_note_size(text: str, cfg: Config) -> str | None:
    limit = cfg.get_int("notes.max_note_tokens")
    tokens = estimate_tokens(text)
    if tokens > limit:
        return (
            f"note is {tokens} tokens, limit is {limit} "
            f"(notes.max_note_tokens); split it into several notes"
        )
    return None


def cmd_note_add(args: argparse.Namespace, cfg: Config) -> int:
    error = _check_note_size(args.text, cfg)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    with _open(cfg) as conn:
        try:
            lib = _note_library(conn, args.name, args.version)
            note_id = add_note(
                conn,
                lib["id"],
                args.text,
                paths=args.path,
                scope_version=args.version,
                pinned=args.pin,
                no_anchor=args.no_anchor,
                auto_anchor_k=cfg.get_int("notes.auto_anchor_k"),
                auto_anchor_ratio=cfg.get_float("notes.auto_anchor_ratio"),
                max_symbols=cfg.get_int("notes.max_symbols"),
            )
            anchors = note_anchors(conn, note_id, lib["id"])
            symbols = note_symbols(conn, note_id)
            tracked = set(symbols)
            untracked = [
                identifier
                for identifier in extract_identifiers(
                    args.text, limit=cfg.get_int("notes.max_symbols")
                )
                if identifier not in tracked
            ]
            row = conn.execute("SELECT anchor_kind FROM notes WHERE id = ?", (note_id,)).fetchone()
        except (LibraryNotFoundError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if row["anchor_kind"] == ANCHOR_NONE:
        print(f"Note #{note_id} added to {lib['name']} without anchors (status: unknown)")
        return 0
    print(f"Note #{note_id} added to {lib['name']}.")
    if anchors:
        print(f"  anchored documents: {', '.join(a['rel_path'] for a in anchors)}")
    if symbols:
        print(f"  anchored symbols:   {', '.join(symbols)}")
    if untracked:
        print(f"  not in docs (not tracked): {', '.join(untracked)}")
    if row["anchor_kind"] == ANCHOR_LIBRARY:
        print("  anchored to the whole library (no document matched the note text)")
    return 0


def _anchor_summary(note, anchors: list[sqlite3.Row]) -> str:
    parts = []
    for anchor in anchors:
        suffix = " (chg)" if anchor["rel_path"] in note.stale_details else ""
        parts.append(f"{anchor['rel_path']}{suffix}")
    for symbol in note.symbols:
        if symbol in note.missing_symbols:
            parts.append(f"{symbol} (gone)")
        else:
            parts.append(symbol)
    return ", ".join(parts) or "—"


def _print_note_table(conn: sqlite3.Connection, lib, stale_only: bool = False) -> None:
    notes = note_statuses(conn, lib["id"])
    if stale_only:
        notes = [note for note in notes if note.status == "stale"]
    if not notes:
        print(f"no notes for {lib['id']}")
        return
    print(
        f"{'ID':<4}{'STATUS':<9}{'REASON':<17}{'PIN':<5}{'SCOPE':<10}"
        f"{'REVIEWED':<18}  {'ANCHORS':<28}TEXT"
    )
    for note in notes:
        anchors = note_anchors(conn, note.id, lib["id"])
        reviewed = note.reviewed_at[:16].replace("T", " ") if note.reviewed_at else "—"
        scope = "*" if note.scope_version is None else note.scope_version
        pin = "★" if note.pinned else "-"
        text = note.text.replace("\n", " ")
        if len(text) > 60:
            text = text[:59] + "…"
        print(
            f"{note.id:<4}{note.status:<9}{(note.stale_reason or '—'):<17}{pin:<5}"
            f"{scope:<10}{reviewed:<18}  {_anchor_summary(note, anchors):<28}{text}"
        )


def cmd_note_list(args: argparse.Namespace, cfg: Config) -> int:
    with _open(cfg) as conn:
        try:
            lib = _note_library(conn, args.name, args.version)
        except LibraryNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if getattr(args, "json", False):
            import json

            notes = note_statuses(conn, lib["id"])
            if getattr(args, "stale_only", False):
                notes = [note for note in notes if note.status == "stale"]
            print(json.dumps([note_payload(note) for note in notes], indent=2, ensure_ascii=False))
            return 0
        _print_note_table(conn, lib, stale_only=getattr(args, "stale_only", False))
    return 0


def cmd_note_remove(args: argparse.Namespace, cfg: Config) -> int:
    with _open(cfg) as conn:
        try:
            lib = _note_library(conn, args.name, args.version)
        except LibraryNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        removed = remove_note(conn, lib["id"], args.id)
    if not removed:
        print(f"error: note #{args.id} not found in {lib['id']}", file=sys.stderr)
        return 1
    print(f"removed note #{args.id} from {lib['id']}")
    return 0


def _print_symbol_error(exc: SymbolRemovedError, lib_name: str, version: str | None) -> None:
    suffix = f" --version {version}" if version else ""
    print(f"error: {exc}.", file=sys.stderr)
    print(
        f'  edit the note:  virtudoc note edit {lib_name} {exc.note_id}{suffix} --text "..."',
        file=sys.stderr,
    )
    print(
        f"  or accept it:   virtudoc note review {lib_name} {exc.note_id}{suffix} --reanchor",
        file=sys.stderr,
    )


def cmd_note_review(args: argparse.Namespace, cfg: Config) -> int:
    if not args.all and args.id is None:
        print("error: pass a note id or --all", file=sys.stderr)
        return 1
    with _open(cfg) as conn:
        try:
            lib = _note_library(conn, args.name, args.version)
        except LibraryNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.all:
            notes = note_statuses(conn, lib["id"])
            if getattr(args, "stale_only", False):
                notes = [note for note in notes if note.status == "stale"]
            if not notes:
                print(f"no notes to review for {lib['id']}")
                return 0
            for note in notes:
                print(f"#{note.id}  {note.status}  {note.text}")
            if not args.yes:
                if not sys.stdin.isatty():
                    print(
                        "error: refusing to confirm without a terminal; pass --yes", file=sys.stderr
                    )
                    return 1
                answer = input(f"Review all {len(notes)} notes of {lib['name']}? [y/N] ")
                if answer.strip().lower() not in ("y", "yes"):
                    print("aborted")
                    return 0
            reviewed = 0
            try:
                for note in notes:
                    if review_note(
                        conn,
                        lib["id"],
                        note.id,
                        reanchor=args.reanchor,
                        auto_anchor_k=cfg.get_int("notes.auto_anchor_k"),
                        auto_anchor_ratio=cfg.get_float("notes.auto_anchor_ratio"),
                        max_symbols=cfg.get_int("notes.max_symbols"),
                    ):
                        reviewed += 1
            except SymbolRemovedError as exc:
                _print_symbol_error(exc, lib["name"], args.version)
                return 1
            print(f"reviewed {reviewed} note(s) of {lib['name']}")
            return 0
        try:
            ok = review_note(
                conn,
                lib["id"],
                args.id,
                reanchor=args.reanchor,
                auto_anchor_k=cfg.get_int("notes.auto_anchor_k"),
                auto_anchor_ratio=cfg.get_float("notes.auto_anchor_ratio"),
                max_symbols=cfg.get_int("notes.max_symbols"),
            )
        except SymbolRemovedError as exc:
            _print_symbol_error(exc, lib["name"], args.version)
            return 1
    if not ok:
        print(f"error: note #{args.id} not found in {lib['name']}", file=sys.stderr)
        return 1
    print(f"reviewed note #{args.id} of {lib['name']}")
    return 0


def cmd_note_edit(args: argparse.Namespace, cfg: Config) -> int:
    error = _check_note_size(args.text, cfg)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    with _open(cfg) as conn:
        try:
            lib = _note_library(conn, args.name, args.version)
        except LibraryNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        ok = edit_note(
            conn,
            lib["id"],
            args.id,
            args.text,
            auto_anchor_k=cfg.get_int("notes.auto_anchor_k"),
            auto_anchor_ratio=cfg.get_float("notes.auto_anchor_ratio"),
            max_symbols=cfg.get_int("notes.max_symbols"),
        )
    if not ok:
        print(f"error: note #{args.id} not found in {lib['name']}", file=sys.stderr)
        return 1
    print(f"updated and reviewed note #{args.id} of {lib['name']}")
    return 0


def _set_pin(args: argparse.Namespace, cfg: Config, pinned: bool) -> int:
    with _open(cfg) as conn:
        try:
            lib = _note_library(conn, args.name, args.version)
        except LibraryNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if not set_pinned(conn, lib["id"], args.id, pinned):
            print(f"error: note #{args.id} not found in {lib['name']}", file=sys.stderr)
            return 1
        used = pinned_tokens(conn, lib["id"])
        limit = cfg.get_int("notes.pinned_max_tokens")
        selection = select_notes(
            conn,
            lib["id"],
            "",
            budget_tokens=cfg.get_int("notes.max_tokens"),
            pinned_budget_tokens=limit,
        )
        all_pinned = [note.id for note in note_statuses(conn, lib["id"]) if note.pinned]
        included = {note.id for note in selection.notes if note.pinned}
        omitted = sorted(note_id for note_id in all_pinned if note_id not in included)
    verb = "pinned" if pinned else "unpinned"
    print(f"{verb} note #{args.id} of {lib['name']}")
    if pinned and omitted:
        print(
            f"warning: pinned notes use {used} tokens, over notes.pinned_max_tokens "
            f"({limit}); note(s) {', '.join(f'#{note_id}' for note_id in omitted)} "
            "will be omitted from agent responses",
            file=sys.stderr,
        )
    return 0


def cmd_note_pin(args: argparse.Namespace, cfg: Config) -> int:
    return _set_pin(args, cfg, True)


def cmd_note_unpin(args: argparse.Namespace, cfg: Config) -> int:
    return _set_pin(args, cfg, False)


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
    strict = bool(getattr(args, "strict", False))
    if result.problems:
        return 1
    if strict and result.note_warnings:
        return 1
    return 0


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
                channel=args.channel,
            )
        except (LibraryExistsError, ValueError, IndexLockError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(f"fetched {args.repo} into {source}")
    print(f"added library {lib_id}")
    print(stats.summary())
    _print_skipped(stats.skipped_files)
    return 0 if stats.errors == 0 else 1


def _export_notes(conn: sqlite3.Connection) -> dict[str, list[dict[str, object]]]:
    """Project notes of every logical library, keyed by library name."""
    notes_by_name: dict[str, list[dict[str, object]]] = {}
    for row in list_libraries(conn):
        if row["name"] in notes_by_name:
            continue
        entries: list[dict[str, object]] = []
        for note in list_notes(conn, row["id"]):
            entry: dict[str, object] = {"text": note.text}
            if note.scope_version:
                entry["scope_version"] = note.scope_version
            if note.pinned:
                entry["pinned"] = True
            anchors = [anchor["rel_path"] for anchor in note_anchors(conn, note.id)]
            if anchors:
                entry["anchors"] = list(dict.fromkeys(anchors))
            entries.append(entry)
        notes_by_name[row["name"]] = entries
    return notes_by_name


def _note_toml(note: dict, dumps) -> str:
    """Render one exported note as a TOML inline table."""
    parts = [f"text = {dumps(note['text'], ensure_ascii=False)}"]
    if note.get("scope_version"):
        parts.append(f"scope_version = {dumps(note['scope_version'], ensure_ascii=False)}")
    if note.get("pinned"):
        parts.append("pinned = true")
    anchors = note.get("anchors")
    if anchors:
        rendered = ", ".join(dumps(anchor, ensure_ascii=False) for anchor in anchors)
        parts.append(f"anchors = [{rendered}]")
    return "{ " + ", ".join(parts) + " }"


def cmd_export(args: argparse.Namespace, cfg: Config) -> int:
    import json

    with _open(cfg) as conn:
        rows = list_libraries(conn)
        notes_by_name = _export_notes(conn)
    lines = [
        "# VirtuDoc libraries — restore with: virtudoc import",
        "# Paths are absolute; adjust after moving between machines if needed.",
        "# Project links are per-machine and are not exported: recreate them with",
        "# `virtudoc init --link` in each project after importing.",
        "",
    ]
    for row in rows:
        lines.append("[[libraries]]")
        lines.append(f"name = {json.dumps(row['name'], ensure_ascii=False)}")
        lines.append(f"path = {json.dumps(row['source_path'], ensure_ascii=False)}")
        if row["version"]:
            lines.append(f"version = {json.dumps(row['version'], ensure_ascii=False)}")
        if row["channel"] != "release":
            lines.append(f"channel = {json.dumps(row['channel'], ensure_ascii=False)}")
        if row["description"]:
            lines.append(f"description = {json.dumps(row['description'], ensure_ascii=False)}")
        notes = notes_by_name.get(row["name"]) or []
        if notes:
            lines.append("notes = [")
            lines.extend(f"    {_note_toml(note, json.dumps)}," for note in notes)
            lines.append("]")
        lines.append("")
    out = Path(args.path) if args.path else data_dir() / "libraries.toml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"exported {len(rows)} libraries to {out}")
    return 0


def _restore_notes(conn: sqlite3.Connection, lib_id: str, entries: list) -> int:
    existing = {note.text: note.id for note in list_notes(conn, lib_id)}
    documents = {
        row["rel_path"]
        for row in conn.execute("SELECT rel_path FROM documents WHERE library_id = ?", (lib_id,))
    }
    restored = 0
    for entry in entries:
        if not isinstance(entry, dict):
            print(f"error: {lib_id}: malformed note: {entry!r}", file=sys.stderr)
            continue
        text = str(entry.get("text", "")).strip()
        if not text:
            print(f"error: {lib_id}: note without text", file=sys.stderr)
            continue
        scope_version = entry.get("scope_version")
        scope_version = str(scope_version) if scope_version else None
        pinned = bool(entry.get("pinned", False))
        if text in existing:
            conn.execute(
                "UPDATE notes SET pinned = ?, scope_version = ? WHERE id = ?",
                (1 if pinned else 0, scope_version, existing[text]),
            )
            conn.commit()
            continue
        anchors = entry.get("anchors")
        if not isinstance(anchors, list):
            anchors = []
        paths = [str(anchor) for anchor in anchors if str(anchor) in documents]
        try:
            note_id = add_note(
                conn,
                lib_id,
                text,
                paths=paths or None,
                scope_version=scope_version,
                pinned=pinned,
            )
        except ValueError as exc:
            print(f"error: {lib_id}: note: {exc}", file=sys.stderr)
            continue
        existing[text] = note_id
        restored += 1
    return restored


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
    if not isinstance(libraries, (list, dict)):
        print(
            f"error: 'libraries' must be a list of tables in {path}",
            file=sys.stderr,
        )
        return 1
    if not libraries:
        print(f"no libraries in {path}")
        return 0
    chunker = _chunker_from_config(cfg)
    imported, failed = 0, 0
    with _open(cfg) as conn:
        for lib in libraries:
            if not isinstance(lib, dict):
                print(f"error: malformed entry: {lib!r}", file=sys.stderr)
                failed += 1
                continue
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
                    description=str(lib["description"]) if lib.get("description") else None,
                    replace=True,
                    channel=str(lib.get("channel") or "release"),
                )
            except (LibraryExistsError, ValueError, OSError, IndexLockError) as exc:
                print(f"error: {name}: {exc}", file=sys.stderr)
                failed += 1
                continue
            imported += 1
            print(f"{lib_id}: {stats.summary()}")
            entries = lib.get("notes") or []
            if not isinstance(entries, list):
                print(f"error: {name}: 'notes' must be a list of tables", file=sys.stderr)
                continue
            restored = _restore_notes(conn, lib_id, entries)
            if restored:
                print(f"{lib_id}: restored {restored} note(s)")
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
    p.add_argument(
        "--version",
        help="documentation version (default: resolved from the project's dependency manifests)",
    )
    p.add_argument("--description", help="library description")
    p.add_argument(
        "--channel",
        choices=("release", "branch"),
        default="release",
        help="index a release (default) or opt into a working branch",
    )
    p.add_argument(
        "--branch",
        action="store_true",
        help="shorthand for --channel branch (a working tree, not a release)",
    )
    p.add_argument("--replace", action="store_true", help="replace an existing library")
    p.add_argument(
        "--agents-hint",
        action="store_true",
        help="add the virtudoc recommendation block to AGENTS.md (or CLAUDE.md)",
    )
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
    p.add_argument(
        "--agents-hint",
        action="store_true",
        help="add the virtudoc recommendation block to the project's AGENTS.md",
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
    n.add_argument(
        "--path",
        action="append",
        help="anchor the note to an indexed document path (repeatable)",
    )
    n.add_argument("--version", help="show the note only for this library version")
    n.add_argument(
        "--no-anchor",
        action="store_true",
        help="do not anchor the note (its status stays unknown)",
    )
    n.add_argument("--pin", action="store_true", help="send the note with every response")
    n.set_defaults(func=cmd_note_add)

    n = note_sub.add_parser("list", help="list notes of a library")
    n.add_argument("name", help="library name")
    n.add_argument("--version", help="status relative to this library version")
    n.add_argument("--stale-only", action="store_true", help="show only stale notes")
    n.add_argument("--json", action="store_true", help="print the MCP note objects")
    n.set_defaults(func=cmd_note_list)

    n = note_sub.add_parser("remove", help="remove a note from a library")
    n.add_argument("name", help="library name")
    n.add_argument("--id", type=int, required=True, help="note id")
    n.add_argument("--version", help="library version")
    n.set_defaults(func=cmd_note_remove)

    n = note_sub.add_parser("review", help="confirm a note against the current docs")
    n.add_argument("name", help="library name")
    n.add_argument("id", type=int, nargs="?", help="note id")
    n.add_argument("--all", action="store_true", help="review every note of the library")
    n.add_argument("--version", help="library version whose docs confirm the note")
    n.add_argument(
        "--reanchor",
        action="store_true",
        help="recompute anchors from the current documentation",
    )
    n.add_argument(
        "--stale-only",
        action="store_true",
        help="with --all, review only stale notes",
    )
    n.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    n.set_defaults(func=cmd_note_review)

    n = note_sub.add_parser("edit", help="change the note text and review it")
    n.add_argument("name", help="library name")
    n.add_argument("id", type=int, help="note id")
    n.add_argument("--text", required=True, help="new note text")
    n.add_argument("--version", help="library version")
    n.set_defaults(func=cmd_note_edit)

    n = note_sub.add_parser("pin", help="send the note with every response")
    n.add_argument("name", help="library name")
    n.add_argument("id", type=int, help="note id")
    n.add_argument("--version", help="library version")
    n.set_defaults(func=cmd_note_pin)

    n = note_sub.add_parser("unpin", help="stop sending the note with every response")
    n.add_argument("name", help="library name")
    n.add_argument("id", type=int, help="note id")
    n.add_argument("--version", help="library version")
    n.set_defaults(func=cmd_note_unpin)

    p = sub.add_parser("link", help="link a library to a project dependency manifest")
    p.add_argument("name", help="library name")
    p.add_argument("--project", required=True, help="path to the consuming project")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("unlink", help="remove the project link of a library")
    p.add_argument("name", help="library name")
    p.set_defaults(func=cmd_unlink)

    p = sub.add_parser("doctor", help="check configuration, sources, index integrity")
    p.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when stale notes exist (for CI and pre-commit)",
    )
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
    p.add_argument(
        "--channel",
        choices=("release", "branch"),
        default="release",
        help="index a release (default) or opt into a working branch",
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
    try:
        return int(args.func(args, cfg))
    except UnsupportedSQLiteError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
