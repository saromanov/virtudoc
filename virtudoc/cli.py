from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config, data_dir
from .db import open_db
from .indexer.chunker import Chunker
from .indexer.indexer import (
    IndexLockError,
    LibraryExistsError,
    LibraryNotFoundError,
    add_library,
    delete_library,
    get_library,
    index_library,
    list_libraries,
    update_all,
)
from .logging_setup import setup_logging
from .search.engine import search


def _chunker_from_config(cfg: Config) -> Chunker:
    return Chunker(
        target_tokens=cfg.get_int("chunk.target_tokens"),
        max_tokens=cfg.get_int("chunk.max_tokens"),
        overlap_tokens=cfg.get_int("chunk.overlap_tokens"),
    )


def _open(cfg: Config):
    return open_db(cfg.db_path)


def _print_skipped(skipped_files: list[str]) -> None:
    for rel_path in skipped_files:
        print(f"  ! skipped (over 5 MB): {rel_path}", file=sys.stderr)


def cmd_add(args: argparse.Namespace, cfg: Config) -> int:
    with _open(cfg) as conn:
        try:
            lib_id = add_library(
                conn,
                Path(args.path),
                args.name,
                version=args.version,
                description=args.description,
                replace=args.replace,
            )
        except (LibraryExistsError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        stats = index_library(conn, lib_id, _chunker_from_config(cfg))
    print(f"added library {lib_id}")
    print(stats.summary())
    _print_skipped(stats.skipped_files)
    return 0 if stats.errors == 0 else 1


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
            "note: changed chunk.* settings only affect new indexing; to rebuild "
            "the index, remove the library and add it again"
        )
    return 0


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
        result = search(
            conn,
            args.query,
            library_id=library_id,
            token_budget=args.tokens or cfg.get_int("search.default_tokens"),
            max_results=cfg.get_int("search.max_results"),
        )
    if not result.snippets:
        print("nothing found; try rephrasing the query with keywords")
        return 0
    for snippet in result.snippets:
        heading = snippet.heading_path or "(no section)"
        print(f"\n## [{snippet.score:.2f}] {heading} — {snippet.source} (~{snippet.tokens} tokens)")
        print(snippet.text)
    print(f"\n---\nfragments: {result.total_chunks}, tokens: {result.total_tokens}")
    return 0


def cmd_serve(args: argparse.Namespace, cfg: Config) -> int:
    print("the serve command will be implemented in milestone M2 (MCP server)", file=sys.stderr)
    return 2


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

    p = sub.add_parser("update", help="incremental reindexing")
    p.add_argument("--name", help="library name (omitted — all libraries)")
    p.set_defaults(func=cmd_update)

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
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("serve", help="start the MCP server (milestone M2)")
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = Config.load()
    setup_logging(data_dir(), cfg.get_str("log.level"))
    return int(args.func(args, cfg))


if __name__ == "__main__":
    raise SystemExit(main())
