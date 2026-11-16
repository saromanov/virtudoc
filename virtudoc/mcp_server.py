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
from .indexer.indexer import index_library, refresh_if_stale
from .logging_setup import get_logger
from .notes import relevant_notes
from .search.engine import library_outline, resolve_libraries, search, top_level_sections
from .versions import mismatch_payload

logger = get_logger("mcp")

MAX_TOKENS_HARD_LIMIT = 20000

RESOLVE_DESCRIPTION = (
    "Find the documentation library (id like 'local/<name>') for a given library "
    "name and the user's question. Always call this first to get a libraryId, "
    "then call get-library-docs with that id. If no library is found, register "
    "the documentation source with the 'virtudoc add <path> --name <name>' CLI command."
)

GET_DOCS_DESCRIPTION = (
    "Get documentation of a library for the agent's question or task. "
    "Use a focused query with keywords (library terminology works better than "
    "full sentences). Output is assembled under a token budget; use page=2 to "
    "continue. Fragments contain Markdown text, the heading path and the source "
    "file path. If nothing is found, the response contains top-level sections "
    "of the library to help rephrase the query. Use mode='outline' to get the "
    "section tree with token estimates instead of fragments, and 'section' to "
    "restrict the output to one branch (e.g. 'API -> Middleware')."
)


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
    def find_library(libraryName: str, query: str) -> dict[str, Any]:
        with connect() as conn:
            matches = resolve_libraries(conn, libraryName, query)
        if not matches:
            return {
                "libraries": [],
                "message": (
                    "no libraries found; register a documentation source with "
                    "the 'virtudoc add <path> --name <name>' CLI command"
                ),
            }
        return {
            "libraries": [
                {
                    "id": match.id,
                    "name": match.name,
                    "description": match.description,
                    "chunk_count": match.chunk_count,
                    "updated_at": match.updated_at,
                }
                for match in matches
            ]
        }

    @server.tool(name="get-library-docs", description=GET_DOCS_DESCRIPTION)
    def get_library_docs(
        libraryId: str,
        query: str,
        tokens: int = 5000,
        language: str | None = None,
        page: int = 1,
        mode: str = "fragments",
        section: str | None = None,
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
            notes = [
                {"id": note.id, "text": note.text, "created_at": note.created_at}
                for note in relevant_notes(
                    conn,
                    libraryId,
                    query,
                    budget_tokens=config.get_int("notes.max_tokens"),
                )
            ]
            if mode == "outline":
                tree = library_outline(conn, libraryId, section=section)
                payload = {
                    "library_id": libraryId,
                    "query": query,
                    "mode": "outline",
                    "section": section,
                    "sections": [node.to_dict() for node in tree],
                    "notes": notes,
                    "total_tokens": sum(node.token_est for node in tree),
                }
                warning = mismatch_payload(conn, libraryId)
                return {"version_mismatch": warning, **payload} if warning else payload
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
            if not result.snippets:
                return {
                    "library_id": libraryId,
                    "query": query,
                    "page": page,
                    "snippets": [],
                    "notes": notes,
                    "total_tokens": 0,
                    "message": (
                        "no fragments found; try a keyword query. "
                        "Top-level sections of this library:"
                    ),
                    "sections": [
                        {
                            "section": hint.section,
                            "chunk_count": hint.chunk_count,
                            "token_est": hint.token_est,
                        }
                        for hint in top_level_sections(conn, libraryId)
                    ],
                }
            payload = {
                "library_id": libraryId,
                "query": query,
                "page": result.page,
                "snippets": [
                    {
                        "heading_path": snippet.heading_path,
                        "source": snippet.source,
                        "text": snippet.text,
                        "score": snippet.score,
                    }
                    for snippet in result.snippets
                ],
                "notes": notes,
                "total_tokens": result.total_tokens,
            }
            warning = mismatch_payload(conn, libraryId)
            return {"version_mismatch": warning, **payload} if warning else payload

    return server


def reindex_before_serve(config: Config) -> None:
    chunker = Chunker(
        target_tokens=config.get_int("chunk.target_tokens"),
        max_tokens=config.get_int("chunk.max_tokens"),
        overlap_tokens=config.get_int("chunk.overlap_tokens"),
    )
    with open_db(config.db_path) as conn:
        rows = conn.execute("SELECT id, source_path FROM libraries ORDER BY id").fetchall()
        for row in rows:
            source = Path(row["source_path"])
            if (source / ".git").is_dir():
                pinned = ensure_pinned_checkout(source)
                if pinned:
                    logger.info(
                        "fetch cache for %s pinned to branch '%s' (reset locally, offline)",
                        row["id"],
                        pinned,
                    )
            try:
                stats = index_library(conn, row["id"], chunker)
            except Exception as exc:  # serve must start regardless
                logger.error("reindex failed for %s: %s", row["id"], exc)
            else:
                logger.info("reindexed %s on serve start: %s", row["id"], stats.summary())


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
    reindex_before_serve(config)
    server = build_server(config)
    if http:
        port = config.get_int("server.http_port") if http is True else int(http)
        logger.info("starting streamable HTTP transport on %s:%s", host, port)
        _run_http(server, host=host, port=port, token=token)
    else:
        logger.info("starting stdio transport")
        anyio.run(server.run_stdio_async)
    return 0
