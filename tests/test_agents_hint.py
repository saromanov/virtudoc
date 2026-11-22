from __future__ import annotations

from virtudoc.agents_hint import AGENTS_HINT, MARKER
from virtudoc.cli import main


def _project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==1.0\n", encoding="utf-8")
    return project


def test_agents_md_directory_fails_without_traceback(home, tmp_path, capsys):
    project = _project(tmp_path)
    (project / "AGENTS.md").mkdir()

    assert main(["init", str(project), "--agents-hint"]) == 1
    captured = capsys.readouterr()
    assert "error: cannot write agents hint" in captured.err
    assert "not a regular file" in captured.err
    assert "Traceback" not in captured.err


def test_file_mentioning_virtudoc_still_receives_the_block(home, tmp_path, capsys):
    project = _project(tmp_path)
    agents = project / "AGENTS.md"
    agents.write_text("# Rules\n\nDo not use virtudoc here.\n", encoding="utf-8")

    assert main(["init", str(project), "--agents-hint"]) == 0
    captured = capsys.readouterr()
    assert f"agents hint written to {agents}" in captured.out
    text = agents.read_text(encoding="utf-8")
    assert text.startswith("# Rules\n\nDo not use virtudoc here.\n")
    assert text.count(MARKER) == 1
    assert AGENTS_HINT in text

    assert main(["init", str(project), "--agents-hint"]) == 0
    captured = capsys.readouterr()
    assert f"agents hint already present in {agents}" in captured.out
    assert agents.read_text(encoding="utf-8") == text


def test_non_utf8_agents_md_fails_with_a_hint_error(home, tmp_path, capsys):
    project = _project(tmp_path)
    (project / "AGENTS.md").write_bytes(b"# Rules\n\n\xff\xfe broken\n")

    assert main(["init", str(project), "--agents-hint"]) == 1
    captured = capsys.readouterr()
    assert "error: cannot write agents hint" in captured.err
    assert "Traceback" not in captured.err
