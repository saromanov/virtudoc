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


def test_update_command(home, docs_dir, capsys):
    main(["add", str(docs_dir), "--name", "demo"])
    capsys.readouterr()
    assert main(["update"]) == 0
    out = capsys.readouterr().out
    assert "skipped: 3" in out


def test_search_unknown_library(home, capsys):
    assert main(["search", "x", "--name", "nope"]) == 1
    assert "not found" in capsys.readouterr().err


def test_serve_help(home, capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["serve", "--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "--http" in out


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
