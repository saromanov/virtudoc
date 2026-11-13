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
