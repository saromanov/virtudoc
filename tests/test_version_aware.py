from __future__ import annotations

import asyncio
import json

import pytest

from virtudoc.cli import main
from virtudoc.config import Config
from virtudoc.db import open_db
from virtudoc.indexer.chunker import Chunker
from virtudoc.indexer.indexer import (
    LibraryExistsError,
    add_and_index_library,
    add_library,
    get_library,
    variant_library_id,
)
from virtudoc.mcp_server import build_server
from virtudoc.search.engine import resolve_libraries
from virtudoc.versions import (
    get_link,
    library_version_choices,
    mismatch_payload,
    resolve_project_version,
)


def _call_tool(name: str, arguments: dict) -> dict:
    server = build_server(Config())

    async def call() -> dict:
        result = await server.call_tool(name, arguments)
        return json.loads(result.content[0].text)

    return asyncio.run(call())


def test_first_release_keeps_canonical_id_and_variants_are_versioned(conn, docs_dir):
    base_id = add_library(conn, docs_dir, "demo", version="1.0")
    next_id = add_library(conn, docs_dir, "demo", version="2.0")
    branch_id = add_library(conn, docs_dir, "demo", channel="branch")

    assert base_id == "local/demo"
    assert next_id == "local/demo@2.0"
    assert branch_id == "local/demo@branch"
    assert get_library(conn, next_id)["version"] == "2.0"
    assert get_library(conn, branch_id)["channel"] == "branch"


def test_duplicate_version_is_rejected(conn, docs_dir):
    add_library(conn, docs_dir, "demo", version="1.0")
    with pytest.raises(LibraryExistsError):
        add_library(conn, docs_dir, "demo", version="1.0")


def test_branch_never_takes_the_canonical_id(conn, docs_dir):
    assert add_library(conn, docs_dir, "demo", channel="branch") == "local/demo@branch"
    assert add_library(conn, docs_dir, "demo", version="1.0") == "local/demo"


def test_replace_branch_does_not_overwrite_the_canonical_release(conn, docs_dir):
    add_library(conn, docs_dir, "demo", version="1.0")
    branch_id = add_library(conn, docs_dir, "demo", channel="branch", replace=True)

    assert branch_id == "local/demo@branch"
    release = get_library(conn, "local/demo")
    assert release["channel"] == "release"
    assert release["version"] == "1.0"
    assert get_library(conn, branch_id)["channel"] == "branch"


def test_replace_updates_the_existing_version_variant(conn, docs_dir):
    add_library(conn, docs_dir, "demo", version="1.0")
    add_library(conn, docs_dir, "demo", version="2.0")
    lib_id = add_library(conn, docs_dir, "demo", version="2.0", replace=True)

    assert lib_id == "local/demo@2.0"
    assert get_library(conn, "local/demo")["version"] == "1.0"
    assert get_library(conn, "local/demo@2.0")["version"] == "2.0"


def test_variant_library_id_sanitizes_the_suffix():
    assert variant_library_id("demo", version="2.0") == "local/demo@2.0"
    assert variant_library_id("demo", channel="branch") == "local/demo@branch"
    assert variant_library_id("demo", version="2.0", channel="branch") == ("local/demo@2.0-branch")


def test_family_name_is_ambiguous_without_a_canonical_release(conn, docs_dir):
    from virtudoc.indexer.indexer import LibraryNotFoundError

    add_library(conn, docs_dir, "demo", channel="branch")
    add_library(conn, docs_dir, "demo", version="2.0", channel="branch")
    with pytest.raises(LibraryNotFoundError, match="ambiguous"):
        get_library(conn, "demo")
    assert get_library(conn, "local/demo@2.0-branch")["version"] == "2.0"


def test_resolve_project_version_prefers_lockfiles(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["demo>=2.0"]\n', encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "demo"\nversion = "2.3.1"\n', encoding="utf-8"
    )
    resolved = resolve_project_version(tmp_path, "demo")
    assert resolved is not None
    assert resolved.version == "2.3.1"
    assert resolved.manifest_path.name == "uv.lock"


def test_resolve_project_version_returns_none_without_match(tmp_path):
    (tmp_path / "requirements.txt").write_text("other==1.0\n", encoding="utf-8")
    assert resolve_project_version(tmp_path, "demo") is None
    assert resolve_project_version(tmp_path / "missing", "demo") is None


def test_resolve_libraries_prefers_the_project_version(conn, docs_dir):
    base_id = add_and_index_library(conn, docs_dir, "demo", chunker=Chunker(), version="1.0")[0]
    add_and_index_library(conn, docs_dir, "demo", chunker=Chunker(), version="2.0")
    branch_id = add_and_index_library(conn, docs_dir, "demo", chunker=Chunker(), channel="branch")[
        0
    ]

    ranked = resolve_libraries(conn, "demo", "middleware", resolved_version="2.0")
    assert ranked[0].id == "local/demo@2.0"
    assert ranked[0].version_status == "match"
    assert ranked[-1].id == branch_id
    assert next(match for match in ranked if match.id == base_id).version_status == "mismatch"


def test_branch_does_not_outrank_a_release_without_a_resolved_version(conn, docs_dir):
    add_library(conn, docs_dir, "demo", version="1.0")
    branch_id = add_library(conn, docs_dir, "demo", channel="branch")
    ranked = resolve_libraries(conn, "demo", "")
    assert ranked[0].id == "local/demo"
    assert ranked[-1].id == branch_id


def test_mismatch_payload_resolves_from_the_project_manifests(conn, docs_dir, tmp_path):
    lib_id = add_and_index_library(conn, docs_dir, "demo", chunker=Chunker(), version="1.0")[0]
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==2.0.0\n", encoding="utf-8")

    payload = mismatch_payload(conn, lib_id, project_path=project)
    assert payload is not None
    assert payload["project_version"] == "2.0.0"
    assert payload["version_not_indexed"] is True
    assert payload["available_versions"][0]["id"] == "local/demo"
    assert "ask the user" in payload["message"]


def test_mismatch_payload_suggests_the_matching_indexed_version(conn, docs_dir, tmp_path):
    lib_id = add_and_index_library(conn, docs_dir, "demo", chunker=Chunker(), version="1.0")[0]
    add_and_index_library(conn, docs_dir, "demo", chunker=Chunker(), version="2.0")
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==2.0.0\n", encoding="utf-8")

    payload = mismatch_payload(conn, lib_id, project_path=project)
    assert payload is not None
    assert payload["suggested_library_id"] == "local/demo@2.0"
    assert payload["version_not_indexed"] is False


def test_mismatch_payload_is_silent_when_the_resolved_version_matches(conn, docs_dir, tmp_path):
    lib_id = add_and_index_library(conn, docs_dir, "demo", chunker=Chunker(), version="2.0")[0]
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==2.0.0\n", encoding="utf-8")
    assert mismatch_payload(conn, lib_id, project_path=project) is None


def test_mcp_find_library_returns_the_matching_version(home, docs_dir, tmp_path):
    with open_db(Config().db_path) as conn:
        add_and_index_library(conn, docs_dir, "demo", chunker=Chunker(), version="1.0")
        add_and_index_library(conn, docs_dir, "demo", chunker=Chunker(), version="2.0")
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==2.0.0\n", encoding="utf-8")

    payload = _call_tool(
        "find-library",
        {"libraryName": "demo", "query": "middleware", "projectPath": str(project)},
    )
    assert payload["libraries"][0]["id"] == "local/demo@2.0"
    assert payload["libraries"][0]["version_status"] == "match"
    assert payload["project"]["dependency_version"] == "2.0.0"
    assert {choice["id"] for choice in payload["available_versions"]} == {
        "local/demo",
        "local/demo@2.0",
    }


def test_mcp_find_library_reports_unindexed_project_version(home, docs_dir, tmp_path):
    with open_db(Config().db_path) as conn:
        add_and_index_library(conn, docs_dir, "demo", chunker=Chunker(), version="1.0")
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==2.0.0\n", encoding="utf-8")

    payload = _call_tool(
        "find-library",
        {"libraryName": "demo", "query": "middleware", "projectPath": str(project)},
    )
    assert payload["version_not_indexed"] is True
    assert payload["available_versions"][0]["version"] == "1.0"
    assert "Ask the user" in payload["message"]


def test_mcp_get_docs_warns_from_the_project_manifest(home, docs_dir, tmp_path):
    with open_db(Config().db_path) as conn:
        add_and_index_library(conn, docs_dir, "demo", chunker=Chunker(), version="1.0")
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==2.0.0\n", encoding="utf-8")

    payload = _call_tool(
        "get-library-docs",
        {"libraryId": "local/demo", "query": "middleware", "projectPath": str(project)},
    )
    warning = payload["version_mismatch"]
    assert warning["project_version"] == "2.0.0"
    assert warning["version_not_indexed"] is True


def test_cli_add_opt_in_branch(home, docs_dir, capsys):
    assert main(["add", str(docs_dir), "--name", "demo", "--version", "1.0"]) == 0
    capsys.readouterr()
    assert main(["add", str(docs_dir), "--name", "demo", "--branch"]) == 0
    out = capsys.readouterr().out
    assert "local/demo@branch" in out
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "branch" in out


def test_cli_add_pins_the_version_from_the_lockfile(home, docs_dir, tmp_path, capsys, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "uv.lock").write_text(
        '[[package]]\nname = "demo"\nversion = "2.3.1"\n', encoding="utf-8"
    )
    monkeypatch.chdir(project)
    assert main(["add", str(docs_dir), "--name", "demo"]) == 0
    out = capsys.readouterr().out
    assert "version pinned from uv.lock: 2.3.1" in out

    with open_db(Config().db_path) as conn:
        assert get_library(conn, "local/demo")["version"] == "2.3.1"


def test_cli_add_explicit_version_wins_over_the_lockfile(
    home, docs_dir, tmp_path, capsys, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "uv.lock").write_text(
        '[[package]]\nname = "demo"\nversion = "2.3.1"\n', encoding="utf-8"
    )
    monkeypatch.chdir(project)
    assert main(["add", str(docs_dir), "--name", "demo", "--version", "9.9.9"]) == 0
    out = capsys.readouterr().out
    assert "version pinned" not in out

    with open_db(Config().db_path) as conn:
        assert get_library(conn, "local/demo")["version"] == "9.9.9"


def test_cli_add_without_a_lockfile_stays_unversioned(
    home, docs_dir, tmp_path, capsys, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    assert main(["add", str(docs_dir), "--name", "demo"]) == 0
    out = capsys.readouterr().out
    assert "version pinned" not in out
    assert "hint: pass --agents-hint" in out

    with open_db(Config().db_path) as conn:
        assert get_library(conn, "local/demo")["version"] is None


def test_init_link_records_the_locked_version(home, docs_dir, tmp_path, capsys):
    assert main(["add", str(docs_dir), "--name", "demo", "--version", "2.3.1"]) == 0
    capsys.readouterr()
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\ndependencies = ["demo>=2.0"]\n', encoding="utf-8"
    )
    (project / "uv.lock").write_text(
        '[[package]]\nname = "demo"\nversion = "2.3.1"\n', encoding="utf-8"
    )

    assert main(["init", str(project), "--link"]) == 0
    out = capsys.readouterr().out
    assert "2.3.1" in out
    with open_db(Config().db_path) as conn:
        link = get_link(conn, "local/demo")
    assert link["dep_version"] == "2.3.1"
    assert link["manifest_path"].endswith("uv.lock")


def test_cli_add_agents_hint_creates_and_keeps_one_block(
    home, docs_dir, tmp_path, capsys, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "CLAUDE.md").write_text("# Notes\n", encoding="utf-8")
    monkeypatch.chdir(project)

    assert main(["add", str(docs_dir), "--name", "demo", "--agents-hint"]) == 0
    out = capsys.readouterr().out
    claude = project / "CLAUDE.md"
    assert f"agents hint written to {claude}" in out
    assert "find-library" in claude.read_text(encoding="utf-8")
    assert not (project / "AGENTS.md").exists()

    assert main(["add", str(docs_dir), "--name", "demo", "--agents-hint", "--replace"]) == 0
    out = capsys.readouterr().out
    assert f"agents hint already present in {claude}" in out
    assert claude.read_text(encoding="utf-8").count("find-library") == 1


def test_cli_add_agents_hint_creates_agents_md(home, docs_dir, tmp_path, capsys, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    assert main(["add", str(docs_dir), "--name", "demo", "--agents-hint"]) == 0
    out = capsys.readouterr().out
    agents = project / "AGENTS.md"
    assert f"agents hint written to {agents}" in out
    text = agents.read_text(encoding="utf-8")
    assert "find-library" in text
    assert "get-library-docs" in text


def test_cli_add_agents_hint_appends_to_an_existing_agents_md(
    home, docs_dir, tmp_path, capsys, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("# Rules\n\nBe concise.\n", encoding="utf-8")
    monkeypatch.chdir(project)

    assert main(["add", str(docs_dir), "--name", "demo", "--agents-hint"]) == 0
    text = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith("# Rules\n\nBe concise.\n")
    assert text.count("find-library") == 1


def test_init_picks_the_version_matching_the_project(conn, docs_dir, tmp_path):
    from virtudoc.onboarding import scan_project

    add_library(conn, docs_dir, "demo", version="1.0")
    add_library(conn, docs_dir, "demo", version="2.0")
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==2.0.0\n", encoding="utf-8")

    scan = scan_project(conn, project)
    assert scan.matches[0].library_id == "local/demo@2.0"
    assert scan.matches[0].status == "match"


def test_link_prefers_the_lockfile_version(conn, docs_dir, tmp_path):
    from virtudoc.versions import link_library

    lib_id = add_library(conn, docs_dir, "demo", version="2.3.1")
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\ndependencies = ["demo>=2.0"]\n', encoding="utf-8"
    )
    (project / "uv.lock").write_text(
        '[[package]]\nname = "demo"\nversion = "2.3.1"\n', encoding="utf-8"
    )
    info = link_library(conn, lib_id, project)
    assert info.dep_version == "2.3.1"
    assert info.manifest_path.name == "uv.lock"


def test_library_version_choices_list_release_and_branch(conn, docs_dir):
    add_library(conn, docs_dir, "demo", version="1.0")
    add_library(conn, docs_dir, "demo", version="2.0")
    add_library(conn, docs_dir, "demo", channel="branch")
    choices = library_version_choices(conn, "local/demo@2.0")
    assert [choice["id"] for choice in choices] == [
        "local/demo",
        "local/demo@2.0",
        "local/demo@branch",
    ]
