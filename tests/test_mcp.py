from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
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
    # Minimal environment: a test must not borrow the developer's variables
    # (VIRTUDOC_HTTP_TOKEN, VIRTUDOC_HOME of another checkout, and so on).
    env = {"VIRTUDOC_HOME": str(home), "PATH": os.environ.get("PATH", "")}
    if os.environ.get("SYSTEMROOT"):
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "virtudoc", "serve"],
        env=env,
        cwd=str(REPO_ROOT),
    )


def _run_cli(home: Path, *args: str) -> None:
    # Minimal environment: a test must not borrow the developer's variables.
    env = {"VIRTUDOC_HOME": str(home), "PATH": os.environ.get("PATH", "")}
    if os.environ.get("SYSTEMROOT"):
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    result = subprocess.run(
        [sys.executable, "-m", "virtudoc", *args],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _is_error(result) -> bool:
    return bool(getattr(result, "isError", None) or getattr(result, "is_error", None))


def _payload(result) -> dict:
    assert result.content, "tool returned no content"
    return json.loads(result.content[0].text)


def _connection_closed(conn) -> bool:
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


@pytest.mark.anyio
async def test_mcp_tools_close_connections(home_index, monkeypatch):
    import virtudoc.mcp_server as mcp_server_module

    real_open_db = mcp_server_module.open_db
    opened: list = []

    def tracking_open_db(path):
        connection = real_open_db(path)
        opened.append(connection)
        return connection

    monkeypatch.setattr(mcp_server_module, "open_db", tracking_open_db)
    server = mcp_server_module.build_server(Config())
    await server.call_tool("find-library", {"libraryName": "demo", "query": "middleware"})
    assert opened, "tool should open a connection"
    assert all(_connection_closed(conn) for conn in opened)


@pytest.mark.anyio
async def test_mcp_handshake_lists_tools(home_index):
    async with stdio_client(_server_params(home_index)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == {"find-library", "get-library-docs"}


@pytest.mark.anyio
async def test_mcp_find_library(home_index):
    async with stdio_client(_server_params(home_index)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "find-library",
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
                "find-library", {"libraryName": "nonexistent", "query": "x"}
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
            assert payload["documentation_version"] == "1.0"
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
async def test_mcp_get_library_docs_uses_configured_default_tokens(home_index):
    from virtudoc.mcp_server import build_server

    config = Config()
    config.values["search.default_tokens"] = 150
    payload = await build_server(config).call_tool(
        "get-library-docs",
        {"libraryId": "local/demo", "query": "middleware конвейер"},
    )
    data = json.loads(payload.content[0].text)
    assert data["snippets"]
    assert data["total_tokens"] <= 150


@pytest.mark.anyio
async def test_mcp_pagination_metadata(home, tmp_path):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("[search]\nmax_results = 2\n", encoding="utf-8")
    docs = tmp_path / "manydocs"
    docs.mkdir()
    for index in range(7):
        (docs / f"doc{index}.md").write_text(
            f"# Раздел {index}\n\npaginationmarker уникальный текст {index}.\n",
            encoding="utf-8",
        )
    with open_db(Config().db_path) as conn:
        lib_id = add_library(conn, docs, "many")
        index_library(conn, lib_id, Chunker())

    async with stdio_client(_server_params(home)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            seen: set[str] = set()
            for page in range(1, 20):
                result = await session.call_tool(
                    "get-library-docs",
                    {
                        "libraryId": "local/many",
                        "query": "paginationmarker",
                        "page": page,
                    },
                )
                assert not _is_error(result)
                payload = _payload(result)
                assert payload["page"] == page
                assert payload["total_matches"] == 7
                texts = {snippet["text"] for snippet in payload["snippets"]}
                assert seen.isdisjoint(texts)
                seen.update(texts)
                if not payload["has_more"]:
                    break
            assert len(seen) == 7

            beyond = await session.call_tool(
                "get-library-docs",
                {
                    "libraryId": "local/many",
                    "query": "paginationmarker",
                    "page": 5,
                },
            )
            payload = _payload(beyond)
            assert payload["snippets"] == []
            assert payload["total_matches"] == 7
            assert payload["has_more"] is False
            assert "no fragments on page 5" in payload["message"]


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


def test_serve_startup_does_not_index_libraries(home, docs_dir):
    import virtudoc.mcp_server as mcp_server_module

    with open_db(Config().db_path) as conn:
        add_library(conn, docs_dir, "demo")
    mcp_server_module.prepare_serve(Config())
    with open_db(Config().db_path) as conn:
        documents = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert documents == 0, "startup must not walk and index sources"
    assert mcp_server_module.build_server(Config()) is not None


def test_prepare_serve_closes_its_connection(home, monkeypatch):
    import virtudoc.mcp_server as mcp_server_module

    real_open_db = mcp_server_module.open_db
    opened: list = []

    def tracking_open_db(path):
        connection = real_open_db(path)
        opened.append(connection)
        return connection

    monkeypatch.setattr(mcp_server_module, "open_db", tracking_open_db)
    mcp_server_module.prepare_serve(Config())
    assert opened, "prepare_serve should open a connection"
    assert all(_connection_closed(conn) for conn in opened)


@pytest.mark.anyio
async def test_mcp_indexes_library_only_on_first_request(home, docs_dir):
    with open_db(Config().db_path) as conn:
        add_library(conn, docs_dir, "demo")
    async with stdio_client(_server_params(home)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "get-library-docs",
                {"libraryId": "local/demo", "query": "middleware"},
            )
            payload = _payload(result)
            assert payload["snippets"]


@pytest.mark.anyio
async def test_mcp_refreshes_changed_library_on_request(home, docs_dir):
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
async def test_mcp_version_mismatch_warning(home, docs_dir, tmp_path):
    with open_db(Config().db_path) as conn:
        lib_id = add_library(conn, docs_dir, "demo", version="1.0")
        index_library(conn, lib_id, Chunker())
        project = tmp_path / "project"
        project.mkdir()
        (project / "requirements.txt").write_text("demo==2.0.0\n", encoding="utf-8")
        from virtudoc.versions import link_library

        link_library(conn, lib_id, project)
    async with stdio_client(_server_params(home)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "get-library-docs",
                {"libraryId": "local/demo", "query": "middleware"},
            )
            payload = _payload(result)
            warning = payload["version_mismatch"]
            assert warning["documentation_version"] == "1.0"
            assert warning["project_version"] == "2.0.0"
            assert payload["snippets"]


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


@pytest.mark.anyio
async def test_mcp_empty_search_keeps_version_mismatch(home, docs_dir, tmp_path):
    with open_db(Config().db_path) as conn:
        lib_id = add_library(conn, docs_dir, "demo", version="1.0")
        index_library(conn, lib_id, Chunker())
        project = tmp_path / "project"
        project.mkdir()
        (project / "requirements.txt").write_text("demo==2.0.0\n", encoding="utf-8")
        from virtudoc.versions import link_library

        link_library(conn, lib_id, project)
    async with stdio_client(_server_params(home)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "get-library-docs",
                {"libraryId": "local/demo", "query": "zizigaura абракадабра"},
            )
            payload = _payload(result)
            assert payload["snippets"] == []
            assert payload["version_mismatch"]["project_version"] == "2.0.0"


@pytest.mark.anyio
async def test_mcp_refreshes_stale_library_on_tool_call(home, docs_dir):
    import virtudoc.mcp_server as mcp_server_module

    with open_db(Config().db_path) as conn:
        lib_id = add_library(conn, docs_dir, "demo")
        index_library(conn, lib_id, Chunker())
    server = mcp_server_module.build_server(Config())
    before = await server.call_tool(
        "get-library-docs", {"libraryId": "local/demo", "query": "поправка свежести"}
    )
    assert _payload(before)["snippets"] == []

    (docs_dir / "fresh-late.md").write_text(
        "# Свежесть\n\nПоправка свежести для автообновления индекса.\n",
        encoding="utf-8",
    )
    after = await server.call_tool(
        "get-library-docs", {"libraryId": "local/demo", "query": "поправка свежести"}
    )
    payload = _payload(after)
    assert payload["snippets"]
    assert payload["snippets"][0]["source"] == "fresh-late.md"


@pytest.mark.anyio
async def test_index_forgets_renamed_symbols_and_marks_query_uncovered(home, tmp_path):
    """A renamed or deleted method must disappear from the served fragments.

    The agent keeps one MCP session while the human edits the docs and runs
    the CLI. An old symbol left in the index would look like a valid answer,
    so the same session must see no fragments for it afterwards.

    This test checks the index and the server response. It does not check how
    a model behaves after the docs change; that is the job of
    `evals/stale_symbols` (§5).
    """
    docs = tmp_path / "authdocs"
    docs.mkdir()
    doc = docs / "auth.md"
    original = (
        "# Authentication\n\n"
        "## Token issuance\n\n"
        "Call `issueLegacyToken` to create a token for the legacy flow.\n\n"
        "## Session validation\n\n"
        "Call `validateSession` before serving a request.\n"
    )
    renamed = original.replace("issueLegacyToken", "issueModernToken")
    without_token_section = (
        "# Authentication\n\n"
        "## Session validation\n\n"
        "Call `validateSession` before serving a request.\n"
    )
    doc.write_text(original, encoding="utf-8")
    _run_cli(home, "add", str(docs), "--name", "auth")

    async with stdio_client(_server_params(home)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            baseline = _payload(
                await session.call_tool(
                    "get-library-docs",
                    {"libraryId": "local/auth", "query": "issueLegacyToken"},
                )
            )
            assert baseline["snippets"]
            assert "issueLegacyToken" in baseline["snippets"][0]["text"]

            doc.write_text(renamed, encoding="utf-8")
            _run_cli(home, "update", "--name", "auth")

            stale = _payload(
                await session.call_tool(
                    "get-library-docs",
                    {"libraryId": "local/auth", "query": "issueLegacyToken"},
                )
            )
            assert stale["snippets"] == []
            assert stale["total_matches"] == 0
            assert "Do not answer from memory" in stale["message"]
            assert "Authentication" in {section["section"] for section in stale["sections"]}

            fresh = _payload(
                await session.call_tool(
                    "get-library-docs",
                    {"libraryId": "local/auth", "query": "issueModernToken"},
                )
            )
            assert fresh["snippets"]
            assert "issueModernToken" in fresh["snippets"][0]["text"]

            doc.write_text(without_token_section, encoding="utf-8")
            _run_cli(home, "reindex", "auth")

            deleted = _payload(
                await session.call_tool(
                    "get-library-docs",
                    {"libraryId": "local/auth", "query": "issueModernToken"},
                )
            )
            assert deleted["snippets"] == []
            assert deleted["total_matches"] == 0
            assert "Do not answer from memory" in deleted["message"]

            survivor = _payload(
                await session.call_tool(
                    "get-library-docs",
                    {"libraryId": "local/auth", "query": "validateSession"},
                )
            )
            assert survivor["snippets"]
            assert survivor["snippets"][0]["source"] == "auth.md"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("authorization", "expected_status"),
    [
        (None, 401),
        (b"Bearer wrong", 401),
        (b"Bearer secret", 204),
    ],
)
async def test_http_bearer_authentication(authorization, expected_status):
    from virtudoc.mcp_server import BearerAuthMiddleware

    called = False

    async def downstream(scope, receive, send):
        nonlocal called
        called = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        messages.append(message)

    headers = [] if authorization is None else [(b"authorization", authorization)]
    app = BearerAuthMiddleware(downstream, "secret")
    await app({"type": "http", "headers": headers}, receive, send)

    assert messages[0]["status"] == expected_status
    assert called is (expected_status == 204)
    if expected_status == 401:
        assert (b"www-authenticate", b"Bearer") in messages[0]["headers"]


def test_non_loopback_http_requires_token(home, monkeypatch, capsys):
    import virtudoc.mcp_server as mcp_server_module

    monkeypatch.delenv("VIRTUDOC_HTTP_TOKEN", raising=False)
    config = Config()
    config.values["server.http_host"] = "0.0.0.0"

    assert mcp_server_module.run_serve(config, http=True) == 1
    assert "non-loopback HTTP requires" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("host", "token", "expected_token"),
    [
        ("127.0.0.1", None, None),
        ("0.0.0.0", "secret", "secret"),
    ],
)
def test_http_authentication_configuration(home, monkeypatch, host, token, expected_token):
    import virtudoc.mcp_server as mcp_server_module

    monkeypatch.delenv("VIRTUDOC_HTTP_TOKEN", raising=False)
    monkeypatch.setattr(mcp_server_module, "prepare_serve", lambda config: None)
    monkeypatch.setattr(mcp_server_module, "build_server", lambda config: object())
    captured = {}

    def fake_run_http(server, *, host, port, token):
        captured.update(host=host, port=port, token=token)

    monkeypatch.setattr(mcp_server_module, "_run_http", fake_run_http)
    config = Config()
    config.values["server.http_host"] = host
    config.values["server.http_token"] = token

    assert mcp_server_module.run_serve(config, http=9000) == 0
    assert captured == {"host": host, "port": 9000, "token": expected_token}


@pytest.mark.anyio
async def test_mcp_find_library_without_matches_in_process(home_index):
    import virtudoc.mcp_server as mcp_server_module

    server = mcp_server_module.build_server(Config())
    result = await server.call_tool("find-library", {"libraryName": "zizigaura", "query": "zzz"})
    payload = _payload(result)
    assert payload["libraries"] == []
    assert "virtudoc add" in payload["message"]


@pytest.mark.anyio
async def test_mcp_rejects_unsupported_mode(home_index):
    from mcp.server.mcpserver.exceptions import ToolError

    import virtudoc.mcp_server as mcp_server_module

    server = mcp_server_module.build_server(Config())
    with pytest.raises(ToolError, match="unsupported mode"):
        await server.call_tool(
            "get-library-docs",
            {"libraryId": "local/demo", "query": "middleware", "mode": "raw"},
        )


@pytest.mark.anyio
async def test_mcp_unknown_library_in_process(home_index):
    from mcp.server.mcpserver.exceptions import ToolError

    import virtudoc.mcp_server as mcp_server_module

    server = mcp_server_module.build_server(Config())
    with pytest.raises(ToolError, match="library not found"):
        await server.call_tool("get-library-docs", {"libraryId": "local/ghost", "query": "x"})


@pytest.mark.anyio
async def test_mcp_outline_mode_in_process(home_index):
    import virtudoc.mcp_server as mcp_server_module

    server = mcp_server_module.build_server(Config())
    result = await server.call_tool(
        "get-library-docs",
        {"libraryId": "local/demo", "query": "middleware", "mode": "outline"},
    )
    payload = _payload(result)
    assert payload["mode"] == "outline"
    assert payload["sections"]
    assert payload["total_tokens"] > 0
    assert "version_mismatch" not in payload


@pytest.mark.anyio
async def test_mcp_outline_includes_version_mismatch(home, docs_dir, tmp_path):
    import virtudoc.mcp_server as mcp_server_module
    from virtudoc.versions import link_library

    with open_db(Config().db_path) as conn:
        lib_id = add_library(conn, docs_dir, "demo", version="1.0")
        index_library(conn, lib_id, Chunker())
        project = tmp_path / "project"
        project.mkdir()
        (project / "requirements.txt").write_text("demo==2.0.0\n", encoding="utf-8")
        link_library(conn, lib_id, project)

    server = mcp_server_module.build_server(Config())
    result = await server.call_tool(
        "get-library-docs",
        {"libraryId": "local/demo", "query": "middleware", "mode": "outline"},
    )
    payload = _payload(result)
    assert payload["version_mismatch"]["project_version"] == "2.0.0"


@pytest.mark.anyio
async def test_mcp_page_beyond_results_reports_message(home_index):
    import virtudoc.mcp_server as mcp_server_module

    server = mcp_server_module.build_server(Config())
    result = await server.call_tool(
        "get-library-docs",
        {"libraryId": "local/demo", "query": "middleware", "page": 9},
    )
    payload = _payload(result)
    assert payload["snippets"] == []
    assert "no fragments on page 9" in payload["message"]


def test_prepare_serve_skips_sources_without_git(home, tmp_path, monkeypatch):
    import virtudoc.mcp_server as mcp_server_module

    plain = tmp_path / "plaindocs"
    plain.mkdir()
    (plain / "index.md").write_text("# Plain\n\nText.\n", encoding="utf-8")
    called = []
    monkeypatch.setattr(
        mcp_server_module, "ensure_pinned_checkout", lambda source: called.append(source)
    )
    with open_db(Config().db_path) as conn:
        add_library(conn, plain, "plain")
    mcp_server_module.prepare_serve(Config())
    assert called == []


@pytest.mark.anyio
async def test_bearer_middleware_passes_non_http_scopes():
    from virtudoc.mcp_server import BearerAuthMiddleware

    seen = []

    async def downstream(scope, receive, send):
        seen.append(scope["type"])

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        pass

    app = BearerAuthMiddleware(downstream, "secret")
    await app({"type": "websocket", "headers": []}, receive, send)
    assert seen == ["websocket"]


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost", True),
        ("LOCALHOST", True),
        ("127.0.0.2", True),
        ("::1", True),
        ("0.0.0.0", False),
        ("example.com", False),
    ],
)
def test_is_loopback_host(host, expected):
    from virtudoc.mcp_server import _is_loopback_host

    assert _is_loopback_host(host) is expected


def _fake_uvicorn(monkeypatch):
    import types

    captured = {}

    async def serve(self):
        captured["served"] = True

    module = types.ModuleType("uvicorn")

    def config(app, host, port, log_level):
        captured.update(app=app, host=host, port=port, log_level=log_level)
        return object()

    module.Config = config
    module.Server = lambda uvicorn_config: types.SimpleNamespace(serve=lambda: serve(None))
    monkeypatch.setitem(sys.modules, "uvicorn", module)
    return captured


@pytest.mark.parametrize("token", [None, "secret"])
def test_run_http_builds_app_and_wraps_auth(token, monkeypatch):
    import types

    import virtudoc.mcp_server as mcp_server_module
    from virtudoc.mcp_server import BearerAuthMiddleware

    raw_app = types.SimpleNamespace(name="app")

    async def run_stdio_async():
        raise AssertionError("stdio must not start in HTTP mode")

    server = types.SimpleNamespace(
        streamable_http_app=lambda host: raw_app,
        settings=types.SimpleNamespace(log_level="INFO"),
        run_stdio_async=run_stdio_async,
    )
    captured = _fake_uvicorn(monkeypatch)

    mcp_server_module._run_http(server, host="127.0.0.1", port=1234, token=token)

    assert captured["served"] is True
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 1234
    assert captured["log_level"] == "info"
    if token is None:
        assert captured["app"] is raw_app
    else:
        assert isinstance(captured["app"], BearerAuthMiddleware)


def test_run_serve_stdio_transport(home, monkeypatch):
    import types

    import virtudoc.mcp_server as mcp_server_module

    started = {}

    async def run_stdio_async():
        started["stdio"] = True

    fake_server = types.SimpleNamespace(run_stdio_async=run_stdio_async)
    monkeypatch.setattr(mcp_server_module, "prepare_serve", lambda config: None)
    monkeypatch.setattr(mcp_server_module, "build_server", lambda config: fake_server)

    assert mcp_server_module.run_serve(Config()) == 0
    assert started["stdio"] is True


@pytest.mark.anyio
async def test_get_library_docs_reports_stale_notes_first(home_index):
    from virtudoc.notes import add_note

    with open_db(Config().db_path) as conn:
        library = conn.execute("SELECT id, source_path FROM libraries").fetchone()
        add_note(conn, library["id"], "Use the retry helper.", paths=["api.md"])

    (Path(library["source_path"]) / "api.md").write_text(
        "# API Reference\n\n## Retry\n\nRetry moved to core.\n"
    )
    _run_cli(home_index, "update")

    async with stdio_client(_server_params(home_index)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "get-library-docs",
                {"libraryId": library["id"], "query": "retry helper"},
            )
    payload = _payload(result)
    keys = list(payload)
    assert "notes_warning" in payload
    assert keys.index("notes_warning") < keys.index("notes") < keys.index("snippets")
    note = payload["notes"][0]
    assert note["status"] == "stale"
    assert note["stale_reason"] == "anchor_changed"
    assert note["stale_details"] == ["api.md"]
    assert "changed_sources" not in note


@pytest.mark.anyio
async def test_get_library_docs_omits_notes_instead_of_truncating(home_index):
    from virtudoc.notes import add_note

    with open_db(Config().db_path) as conn:
        library = conn.execute("SELECT id FROM libraries").fetchone()
        add_note(
            conn,
            library["id"],
            "Middleware convention: " + "detail " * 2000,
            no_anchor=True,
        )

    async with stdio_client(_server_params(home_index)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "get-library-docs",
                {"libraryId": library["id"], "query": "middleware"},
            )
    payload = _payload(result)
    assert payload["notes"] == []
    assert payload["notes_omitted"] == 1


@pytest.mark.anyio
async def test_get_library_docs_reports_missing_identifiers_and_mentions(home_index):
    from virtudoc.notes import add_note

    with open_db(Config().db_path) as conn:
        library = conn.execute("SELECT id FROM libraries").fetchone()
        add_note(conn, library["id"], "Prefer retryWithJitter for flaky calls.", no_anchor=True)

    async with stdio_client(_server_params(home_index)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "get-library-docs",
                {"libraryId": library["id"], "query": "retryWithJitter auth"},
            )
    payload = _payload(result)
    assert payload["missing_identifiers"] == ["retryWithJitter"]
    note = payload["notes"][0]
    assert note["status"] == "unknown"
    assert note["mentions_missing"] == ["retryWithJitter"]
    keys = list(payload)
    assert keys.index("missing_identifiers") < keys.index("notes")
    assert keys.index("notes") < keys.index("snippets")


@pytest.mark.anyio
async def test_note_status_is_computed_after_refresh(home_index):
    from virtudoc.notes import add_note

    with open_db(Config().db_path) as conn:
        library = conn.execute("SELECT id, source_path FROM libraries").fetchone()
        add_note(conn, library["id"], "Use the retry helper.", paths=["api.md"])

    # Change the source after indexing: get-library-docs refreshes the index
    # first, so the status must already see the new content hash.
    (Path(library["source_path"]) / "api.md").write_text(
        "# API Reference\n\n## Retry\n\nChanged after indexing.\n"
    )

    async with stdio_client(_server_params(home_index)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "get-library-docs",
                {"libraryId": library["id"], "query": "retry helper"},
            )
    payload = _payload(result)
    assert payload["notes"][0]["status"] == "stale"
    assert payload["notes"][0]["stale_reason"] == "anchor_changed"


LEGACY_DOC = """# API Reference

## Legacy auth

legacyCreateToken validates credentials.

## Modern auth

modernCreateToken replaces the legacy flow.
"""

NEIGHBOR_DOC = """# Neighbor

## Tokens

neighborToken handles the create token legacy flow.
"""


def _apply_docs_change(docs: Path, change: str) -> Path | None:
    api = docs / "api.md"
    if change == "rename_symbol":
        api.write_text(api.read_text().replace("legacyCreateToken", "legacyRenamedToken"))
        return api
    if change == "delete_section":
        api.write_text(
            "# API Reference\n\n## Modern auth\n\nmodernCreateToken replaces the legacy flow.\n"
        )
        return api
    if change == "delete_file":
        api.unlink()
        return None
    if change == "rename_file":
        renamed = docs / "api.md.bak"
        api.rename(renamed)
        return renamed
    # Renames are not tracked: moving the file to a path the walker ignores
    # makes the index see a deletion of the old document.
    renamed = docs / "api.md.bak"
    api.rename(renamed)
    return renamed


async def _inprocess_docs(server, library_id: str, query: str) -> dict:
    result = await server.call_tool("get-library-docs", {"libraryId": library_id, "query": query})
    return json.loads(result.content[0].text)


@pytest.mark.anyio
@pytest.mark.parametrize("refresh", ["update", "reindex", "auto"])
@pytest.mark.parametrize(
    "change", ["rename_symbol", "delete_section", "delete_file", "rename_file"]
)
async def test_changed_docs_leave_no_stale_symbol(home, tmp_path, refresh, change):
    from virtudoc.mcp_server import build_server

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "api.md").write_text(LEGACY_DOC)
    (docs / "neighbor.md").write_text(NEIGHBOR_DOC)

    _run_cli(home, "add", str(docs), "--name", "demo")
    _run_cli(
        home,
        "note",
        "add",
        "demo",
        "--text",
        "Use legacyCreateToken to validate credentials.",
        "--path",
        "api.md",
    )

    changed_path = _apply_docs_change(docs, change)
    if refresh == "update":
        _run_cli(home, "update")
    elif refresh == "reindex":
        _run_cli(home, "reindex", "demo")
    elif changed_path is not None:
        # Make the auto-refresh heuristic see the change regardless of the
        # filesystem timestamp resolution.
        stamp = time.time() + 2
        os.utime(changed_path, (stamp, stamp))

    server = build_server(Config())

    old = await _inprocess_docs(server, "local/demo", "legacyCreateToken")
    assert old["coverage"] == "none"
    assert "legacyCreateToken" in old["missing_identifiers"]
    assert old["notes"][0]["status"] == "stale"
    assert old["notes"][0]["stale_reason"] == "symbol_removed"

    phrase = await _inprocess_docs(
        server, "local/demo", "legacyCreateToken create token legacy flow"
    )
    assert phrase["coverage"] == "partial"

    neighbor = await _inprocess_docs(server, "local/demo", "neighborToken")
    assert neighbor["snippets"]

    if change == "rename_file":
        with open_db(Config().db_path) as conn:
            assert (
                conn.execute("SELECT COUNT(*) FROM documents WHERE rel_path = 'api.md'").fetchone()[
                    0
                ]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM chunks WHERE library_id = 'local/demo' "
                    "AND text LIKE '%legacyCreateToken%'"
                ).fetchone()[0]
                == 0
            )


@pytest.mark.anyio
async def test_response_always_reports_coverage_fields(home_index):
    from virtudoc.mcp_server import build_server

    server = build_server(Config())
    matched = await _inprocess_docs(server, "local/demo", "middleware конвейер")
    assert matched["coverage"] == "matched"
    assert matched["missing_identifiers"] == []
    assert matched["unreturned_identifiers"] == []

    uncovered = await _inprocess_docs(server, "local/demo", "issueMissingToken")
    assert uncovered["coverage"] == "none"
    assert uncovered["missing_identifiers"] == ["issueMissingToken"]
    assert uncovered["unreturned_identifiers"] == []

    keys = list(uncovered)
    assert keys.index("coverage") < keys.index("missing_identifiers")
    assert keys.index("missing_identifiers") < keys.index("unreturned_identifiers")
    assert keys.index("unreturned_identifiers") < keys.index("message")
    assert keys.index("message") < keys.index("notes")
    assert keys.index("notes") < keys.index("snippets")
    assert keys.index("snippets") < keys.index("total_tokens")


@pytest.mark.anyio
async def test_partial_coverage_message_names_missing_identifiers(home, tmp_path):
    from virtudoc.mcp_server import build_server

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "flow.md").write_text(
        "# Flow\n\n## Tokens\n\ncreate token legacy flow with the modern helper.\n"
    )
    _run_cli(home, "add", str(docs), "--name", "flow")

    payload = await _inprocess_docs(
        build_server(Config()), "local/flow", "issueLegacyToken create token legacy flow"
    )
    assert payload["coverage"] == "partial"
    assert payload["missing_identifiers"] == ["issueLegacyToken"]
    assert payload["snippets"]
    assert "issueLegacyToken" in payload["message"]
    assert "Do not use them from memory" in payload["message"]


@pytest.mark.anyio
async def test_outline_reports_coverage_and_empty_unreturned(home_index):
    from virtudoc.mcp_server import build_server

    payload = await build_server(Config()).call_tool(
        "get-library-docs",
        {
            "libraryId": "local/demo",
            "query": "middleware",
            "mode": "outline",
        },
    )
    data = json.loads(payload.content[0].text)
    assert data["coverage"] == "matched"
    assert data["missing_identifiers"] == []
    assert data["unreturned_identifiers"] == []


@pytest.mark.anyio
async def test_note_text_is_byte_for_byte_in_mcp_response(home_index):
    from virtudoc.notes import add_note

    text = "Middleware rule:\n  keep  two spaces\tand `validateSession`."
    with open_db(Config().db_path) as conn:
        library = conn.execute("SELECT id FROM libraries").fetchone()
        add_note(conn, library["id"], text, no_anchor=True)

    async with stdio_client(_server_params(home_index)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "get-library-docs",
                {"libraryId": library["id"], "query": "middleware rule"},
            )
    payload = _payload(result)
    assert payload["notes"][0]["text"] == text


def test_server_params_do_not_inherit_http_token(home, monkeypatch):
    monkeypatch.setenv("VIRTUDOC_HTTP_TOKEN", "secret-from-developer-shell")
    assert "VIRTUDOC_HTTP_TOKEN" not in _server_params(home).env
