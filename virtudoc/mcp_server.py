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
from .db import open_db
from .fetcher import ensure_pinned_checkout
from .indexer.chunker import Chunker
from .indexer.indexer import refresh_if_stale
from .logging_setup import get_logger
from .notes import NotesSelection, select_notes
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
    "When a stale note contradicts the fragments, follow the fragments and "
    "tell the user the note may be outdated. The response warns with "
    "'version_mismatch' when the indexed docs differ from the version resolved "
    "from the project's lockfile (projectPath, defaults to the server working "
    "directory); follow 'suggested_library_id' when it is present."
)

NOTES_WARNING = (
    "{count} team note(s) may be outdated: the documentation they rely on "
    "changed after they were written or last reviewed. If a note contradicts "
    "the fragments, follow the fragments and tell the user the note may be "
    "outdated."
)


def _notes_payload(selection: NotesSelection) -> list[dict[str, Any]]:
    return [
        {
            "id": note.id,
            "text": note.text,
            "created_at": note.created_at,
            "status": note.status,
            "stale_reason": note.stale_reason,
            "changed_sources": note.changed_sources,
            "reviewed_at": note.reviewed_or_created,
            "pinned": note.pinned,
        }
        for note in selection.notes
    ]


def _doc_payload(
    warning: dict | None,
    *,
    library_id: str,
    query: str,
    selection: NotesSelection,
    body_key: str,
    body: Any,
    rest: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the response in the documented key order.

    `version_mismatch` → `library_id` → `query` → `notes_warning` → `notes`
    → `snippets`/`sections` → the rest.
    """
    payload: dict[str, Any] = {}
    if warning:
        payload["version_mismatch"] = warning
    payload["library_id"] = library_id
    payload["query"] = query
    if selection.stale_count:
        payload["notes_warning"] = NOTES_WARNING.format(count=selection.stale_count)
    if selection.omitted:
        payload["notes_omitted"] = selection.omitted
    payload["notes"] = _notes_payload(selection)
    payload[body_key] = body
    payload.update(rest)
    return payload


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
            "Everything runs offline on the user's machine."
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
            if config.get_bool("index.auto_refresh"):
                refresh_if_stale(conn, libraryId, chunker)
            selection = select_notes(
                conn,
                libraryId,
                query,
                budget_tokens=config.get_int("notes.max_tokens"),
                pinned_budget_tokens=config.get_int("notes.pinned_max_tokens"),
            )
            warning = mismatch_payload(conn, libraryId, project_path=projectPath)
            if mode == "outline":
                tree = library_outline(conn, libraryId, section=section)
                return _doc_payload(
                    warning,
                    library_id=libraryId,
                    query=query,
                    selection=selection,
                    body_key="sections",
                    body=[node.to_dict() for node in tree],
                    rest={
                        "mode": "outline",
                        "section": section,
                        "total_tokens": sum(node.token_est for node in tree),
                    },
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
            if not result.snippets and not result.total_matches:
                return _doc_payload(
                    warning,
                    library_id=libraryId,
                    query=query,
                    selection=selection,
                    body_key="snippets",
                    body=[],
                    rest={
                        "page": result.page,
                        "total_tokens": 0,
                        "total_matches": 0,
                        "has_more": False,
                        "message": (
                            "no fragments found; the indexed documentation does not "
                            "cover this query. Do not answer from memory: rephrase with "
                            "the library's keywords or state that the documentation does "
                            "not cover it. Top-level sections of this library:"
                        ),
                        "sections": [
                            {
                                "section": hint.section,
                                "chunk_count": hint.chunk_count,
                                "token_est": hint.token_est,
                            }
                            for hint in top_level_sections(conn, libraryId)
                        ],
                    },
                )
            rest: dict[str, Any] = {
                "page": result.page,
                "total_tokens": result.total_tokens,
                "total_matches": result.total_matches,
                "has_more": result.has_more,
            }
            if not result.snippets:
                rest["message"] = (
                    f"no fragments on page {result.page}; total_matches: {result.total_matches}"
                )
            return _doc_payload(
                warning,
                library_id=libraryId,
                query=query,
                selection=selection,
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
                rest=rest,
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
