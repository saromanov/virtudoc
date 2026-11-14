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
