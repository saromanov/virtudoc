import pytest

from virtudoc.cli import main


def test_add_list_search_remove_flow(home, docs_dir, capsys):
    assert main(["add", str(docs_dir), "--name", "demo", "--version", "1.0"]) == 0
    out = capsys.readouterr().out
    assert "local/demo" in out

    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "local/demo" in out
    assert "guide.md" not in out

    assert main(["search", "middleware"]) == 0
    out = capsys.readouterr().out
    assert "API → Middleware" in out
    assert "fragments:" in out

    assert main(["remove", "demo", "--yes"]) == 0
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "no libraries registered" in out


def test_add_duplicate_errors(home, docs_dir, capsys):
    assert main(["add", str(docs_dir), "--name", "demo"]) == 0
    capsys.readouterr()
    assert main(["add", str(docs_dir), "--name", "demo"]) == 1
    err = capsys.readouterr().err
    assert "already exists" in err


def test_add_reports_lock_error(home, tmp_path, capsys, monkeypatch):
    import virtudoc.cli as cli_module
    from virtudoc.indexer.indexer import IndexLockError

    def locked(*args, **kwargs):
        raise IndexLockError("failed to acquire the index lock")

    monkeypatch.setattr(cli_module, "add_and_index_library", locked)
    docs = tmp_path / "lockdocs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\nТекст.\n", encoding="utf-8")
    assert main(["add", str(docs), "--name", "locklib"]) == 1
    assert "error: failed to acquire the index lock" in capsys.readouterr().err


def test_import_reports_lock_error(home, docs_dir, tmp_path, capsys, monkeypatch):
    import virtudoc.cli as cli_module
    from virtudoc.indexer.indexer import IndexLockError

    def locked(*args, **kwargs):
        raise IndexLockError("locked")

    monkeypatch.setattr(cli_module, "add_and_index_library", locked)
    setup = tmp_path / "setup.toml"
    setup.write_text(f'[[libraries]]\nname = "demo"\npath = "{docs_dir}"\n', encoding="utf-8")
    assert main(["import", str(setup)]) == 1
    captured = capsys.readouterr()
    assert "failed: 1" in captured.out
    assert "error: demo: locked" in captured.err


def test_update_command(home, docs_dir, capsys):
    main(["add", str(docs_dir), "--name", "demo"])
    capsys.readouterr()
    assert main(["update"]) == 0
    out = capsys.readouterr().out
    assert "skipped: 3" in out


def test_reindex_full_rebuild(home, docs_dir, capsys):
    main(["add", str(docs_dir), "--name", "demo"])
    capsys.readouterr()
    (home / "config.toml").write_text(
        "[chunk]\ntarget_tokens = 40\nmax_tokens = 80\n", encoding="utf-8"
    )
    assert main(["reindex", "demo"]) == 0
    out = capsys.readouterr().out
    assert "local/demo" in out
    assert "reindexed: 3" in out

    from virtudoc.config import Config
    from virtudoc.db import open_db

    with open_db(Config().db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert rows > 3  # smaller chunks -> more chunks than before


def test_reindex_unknown_library(home, capsys):
    assert main(["reindex", "nope"]) == 1
    assert "not found" in capsys.readouterr().err


def test_search_unknown_library(home, capsys):
    assert main(["search", "x", "--name", "nope"]) == 1
    assert "not found" in capsys.readouterr().err


def test_search_pagination_metadata(home, tmp_path, capsys):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("[search]\nmax_results = 2\n", encoding="utf-8")
    docs = tmp_path / "manydocs"
    docs.mkdir()
    for index in range(7):
        (docs / f"doc{index}.md").write_text(
            f"# Раздел {index}\n\npaginationmarker уникальный текст {index}.\n",
            encoding="utf-8",
        )
    assert main(["add", str(docs), "--name", "many"]) == 0
    capsys.readouterr()

    assert main(["search", "paginationmarker", "--page", "1"]) == 0
    first = capsys.readouterr().out
    assert "fragments: 2, total_matches: 7, has_more: true" in first
    assert "next page: 2" in first

    assert main(["search", "paginationmarker", "--page", "4"]) == 0
    last = capsys.readouterr().out
    assert "fragments: 1, total_matches: 7, has_more: false" in last

    assert main(["search", "paginationmarker", "--page", "5"]) == 0
    beyond = capsys.readouterr().out
    assert "no fragments on page 5" in beyond
    assert "total_matches: 7" in beyond


def test_serve_help(home, capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["serve", "--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "--http" in out


def _parse_mcp_config(capsys) -> dict:
    import json

    assert main(["mcp-config"]) == 0
    captured = capsys.readouterr()
    return json.loads(captured.out)


def test_mcp_config_prints_ready_snippet(home, capsys):
    config = _parse_mcp_config(capsys)
    entry = config["mcpServers"]["virtudoc"]
    assert entry["args"][-1] == "serve"
    # `command` must stay a single executable path: MCP clients do not split it.
    assert " " not in entry["command"]
    if entry["args"] != ["serve"]:
        assert entry["args"] == ["-m", "virtudoc", "serve"]
    assert entry["env"]["VIRTUDOC_HOME"] == str(home)


def test_mcp_config_without_data_dir_override(home, capsys, monkeypatch):
    monkeypatch.delenv("VIRTUDOC_HOME", raising=False)
    config = _parse_mcp_config(capsys)
    entry = config["mcpServers"]["virtudoc"]
    assert "env" not in entry


def test_remove_requires_confirmation(home, docs_dir, capsys, monkeypatch):
    main(["add", str(docs_dir), "--name", "demo"])
    capsys.readouterr()
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert main(["remove", "demo"]) == 0
    assert "cancelled" in capsys.readouterr().out


def test_note_lifecycle_in_search_output(home, docs_dir, capsys):
    assert main(["add", str(docs_dir), "--name", "demo"]) == 0
    capsys.readouterr()
    assert main(["note", "add", "demo", "--text", "Use middleware for auth checks."]) == 0
    capsys.readouterr()
    assert main(["search", "middleware auth", "--name", "demo"]) == 0
    out = capsys.readouterr().out
    assert "=== project notes ===" in out
    assert "Use middleware for auth checks." in out
    assert main(["note", "remove", "demo", "--id", "1"]) == 0
    capsys.readouterr()
    assert main(["search", "middleware auth", "--name", "demo"]) == 0
    out = capsys.readouterr().out
    assert "=== project notes ===" not in out


def test_note_list_and_unknown_library(home, docs_dir, capsys):
    main(["add", str(docs_dir), "--name", "demo"])
    capsys.readouterr()
    assert main(["note", "add", "nope", "--text", "x"]) == 1
    assert "not found" in capsys.readouterr().err
    assert main(["note", "remove", "demo", "--id", "99"]) == 1


def test_link_unlink_and_doctor_flow(home, docs_dir, tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==2.0.0\n", encoding="utf-8")
    assert main(["add", str(docs_dir), "--name", "demo", "--version", "1.0"]) == 0
    capsys.readouterr()
    assert main(["link", "demo", "--project", str(project)]) == 0
    out = capsys.readouterr().out
    assert "project version: 2.0.0" in out
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "version drift" in out
    assert main(["unlink", "demo"]) == 0
    capsys.readouterr()
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "no problems found" in out


def test_doctor_reports_missing_source(home, docs_dir, capsys):
    main(["add", str(docs_dir), "--name", "demo"])
    capsys.readouterr()
    import shutil

    shutil.rmtree(docs_dir)
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "source directory missing" in out


def test_export_import_roundtrip(home, docs_dir, tmp_path, capsys):
    assert main(["add", str(docs_dir), "--name", "demo", "--version", "1.0"]) == 0
    capsys.readouterr()
    setup = tmp_path / "setup.toml"
    assert main(["export", str(setup)]) == 0
    text = setup.read_text(encoding="utf-8")
    assert 'name = "demo"' in text
    assert 'version = "1.0"' in text
    assert str(docs_dir) in text

    assert main(["remove", "demo", "--yes"]) == 0
    capsys.readouterr()
    assert main(["import", str(setup)]) == 0
    out = capsys.readouterr().out
    assert "imported: 1, failed: 0" in out
    assert main(["list"]) == 0
    assert "local/demo" in capsys.readouterr().out


def test_import_is_idempotent(home, docs_dir, tmp_path, capsys):
    main(["add", str(docs_dir), "--name", "demo"])
    capsys.readouterr()
    from virtudoc.config import Config
    from virtudoc.db import open_db
    from virtudoc.notes import add_note, list_notes

    with open_db(Config().db_path) as conn:
        add_note(conn, "local/demo", "Preserve across imports")
    setup = tmp_path / "setup.toml"
    main(["export", str(setup)])
    capsys.readouterr()
    assert main(["import", str(setup)]) == 0
    capsys.readouterr()
    assert main(["import", str(setup)]) == 0
    assert "imported: 1, failed: 0" in capsys.readouterr().out
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert out.count("local/demo") == 1
    with open_db(Config().db_path) as conn:
        assert [note.text for note in list_notes(conn, "local/demo")] == ["Preserve across imports"]


def test_import_missing_source_fails_cleanly(home, tmp_path, capsys):
    setup = tmp_path / "setup.toml"
    setup.write_text(
        '[[libraries]]\nname = "ghost"\npath = "/nonexistent/path"\n', encoding="utf-8"
    )
    assert main(["import", str(setup)]) == 1
    captured = capsys.readouterr()
    assert "failed: 1" in captured.out
    assert "source directory not found" in captured.err


def test_import_default_path(home, docs_dir, capsys):
    main(["add", str(docs_dir), "--name", "demo"])
    capsys.readouterr()
    assert main(["export"]) == 0
    capsys.readouterr()
    assert main(["remove", "demo", "--yes"]) == 0
    capsys.readouterr()
    assert main(["import"]) == 0
    assert "imported: 1" in capsys.readouterr().out


def test_export_escapes_special_characters(home, tmp_path, capsys):
    weird = tmp_path / 'weird "dir"'
    weird.mkdir()
    (weird / "a.md").write_text("# A\n\nТекст.\n", encoding="utf-8")
    main(["add", str(weird), "--name", "weird"])
    capsys.readouterr()
    setup = tmp_path / "setup.toml"
    assert main(["export", str(setup)]) == 0
    assert main(["remove", "weird", "--yes"]) == 0
    capsys.readouterr()
    assert main(["import", str(setup)]) == 0
    assert "imported: 1, failed: 0" in capsys.readouterr().out


def test_update_single_library(home, docs_dir, capsys):
    main(["add", str(docs_dir), "--name", "demo"])
    capsys.readouterr()
    assert main(["update", "--name", "demo"]) == 0
    out = capsys.readouterr().out
    assert "local/demo:" in out
    assert "skipped: 3" in out


def test_update_unknown_library(home, capsys):
    assert main(["update", "--name", "nope"]) == 1
    assert "not found" in capsys.readouterr().err


def test_update_without_libraries(home, capsys):
    assert main(["update"]) == 0
    assert "no libraries registered" in capsys.readouterr().out


def test_update_prints_chunk_settings_note(home, docs_dir, capsys):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("[chunk]\ntarget_tokens = 40\n", encoding="utf-8")
    main(["add", str(docs_dir), "--name", "demo"])
    capsys.readouterr()
    assert main(["update"]) == 0
    out = capsys.readouterr().out
    assert "changed chunk.* settings only affect new indexing" in out
    assert "virtudoc reindex <name>" in out


def test_add_reports_skipped_large_files(home, tmp_path, capsys, monkeypatch):
    from virtudoc.indexer import walker

    monkeypatch.setattr(walker, "MAX_FILE_SIZE", 10)
    docs = tmp_path / "skipped"
    docs.mkdir()
    (docs / "small.md").write_text("# A\n", encoding="utf-8")
    (docs / "big.md").write_text("# Big\n\n" + "x" * 100, encoding="utf-8")
    assert main(["add", str(docs), "--name", "skips"]) == 0
    err = capsys.readouterr().err
    assert "skipped (over 5 MB): big.md" in err


def test_search_outline(home, docs_dir, capsys):
    main(["add", str(docs_dir), "--name", "demo"])
    capsys.readouterr()
    assert main(["search", "middleware", "--name", "demo", "--outline"]) == 0
    out = capsys.readouterr().out
    assert "outline of local/demo" in out
    assert "- Руководство" in out
    assert "API" in out


def test_search_outline_requires_name(home, capsys):
    assert main(["search", "middleware", "--outline"]) == 1
    assert "--outline requires --name" in capsys.readouterr().err


def test_search_without_matches(home, docs_dir, capsys):
    main(["add", str(docs_dir), "--name", "demo"])
    capsys.readouterr()
    assert main(["search", "абракадабра-которой-нет"]) == 0
    assert "nothing found" in capsys.readouterr().out


def test_note_list_output(home, docs_dir, capsys):
    main(["add", str(docs_dir), "--name", "demo"])
    capsys.readouterr()
    assert main(["note", "list", "demo"]) == 0
    assert "no notes for local/demo" in capsys.readouterr().out
    main(["note", "add", "demo", "--text", "Convention note."])
    capsys.readouterr()
    assert main(["note", "list", "demo"]) == 0
    out = capsys.readouterr().out
    assert "#1\t" in out
    assert "Convention note." in out


def test_note_commands_unknown_library(home, capsys):
    assert main(["note", "list", "nope"]) == 1
    assert "not found" in capsys.readouterr().err
    assert main(["note", "remove", "nope", "--id", "1"]) == 1
    assert "not found" in capsys.readouterr().err


def test_link_and_unlink_errors(home, docs_dir, tmp_path, capsys):
    main(["add", str(docs_dir), "--name", "demo"])
    capsys.readouterr()
    assert main(["link", "nope", "--project", str(tmp_path)]) == 1
    assert "not found" in capsys.readouterr().err

    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("other==1.0\n", encoding="utf-8")
    assert main(["link", "demo", "--project", str(project)]) == 1
    assert "not found in requirements.txt" in capsys.readouterr().err

    assert main(["unlink", "demo"]) == 0
    assert "has no project link" in capsys.readouterr().out
    assert main(["unlink", "nope"]) == 1
    assert "not found" in capsys.readouterr().err


def test_remove_unknown_library(home, capsys):
    assert main(["remove", "nope", "--yes"]) == 1
    assert "not found" in capsys.readouterr().err


def test_import_missing_file(home, tmp_path, capsys):
    assert main(["import", str(tmp_path / "absent.toml")]) == 1
    assert "setup file not found" in capsys.readouterr().err


def test_import_malformed_toml(home, tmp_path, capsys):
    setup = tmp_path / "broken.toml"
    setup.write_text("[[libraries]\n", encoding="utf-8")
    assert main(["import", str(setup)]) == 1
    assert "cannot parse" in capsys.readouterr().err


def test_import_empty_file(home, tmp_path, capsys):
    setup = tmp_path / "empty.toml"
    setup.write_text("libraries = []\n", encoding="utf-8")
    assert main(["import", str(setup)]) == 0
    assert "no libraries in" in capsys.readouterr().out


def test_import_entry_without_path(home, tmp_path, capsys):
    setup = tmp_path / "partial.toml"
    setup.write_text('[[libraries]]\nname = "demo"\n', encoding="utf-8")
    assert main(["import", str(setup)]) == 1
    captured = capsys.readouterr()
    assert "entry without name/path" in captured.err
    assert "failed: 1" in captured.out


def test_serve_delegates_to_mcp_server(home, monkeypatch):
    import virtudoc.mcp_server as mcp_server_module

    captured = {}

    def fake_run_serve(config, *, http):
        captured["http"] = http
        return 7

    monkeypatch.setattr(mcp_server_module, "run_serve", fake_run_serve)
    assert main(["serve"]) == 7
    assert captured["http"] is False


def test_server_command_prefers_executable_named_virtudoc(home, tmp_path, monkeypatch):
    import sys

    from virtudoc.cli import _server_command

    executable = tmp_path / "virtudoc"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(executable), "list"])
    assert _server_command() == (str(executable), [])


def test_server_command_falls_back_to_which(home, tmp_path, monkeypatch):
    import sys

    from virtudoc import cli as cli_module

    monkeypatch.setattr(sys, "argv", ["python", "list"])
    monkeypatch.setattr(cli_module.shutil, "which", lambda name: str(tmp_path / "virtudoc"))
    assert cli_module._server_command() == (str((tmp_path / "virtudoc").resolve()), [])


def test_module_entrypoint_runs_cli(home, monkeypatch):
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", ["virtudoc", "list"])
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("virtudoc", run_name="__main__")
    assert exc_info.value.code == 0


def test_init_reports_link_errors(home, docs_dir, tmp_path, capsys, monkeypatch):
    main(["add", str(docs_dir), "--name", "demo"])
    capsys.readouterr()
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==1.0\n", encoding="utf-8")
    monkeypatch.setattr("virtudoc.cli.link_matches", lambda conn, scan: ([], ["local/demo: boom"]))
    assert main(["init", str(project), "--link"]) == 1
    assert "error: local/demo: boom" in capsys.readouterr().err


def test_cli_module_entrypoint(home, monkeypatch):
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", ["virtudoc", "list"])
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("virtudoc.cli", run_name="__main__")
    assert exc_info.value.code == 0


def test_cli_closes_every_database_connection(home, monkeypatch):
    import sqlite3

    import virtudoc.cli as cli_module

    real_open_db = cli_module.open_db
    opened = []

    def tracking_open_db(path):
        connection = real_open_db(path)
        opened.append(connection)
        return connection

    monkeypatch.setattr(cli_module, "open_db", tracking_open_db)
    assert main(["list"]) == 0
    assert opened
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


def test_import_rejects_non_array_libraries(home, tmp_path, capsys):
    setup = tmp_path / "bad-shape.toml"
    setup.write_text('libraries = "oops"\n', encoding="utf-8")
    assert main(["import", str(setup)]) == 1
    assert "'libraries' must be an array of tables" in capsys.readouterr().err


def test_import_rejects_non_table_entries(home, tmp_path, capsys):
    setup = tmp_path / "bad-entries.toml"
    setup.write_text('libraries = ["oops"]\n', encoding="utf-8")
    assert main(["import", str(setup)]) == 1
    captured = capsys.readouterr()
    assert "entry is not a table" in captured.err
    assert "failed: 1" in captured.out
