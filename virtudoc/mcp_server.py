from __future__ import annotations

import ipaddress
import os
import secrets
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__
from .config import Config, data_dir
from .coverage import IdentifierChecker, assess
from .db import open_db
from .fetcher import ensure_pinned_checkout
from .indexer.chunker import Chunker
from .indexer.indexer import refresh_if_stale
from .logging_setup import get_logger
from .notes import build_notes_payload
from .payload import coverage_message, doc_payload
from .search.engine import library_outline, resolve_libraries, search, top_level_sections
from .versions import (
    ResolvedVersion,
    library_version_choices,
    mismatch_payload,
    resolve_project_version,
)

logger = get_logger("mcp")

MAX_TOKENS_HARD_LIMIT = 20000

RESOLVE_DESCRIPTION = (
    "Find the documentation library (id like 'local/<name>') for a given library "
    "name and the user's question. Always call this first to get a libraryId, "
    "then call get-library-docs with that id. The version is resolved from the "
    "consuming project's lockfile or manifest (projectPath, defaults to the "
    "server working directory) and the matching release is ranked first. When "
    "that version is not indexed, the response lists the available versions: "
    "ask the user before using another release. If no library is found, register "
    "the documentation source with the 'virtudoc add <path> --name <name>' CLI command."
)

GET_DOCS_DESCRIPTION = (
    "Get documentation of a library for the agent's question or task. "
    "Use a focused query with keywords (library terminology works better than "
    "full sentences). Output is assembled under a token budget; total_matches "
    "and has_more tell you whether page=2 continues the ranking. Fragments "
    "contain Markdown text, the heading path and the source file path. If "
    "nothing is found, the response contains top-level sections of the library "
    "to help rephrase the query. Use mode='outline' to get the section tree "
    "with token estimates instead of fragments, and 'section' to restrict the "
    "output to one branch (e.g. 'API -> Middleware'). Team notes carry "
    "status='current', 'stale' or 'unknown'; notes_warning lists stale ones. "
    "missing_identifiers lists query identifiers the documentation never "
    "mentions; a note that mentions one carries mentions_missing. "
    "When a stale note contradicts the fragments, follow the fragments and "
    "tell the user the note may be outdated. The response warns with "
    "'version_mismatch' when the indexed docs differ from the version resolved "
    "from the project's lockfile (projectPath, defaults to the server working "
    "directory); follow 'suggested_library_id' when it is present. "
    "Check 'coverage' before using the result: 'none' means the documentation "
    "does not cover the query, 'partial' means identifiers listed in "
    "'missing_identifiers' are not in the documentation at all (they may be "
    "renamed or removed). In both cases do not use those identifiers from "
    "memory; tell the user instead."
)


def _project_payload(project: Path, resolved: ResolvedVersion) -> dict[str, str]:
    return {
        "path": str(project),
        "manifest": str(resolved.manifest_path),
        "dependency_name": resolved.dependency_name,
        "dependency_version": resolved.version,
    }


def build_server(config: Config) -> MCPServer:
    server = MCPServer(
        name="virtudoc",
        version=__version__,
        instructions=(
            "Local documentation provider. Workflow: 1) find-library to "
            "find the library id, 2) get-library-docs with a keyword query. "
            "3) respect 'coverage' and the 'status' of notes; never fill gaps "
            "from memory. Everything runs offline on the user's machine."
        ),
    )
    chunker = Chunker(
        target_tokens=config.get_int("chunk.target_tokens"),
        max_tokens=config.get_int("chunk.max_tokens"),
        overlap_tokens=config.get_int("chunk.overlap_tokens"),
    )

    def connect() -> closing[sqlite3.Connection]:
        # sqlite3.Connection's own context manager only manages the
        # transaction; without closing() the connection object would leak
        # on every tool call for the lifetime of the server process.
        return closing(open_db(config.db_path))

    @server.tool(name="find-library", description=RESOLVE_DESCRIPTION)
    def find_library(
        libraryName: str,
        query: str,
        projectPath: str | None = None,
    ) -> dict[str, Any]:
        project = Path(projectPath).expanduser() if projectPath else Path.cwd()
        resolved = resolve_project_version(project, libraryName)
        with connect() as conn:
            matches = resolve_libraries(
                conn,
                libraryName,
                query,
                resolved_version=resolved.version if resolved else None,
            )
            choices = library_version_choices(conn, libraryName)
            if not choices:
                seen: set[str] = set()
                for match in matches:
                    for choice in library_version_choices(conn, match.name):
                        if choice["id"] not in seen:
                            seen.add(choice["id"])
                            choices.append(choice)
        if not matches:
            payload: dict[str, Any] = {
                "libraries": [],
                "message": (
                    "no libraries found; register a documentation source with "
                    "the 'virtudoc add <path> --name <name>' CLI command"
                ),
            }
            if resolved is not None:
                payload["project"] = _project_payload(project, resolved)
            return payload
        payload = {
            "libraries": [
                {
                    "id": match.id,
                    "name": match.name,
                    "description": match.description,
                    "chunk_count": match.chunk_count,
                    "updated_at": match.updated_at,
                    "version": match.version,
                    "channel": match.channel,
                    "version_status": match.version_status,
                }
                for match in matches
            ]
        }
        if choices:
            payload["available_versions"] = choices
        if resolved is not None:
            payload["project"] = _project_payload(project, resolved)
            payload["resolved_version"] = resolved.version
            if not any(match.version_status == "match" for match in matches):
                available = ", ".join(
                    f"{choice['version'] or 'unversioned'} ({choice['id']})" for choice in choices
                )
                payload["version_not_indexed"] = True
                payload["message"] = (
                    f"the project depends on {resolved.dependency_name} "
                    f"{resolved.version} ({resolved.manifest_path.name}), but that "
                    f"version is not indexed (available: {available or 'none'}). "
                    "Ask the user before using another version."
                )
        return payload

    @server.tool(name="get-library-docs", description=GET_DOCS_DESCRIPTION)
    def get_library_docs(
        libraryId: str,
        query: str,
        tokens: int = 5000,
        language: str | None = None,
        page: int = 1,
        mode: str = "fragments",
        section: str | None = None,
        projectPath: str | None = None,
    ) -> dict[str, Any]:
        if mode not in ("fragments", "outline"):
            raise ToolError(f"unsupported mode: {mode!r}; supported: 'fragments', 'outline'")
        budget = min(max(tokens, 100), MAX_TOKENS_HARD_LIMIT)
        page = max(page, 1)
        with connect() as conn:
            lib = conn.execute("SELECT id FROM libraries WHERE id = ?", (libraryId,)).fetchone()
            if lib is None:
                raise ToolError(
                    f"library not found: {libraryId!r}; "
                    "call find-library to list available libraries"
                )
            # Statuses are computed after the refresh, so they always see the
            # index an agent is about to read (FR-1.6).
            if config.get_bool("index.auto_refresh"):
                refresh_if_stale(conn, libraryId, chunker)
            checker = IdentifierChecker(conn, libraryId)
            warning = mismatch_payload(conn, libraryId, project_path=projectPath)
            limit = config.get_int("coverage.max_identifiers")
            if mode == "outline":
                tree = library_outline(conn, libraryId, section=section)
                coverage = assess(
                    conn,
                    libraryId,
                    query,
                    [node.section for node in tree],
                    total_matches=len(tree),
                    outline=True,
                    limit=limit,
                    checker=checker,
                )
                notes = build_notes_payload(
                    conn,
                    libraryId,
                    query,
                    coverage=coverage,
                    budget=config.get_int("notes.max_tokens"),
                    pinned_budget=config.get_int("notes.pinned_max_tokens"),
                    checker=checker,
                )
                return doc_payload(
                    warning,
                    library_id=libraryId,
                    query=query,
                    context={"mode": "outline", "section": section},
                    coverage=coverage,
                    notes=notes,
                    message=coverage_message(coverage, page=page, has_fragments=True),
                    body_key="sections",
                    body=[node.to_dict() for node in tree],
                    trailing={"total_tokens": sum(node.token_est for node in tree)},
                )
            result = search(
                conn,
                query,
                library_id=libraryId,
                token_budget=budget,
                max_results=config.get_int("search.max_results"),
                page=page,
                language=language,
                section=section,
                window_tokens=config.get_int("search.snippet_window_tokens"),
            )
            texts = [snippet.text for snippet in result.snippets]
            coverage = assess(
                conn,
                libraryId,
                query,
                texts,
                total_matches=result.total_matches,
                limit=limit,
                checker=checker,
            )
            notes = build_notes_payload(
                conn,
                libraryId,
                query,
                coverage=coverage,
                budget=config.get_int("notes.max_tokens"),
                pinned_budget=config.get_int("notes.pinned_max_tokens"),
                checker=checker,
            )
            message = coverage_message(
                coverage, page=result.page, has_fragments=bool(result.snippets)
            )
            trailing: dict[str, Any] = {
                "total_tokens": result.total_tokens,
                "total_matches": result.total_matches,
                "has_more": result.has_more,
            }
            if not result.snippets and not result.total_matches:
                trailing["sections"] = [
                    {
                        "section": hint.section,
                        "chunk_count": hint.chunk_count,
                        "token_est": hint.token_est,
                    }
                    for hint in top_level_sections(conn, libraryId)
                ]
            return doc_payload(
                warning,
                library_id=libraryId,
                query=query,
                context={"page": result.page},
                coverage=coverage,
                notes=notes,
                message=message,
                body_key="snippets",
                body=[
                    {
                        "heading_path": snippet.heading_path,
                        "source": snippet.source,
                        "text": snippet.text,
                        "score": snippet.score,
                    }
                    for snippet in result.snippets
                ],
                trailing=trailing,
            )

    return server


def prepare_serve(config: Config) -> None:
    """Reset pinned fetch caches before the transport starts.

    Startup must stay cheap on large corpora: no source is walked or
    hashed here. `get-library-docs` refreshes the requested library
    through `refresh_if_stale` instead.
    """
    with closing(open_db(config.db_path)) as conn:
        rows = conn.execute("SELECT id, source_path FROM libraries ORDER BY id").fetchall()
    for row in rows:
        source = Path(row["source_path"])
        if not (source / ".git").is_dir():
            continue
        pinned = ensure_pinned_checkout(source)
        if pinned:
            logger.info(
                "fetch cache for %s pinned to branch '%s' (reset locally, offline)",
                row["id"],
                pinned,
            )


class BearerAuthMiddleware:
    def __init__(self, app, token: str) -> None:
        self.app = app
        self.expected = b"Bearer " + token.encode("utf-8")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        authorization = headers.get(b"authorization", b"")
        if not secrets.compare_digest(authorization, self.expected):
            body = b'{"error":"unauthorized"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _configured_http_token(config: Config) -> str | None:
    token = os.environ.get("VIRTUDOC_HTTP_TOKEN") or config.values.get("server.http_token")
    return str(token).strip() if token and str(token).strip() else None


def _run_http(server: MCPServer, *, host: str, port: int, token: str | None) -> None:
    import uvicorn

    async def serve() -> None:
        app = server.streamable_http_app(host=host)
        if token is not None:
            app = BearerAuthMiddleware(app, token)
        uvicorn_config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=server.settings.log_level.lower(),
        )
        await uvicorn.Server(uvicorn_config).serve()

    anyio.run(serve)


def run_serve(config: Config, *, http: bool | int = False) -> int:
    print(
        f"virtudoc serve: data directory {data_dir()} (set VIRTUDOC_HOME to override)",
        file=sys.stderr,
    )
    host = config.get_str("server.http_host")
    token = _configured_http_token(config)
    if http and not _is_loopback_host(host) and token is None:
        print(
            "error: non-loopback HTTP requires server.http_token or VIRTUDOC_HTTP_TOKEN",
            file=sys.stderr,
        )
        return 1
    prepare_serve(config)
    server = build_server(config)
    if http:
        port = config.get_int("server.http_port") if http is True else int(http)
        logger.info("starting streamable HTTP transport on %s:%s", host, port)
        _run_http(server, host=host, port=port, token=token)
    else:
        logger.info("starting stdio transport")
        anyio.run(server.run_stdio_async)
    return 0
