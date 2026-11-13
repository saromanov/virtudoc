from __future__ import annotations

import sqlite3
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__
from .config import Config
from .db import open_db
from .indexer.chunker import Chunker
from .indexer.indexer import index_library
from .logging_setup import get_logger
from .notes import relevant_notes
from .search.engine import resolve_libraries, search, top_level_sections

logger = get_logger("mcp")

MAX_TOKENS_HARD_LIMIT = 20000

RESOLVE_DESCRIPTION = (
    "Find the documentation library (id like 'local/<name>') for a given library "
    "name and the user's question. Always call this first to get a libraryId, "
    "then call get-library-docs with that id. If no library is found, register "
    "the documentation source with the 'virtudoc add <path> --name <name>' CLI command."
)

GET_DOCS_DESCRIPTION = (
    "Get documentation fragments of a library for the agent's question or task. "
    "Use a focused query with keywords (library terminology works better than "
    "full sentences). Output is assembled under a token budget; use page=2 to "
    "continue. Fragments contain Markdown text, the heading path and the source "
    "file path. If nothing is found, the response contains top-level sections "
    "of the library to help rephrase the query."
)


def build_server(config: Config) -> MCPServer:
    server = MCPServer(
        name="virtudoc",
        version=__version__,
        instructions=(
            "Local documentation provider. Workflow: 1) resolve-library-id to "
            "find the library id, 2) get-library-docs with a keyword query. "
            "Everything runs offline on the user's machine."
        ),
    )

    def connect() -> sqlite3.Connection:
        return open_db(config.db_path)

    @server.tool(name="resolve-library-id", description=RESOLVE_DESCRIPTION)
    def resolve_library_id(libraryName: str, query: str) -> dict[str, Any]:
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
        if mode != "fragments":
            raise ToolError(f"unsupported mode: {mode!r}; supported: 'fragments'")
        budget = min(max(tokens, 100), MAX_TOKENS_HARD_LIMIT)
        page = max(page, 1)
        with connect() as conn:
            lib = conn.execute("SELECT id FROM libraries WHERE id = ?", (libraryId,)).fetchone()
            if lib is None:
                raise ToolError(
                    f"library not found: {libraryId!r}; "
                    "call resolve-library-id to list available libraries"
                )
            result = search(
                conn,
                query,
                library_id=libraryId,
                token_budget=budget,
                max_results=config.get_int("search.max_results"),
                page=page,
                language=language,
            )
            notes = [
                {"id": note.id, "text": note.text, "created_at": note.created_at}
                for note in relevant_notes(
                    conn,
                    libraryId,
                    query,
                    budget_tokens=config.get_int("notes.max_tokens"),
                )
            ]
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
            return {
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

    return server


def reindex_before_serve(config: Config) -> None:
    chunker = Chunker(
        target_tokens=config.get_int("chunk.target_tokens"),
        max_tokens=config.get_int("chunk.max_tokens"),
        overlap_tokens=config.get_int("chunk.overlap_tokens"),
    )
    with open_db(config.db_path) as conn:
        rows = conn.execute("SELECT id FROM libraries ORDER BY id").fetchall()
        for row in rows:
            try:
                stats = index_library(conn, row["id"], chunker)
            except Exception as exc:  # serve must start regardless
                logger.error("reindex failed for %s: %s", row["id"], exc)
            else:
                logger.info("reindexed %s on serve start: %s", row["id"], stats.summary())


def run_serve(config: Config, *, http: bool | int = False) -> int:
    reindex_before_serve(config)
    server = build_server(config)
    if http:
        port = config.get_int("server.http_port") if http is True else int(http)
        host = config.get_str("server.http_host")
        logger.info("starting streamable HTTP transport on %s:%s", host, port)
        server.run(transport="streamable-http", host=host, port=port)
    else:
        logger.info("starting stdio transport")
        anyio.run(server.run_stdio_async)
    return 0
