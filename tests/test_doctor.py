import shutil
import subprocess
from pathlib import Path

import pytest

from virtudoc.cli import main


def _git_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "index.md").write_text("# Docs\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=origin, check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=origin, check=True)
    subprocess.run(["git", "add", "."], cwd=origin, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=origin,
        check=True,
    )
    return origin


def _clone(origin: Path, tmp_path: Path) -> Path:
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    return clone


def _add_and_doctor(path: Path, capsys) -> tuple[int, str]:
    assert main(["add", str(path), "--name", "demo"]) == 0
    capsys.readouterr()
    code = main(["doctor"])
    return code, capsys.readouterr().out


def test_doctor_clean_clone_on_default_branch(home, tmp_path, capsys):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    clone = _clone(_git_origin(tmp_path), tmp_path)
    code, out = _add_and_doctor(clone, capsys)
    assert code == 0
    assert "on branch" not in out
    assert "uncommitted" not in out


def test_doctor_warns_on_non_default_branch(home, tmp_path, capsys):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    clone = _clone(_git_origin(tmp_path), tmp_path)
    subprocess.run(["git", "checkout", "-qb", "feature"], cwd=clone, check=True)
    code, out = _add_and_doctor(clone, capsys)
    assert code == 1
    assert "on branch 'feature'" in out
    assert "default 'main'" in out
    assert "uncommitted" not in out


def test_doctor_warns_on_uncommitted_changes(home, tmp_path, capsys):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    clone = _clone(_git_origin(tmp_path), tmp_path)
    (clone / "wip.md").write_text("# WIP\n", encoding="utf-8")
    code, out = _add_and_doctor(clone, capsys)
    assert code == 1
    assert "uncommitted changes" in out
    assert "on branch" not in out


def test_doctor_ignores_the_fetch_pin_sidecar(home, tmp_path, capsys):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    from virtudoc.fetcher import PIN_FILE

    clone = _clone(_git_origin(tmp_path), tmp_path)
    (clone / PIN_FILE).write_text('{"branch": "main"}', encoding="utf-8")
    code, out = _add_and_doctor(clone, capsys)
    assert code == 0
    assert "uncommitted" not in out


def _doctor_config(tmp_path):
    from virtudoc.config import Config

    cfg = Config()
    cfg.values["db.path"] = str(tmp_path / "index.db")
    return cfg


def test_doctor_reports_missing_database(conn, tmp_path):
    from virtudoc.config import Config
    from virtudoc.doctor import run_doctor

    cfg = Config()
    cfg.values["db.path"] = str(tmp_path / "absent.db")
    result = run_doctor(conn, cfg)
    assert result.problems == 1
    assert any("database file not found" in line for line in result.lines)


def test_doctor_reports_unreadable_meta(conn, tmp_path):
    from virtudoc.doctor import run_doctor

    conn.execute("DROP TABLE meta")
    conn.commit()
    result = run_doctor(conn, _doctor_config(tmp_path))
    assert any("cannot read meta table" in line for line in result.lines)


def test_doctor_reports_no_libraries(conn, tmp_path):
    from virtudoc.doctor import run_doctor

    result = run_doctor(conn, _doctor_config(tmp_path))
    assert any("no libraries registered" in line for line in result.lines)
    assert result.problems == 0


def test_doctor_reports_missing_manifest(conn, docs_dir, tmp_path):
    from virtudoc.doctor import run_doctor
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import add_library, index_library
    from virtudoc.versions import link_library

    lib_id = add_library(conn, docs_dir, "demo", version="1.0")
    index_library(conn, lib_id, Chunker())
    project = tmp_path / "project"
    project.mkdir()
    manifest = project / "requirements.txt"
    manifest.write_text("demo==1.0\n", encoding="utf-8")
    link_library(conn, lib_id, project)
    manifest.unlink()

    result = run_doctor(conn, _doctor_config(tmp_path))
    assert any("manifest missing" in line for line in result.lines)


def test_doctor_reports_dependency_removed_from_manifest(conn, docs_dir, tmp_path):
    from virtudoc.doctor import run_doctor
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import add_library, index_library
    from virtudoc.versions import link_library

    lib_id = add_library(conn, docs_dir, "demo", version="1.0")
    index_library(conn, lib_id, Chunker())
    project = tmp_path / "project"
    project.mkdir()
    manifest = project / "requirements.txt"
    manifest.write_text("demo==1.0\n", encoding="utf-8")
    link_library(conn, lib_id, project)
    manifest.write_text("other==1.0\n", encoding="utf-8")

    result = run_doctor(conn, _doctor_config(tmp_path))
    assert any("dependency not found" in line for line in result.lines)


def test_doctor_reports_matching_versions(conn, docs_dir, tmp_path):
    from virtudoc.doctor import run_doctor
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import add_library, index_library
    from virtudoc.versions import link_library

    lib_id = add_library(conn, docs_dir, "demo", version="1.0")
    index_library(conn, lib_id, Chunker())
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==1.0.0\n", encoding="utf-8")
    link_library(conn, lib_id, project)

    result = run_doctor(conn, _doctor_config(tmp_path))
    assert any("versions match" in line for line in result.lines)
    assert result.problems == 0


def test_doctor_accepts_docs_inside_the_declared_range(conn, docs_dir, tmp_path):
    from virtudoc.doctor import run_doctor
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import add_library, index_library
    from virtudoc.versions import link_library

    lib_id = add_library(conn, docs_dir, "demo", version="2.5")
    index_library(conn, lib_id, Chunker())
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo>=2.0\n", encoding="utf-8")
    link_library(conn, lib_id, project)

    result = run_doctor(conn, _doctor_config(tmp_path))
    assert not any("version drift" in line for line in result.lines)
    assert any("versions match" in line for line in result.lines)
    assert result.problems == 0


def test_doctor_reports_drift_for_an_exact_pin(conn, docs_dir, tmp_path):
    from virtudoc.doctor import run_doctor
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import add_library, index_library
    from virtudoc.versions import link_library

    lib_id = add_library(conn, docs_dir, "demo", version="2.5")
    index_library(conn, lib_id, Chunker())
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==2.0\n", encoding="utf-8")
    link_library(conn, lib_id, project)

    result = run_doctor(conn, _doctor_config(tmp_path))
    assert any("version drift" in line for line in result.lines)


def test_doctor_does_not_certify_unevaluable_constraints(conn, docs_dir, tmp_path):
    from virtudoc.doctor import run_doctor
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import add_library, index_library
    from virtudoc.versions import link_library

    lib_id = add_library(conn, docs_dir, "demo", version="2.5")
    index_library(conn, lib_id, Chunker())
    project = tmp_path / "project"
    project.mkdir()
    (project / "package.json").write_text('{"dependencies": {"demo": "^2.0"}}', encoding="utf-8")
    link_library(conn, lib_id, project)

    result = run_doctor(conn, _doctor_config(tmp_path))
    assert not any("versions match" in line for line in result.lines)
    assert not any("satisfies ^2.0" in line for line in result.lines)
    assert any("versions unknown" in line for line in result.lines)
    assert result.problems == 0


def test_doctor_reports_drift_outside_the_declared_range(conn, docs_dir, tmp_path):
    from virtudoc.doctor import run_doctor
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import add_library, index_library
    from virtudoc.versions import link_library

    lib_id = add_library(conn, docs_dir, "demo", version="1.9")
    index_library(conn, lib_id, Chunker())
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo>=2.0\n", encoding="utf-8")
    link_library(conn, lib_id, project)

    result = run_doctor(conn, _doctor_config(tmp_path))
    assert any("version drift" in line for line in result.lines)


def test_doctor_reports_orphan_chunks(conn, tmp_path):
    from virtudoc.doctor import run_doctor

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO chunks (document_id, library_id, heading_path, ord, text, token_est)"
        " VALUES (999999, 'local/ghost', '', 0, 'orphan', 1)"
    )
    conn.commit()
    result = run_doctor(conn, _doctor_config(tmp_path))
    assert any("1 orphan chunks" in line for line in result.lines)


def test_doctor_reports_integrity_check_failure(conn, tmp_path):
    from virtudoc.doctor import run_doctor

    conn.execute("DROP TABLE chunks_fts")
    conn.commit()
    result = run_doctor(conn, _doctor_config(tmp_path))
    assert any("integrity check failed" in line for line in result.lines)


def test_doctor_warns_on_detached_head(home, tmp_path, capsys):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    clone = _clone(_git_origin(tmp_path), tmp_path)
    subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=clone, check=True)
    code, out = _add_and_doctor(clone, capsys)
    assert code == 1
    assert "detached HEAD state" in out


def test_doctor_warns_on_pinned_branch_mismatch(home, tmp_path, capsys):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    from virtudoc.fetcher import PIN_FILE

    clone = _clone(_git_origin(tmp_path), tmp_path)
    (clone / PIN_FILE).write_text('{"branch": "release"}', encoding="utf-8")
    code, out = _add_and_doctor(clone, capsys)
    assert code == 1
    assert "pinned to branch 'release'" in out
    assert "is on 'main'" in out


def test_doctor_skips_git_checks_without_git(conn, docs_dir, tmp_path, monkeypatch):
    import virtudoc.doctor as doctor_module
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import add_library, index_library

    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    monkeypatch.setattr(doctor_module.shutil, "which", lambda name: None)
    result = doctor_module.run_doctor(conn, _doctor_config(tmp_path))
    assert any("source present" in line for line in result.lines)
    assert not any("git checkout" in line for line in result.lines)


def test_doctor_reports_files_that_failed_indexing(conn, docs_dir, tmp_path, monkeypatch):
    from virtudoc.doctor import run_doctor
    from virtudoc.indexer import indexer as indexer_module
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import add_library, index_library
    from virtudoc.indexer.parsers import ParseError

    lib_id = add_library(conn, docs_dir, "demo")
    monkeypatch.setattr(
        indexer_module,
        "parse_file",
        lambda path, text: (_ for _ in ()).throw(ParseError("broken")),
    )
    index_library(conn, lib_id, Chunker())
    result = run_doctor(conn, _doctor_config(tmp_path))
    assert any("files failed to index" in line for line in result.lines)


def test_doctor_truncates_long_failure_list(conn, docs_dir, tmp_path):
    from virtudoc.doctor import run_doctor
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import add_library, index_library

    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        ("index_failures:local/demo", '["a.md", "b.md", "c.md", "d.md", "e.md"]'),
    )
    conn.commit()
    result = run_doctor(conn, _doctor_config(tmp_path))
    line = next(line for line in result.lines if "files failed to index" in line)
    assert "a.md, b.md, c.md, ... (2 more)" in line


def test_doctor_warns_on_stale_note(conn, docs_dir, tmp_path):
    from virtudoc.doctor import run_doctor
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import add_library, index_library
    from virtudoc.notes import add_note

    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    add_note(
        conn,
        lib_id,
        "Use the retry helper for all outbound calls.",
        paths=["api.md"],
    )
    (docs_dir / "api.md").write_text("# API Reference\n\n## Retry\n\nRetry moved to core.\n")
    index_library(conn, lib_id, Chunker())

    result = run_doctor(conn, _doctor_config(tmp_path))
    line = next(line for line in result.lines if "note #1" in line)
    assert "stale" in line
    assert "anchor_changed" in line
    assert "api.md" in line
    assert result.problems == 0
    assert result.note_warnings == 1


def test_doctor_accepts_fresh_note(conn, docs_dir, tmp_path):
    from virtudoc.doctor import run_doctor
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import add_library, index_library
    from virtudoc.notes import add_note

    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    add_note(conn, lib_id, "Use the retry helper for all outbound calls.")

    result = run_doctor(conn, _doctor_config(tmp_path))
    assert not any("stale" in line for line in result.lines)
    assert result.problems == 0


def test_doctor_git_checks_have_a_timeout(tmp_path, monkeypatch):
    import subprocess

    import virtudoc.doctor as doctor_module

    source = tmp_path / "repo"
    (source / ".git").mkdir(parents=True)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=doctor_module.GIT_TIMEOUT)

    monkeypatch.setattr(doctor_module.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(doctor_module.subprocess, "run", timeout)
    result = doctor_module.CheckResult()
    doctor_module._warn_unstable_source(result, "local/demo", source)
    assert result.problems == 0
    assert result.lines == []


def test_doctor_reports_removed_symbol_with_commands(conn, docs_dir, tmp_path):
    from virtudoc.doctor import run_doctor
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import add_library, index_library
    from virtudoc.notes import add_note

    (docs_dir / "api.md").write_text("# API\n\nvalidateSession() here.\n")
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    add_note(conn, lib_id, "Use validateSession().", paths=["api.md"])

    (docs_dir / "api.md").write_text("# API\n\nNothing left.\n")
    index_library(conn, lib_id, Chunker())

    result = run_doctor(conn, _doctor_config(tmp_path))
    line = next(line for line in result.lines if "note #1" in line)
    assert "symbol_removed: validateSession" in line
    assert any("edit:" in item for item in result.lines)
    assert any("reanchor:" in item for item in result.lines)


def _stale_note_home(home):
    from virtudoc.config import Config
    from virtudoc.db import open_db
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import add_library, index_library
    from virtudoc.notes import add_note

    docs = home.parent / "doctor-docs"
    docs.mkdir()
    (docs / "api.md").write_text("# API\n\n## Retry\n\nRetry lives here.\n")
    with open_db(Config().db_path) as conn:
        lib_id = add_library(conn, docs, "demo")
        index_library(conn, lib_id, Chunker())
        add_note(conn, lib_id, "Use the retry helper.", paths=["api.md"])
        (docs / "api.md").write_text("# API\n\n## Retry\n\nMoved to core.\n")
        index_library(conn, lib_id, Chunker())


def test_doctor_exit_code_ignores_stale_notes_without_strict(home, capsys):
    _stale_note_home(home)
    assert main(["doctor"]) == 0
    assert "is stale" in capsys.readouterr().out
    assert main(["doctor", "--strict"]) == 1


def test_doctor_strict_does_not_hide_real_errors(home, tmp_path, capsys):
    docs = tmp_path / "gone"
    docs.mkdir()
    (docs / "index.md").write_text("# Docs\n", encoding="utf-8")
    assert main(["add", str(docs), "--name", "gone"]) == 0
    shutil.rmtree(docs)
    assert main(["doctor"]) == 1
    assert main(["doctor", "--strict"]) == 1
    assert "source directory missing" in capsys.readouterr().out
