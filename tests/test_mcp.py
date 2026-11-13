from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from virtudoc.config import Config
from virtudoc.db import open_db
from virtudoc.indexer.chunker import Chunker
from virtudoc.indexer.indexer import add_library, index_library

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def home_index(home, docs_dir):
    with open_db(Config().db_path) as conn:
        lib_id = add_library(conn, docs_dir, "demo", version="1.0")
        index_library(conn, lib_id, Chunker())
    return home


def _server_params(home) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "virtudoc", "serve"],
        env={**os.environ, "VIRTUDOC_HOME": str(home)},
        cwd=str(REPO_ROOT),
    )


def _is_error(result) -> bool:
    return bool(getattr(result, "isError", None) or getattr(result, "is_error", None))


def _payload(result) -> dict:
    assert result.content, "tool returned no content"
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_mcp_handshake_lists_both_tools(home_index):
    async with stdio_client(_server_params(home_index)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == {"resolve-library-id", "get-library-docs"}


@pytest.mark.anyio
async def test_mcp_resolve_library_id(home_index):
    async with stdio_client(_server_params(home_index)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "resolve-library-id",
                {"libraryName": "demo", "query": "middleware обработка"},
            )
            assert not _is_error(result)
            payload = _payload(result)
            assert payload["libraries"][0]["id"] == "local/demo"
            assert payload["libraries"][0]["chunk_count"] > 0


@pytest.mark.anyio
async def test_mcp_resolve_unknown_library_returns_hint(home_index):
    async with stdio_client(_server_params(home_index)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "resolve-library-id", {"libraryName": "nonexistent", "query": "x"}
            )
            payload = _payload(result)
            assert payload["libraries"] == []
            assert "virtudoc add" in payload["message"]


@pytest.mark.anyio
async def test_mcp_get_library_docs(home_index):
    async with stdio_client(_server_params(home_index)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "get-library-docs",
                {"libraryId": "local/demo", "query": "middleware конвейер"},
            )
            assert not _is_error(result)
            payload = _payload(result)
            assert payload["library_id"] == "local/demo"
            assert payload["snippets"], "expected fragments"
            top = payload["snippets"][0]
            assert top["source"] == "guide.md"
            assert top["heading_path"] == "Руководство → API → Middleware"
            assert payload["total_tokens"] > 0
            assert payload["total_tokens"] <= 5000


@pytest.mark.anyio
async def test_mcp_get_library_docs_budget(home_index):
    async with stdio_client(_server_params(home_index)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "get-library-docs",
                {"libraryId": "local/demo", "query": "middleware", "tokens": 200},
            )
            payload = _payload(result)
            assert payload["total_tokens"] <= 220


@pytest.mark.anyio
async def test_mcp_unknown_library_is_structured_error(home_index):
    async with stdio_client(_server_params(home_index)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "get-library-docs", {"libraryId": "local/nope", "query": "x"}
            )
            assert _is_error(result)
            assert "library not found" in result.content[0].text


@pytest.mark.anyio
async def test_mcp_reindex_on_start_serves_fresh_content(home, docs_dir):
    with open_db(Config().db_path) as conn:
        lib_id = add_library(conn, docs_dir, "demo")
        index_library(conn, lib_id, Chunker())
    (docs_dir / "fresh.md").write_text(
        "# Свежий раздел\n\nСовершенно новый контент для проверки старта.\n",
        encoding="utf-8",
    )
    async with stdio_client(_server_params(home)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "get-library-docs",
                {"libraryId": "local/demo", "query": "новый контент проверки"},
            )
            payload = _payload(result)
            assert payload["snippets"]
            assert payload["snippets"][0]["source"] == "fresh.md"


@pytest.mark.anyio
async def test_mcp_get_library_docs_includes_relevant_notes(home, docs_dir):
    with open_db(Config().db_path) as conn:
        lib_id = add_library(conn, docs_dir, "demo")
        index_library(conn, lib_id, Chunker())
        from virtudoc.notes import add_note

        add_note(conn, lib_id, "Team convention: use the middleware pipeline for auth.")
    async with stdio_client(_server_params(home)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "get-library-docs",
                {"libraryId": "local/demo", "query": "middleware auth checks"},
            )
            payload = _payload(result)
            assert payload["notes"], "relevant note expected in output"
            assert "middleware" in payload["notes"][0]["text"]
            unrelated = await session.call_tool(
                "get-library-docs",
                {"libraryId": "local/demo", "query": "quantum chromodynamics"},
            )
            assert _payload(unrelated)["notes"] == []


@pytest.mark.anyio
async def test_mcp_outline_mode_and_section_filter(home_index):
    async with stdio_client(_server_params(home_index)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "get-library-docs",
                {"libraryId": "local/demo", "query": "middleware", "mode": "outline"},
            )
            payload = _payload(result)
            assert payload["mode"] == "outline"
            roots = {node["section"]: node for node in payload["sections"]}
            guide = roots["Руководство"]
            api = next(n for n in guide["children"] if n["section"] == "API")
            assert api["chunk_count"] >= 1
            assert api["token_est"] > 0

            filtered = await session.call_tool(
                "get-library-docs",
                {
                    "libraryId": "local/demo",
                    "query": "исключения",
                    "section": "Руководство → API → Ошибки",
                },
            )
            fpayload = _payload(filtered)
            assert fpayload["snippets"]
            assert all(
                s["heading_path"] == "Руководство → API → Ошибки"
                or s["heading_path"].startswith("Руководство → API → Ошибки →")
                for s in fpayload["snippets"]
            )


@pytest.mark.anyio
async def test_mcp_empty_search_returns_section_hints(home_index):
    async with stdio_client(_server_params(home_index)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "get-library-docs",
                {"libraryId": "local/demo", "query": "zizigaura абракадабра"},
            )
            assert not _is_error(result)
            payload = _payload(result)
            assert payload["snippets"] == []
            sections = {s["section"] for s in payload["sections"]}
            assert "Руководство" in sections
