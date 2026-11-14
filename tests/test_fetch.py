import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from virtudoc.cli import main
from virtudoc.db import open_db
from virtudoc.fetcher import FetchError, cache_path, fetch_source


def _docs_zip(tmp_path: Path, name: str = "docs.zip") -> Path:
    archive = tmp_path / name
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("docs-repo/index.md", "# Fetched\n\nСодержимое из архива.\n")
        zf.writestr("docs-repo/guide.md", "# Guide\n\nВторой документ.\n")
    return archive


def test_fetch_from_zip(home, tmp_path, capsys):
    archive = _docs_zip(tmp_path)
    assert main(["fetch", "--repo", archive.as_uri(), "--name", "ziplib"]) == 0
    out = capsys.readouterr().out
    assert "added library local/ziplib" in out
    from virtudoc.config import Config

    with open_db(Config().db_path) as conn:
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        assert chunks > 0
    assert (cache_path("ziplib") / "index.md").is_file()


def test_fetch_zip_single_root_unwrapped(home, tmp_path):
    archive = _docs_zip(tmp_path)
    dest = fetch_source(archive.as_uri(), "unwrap")
    assert dest.is_dir()
    assert (dest / "index.md").is_file()


def test_fetch_from_tarball(home, tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.md").write_text("# A\n\nТекст.\n", encoding="utf-8")
    archive = tmp_path / "docs.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(source, arcname="repo")
    assert main(["fetch", "--repo", archive.as_uri(), "--name", "tarlib"]) == 0
    assert (cache_path("tarlib") / "a.md").is_file()


def test_fetch_rejects_zip_slip(home, tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.md", "# Evil\n")
    with pytest.raises(FetchError, match="unsafe path"):
        fetch_source(archive.as_uri(), "evil")
    assert main(["fetch", "--repo", archive.as_uri(), "--name", "evil"]) == 1


def test_fetch_git_local_clone(home, tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "index.md").write_text("# Cloned\n\nДокументация из git.\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    assert main(["fetch", "--repo", str(repo), "--name", "gitlib"]) == 0
    assert (cache_path("gitlib") / "index.md").is_file()
    # second fetch pulls instead of cloning
    assert main(["fetch", "--repo", str(repo), "--name", "gitlib", "--replace"]) == 0


def _git_repo_with_branches(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=repo, check=True)
    (repo / "index.md").write_text("# Main docs\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "main"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "checkout", "-qb", "release"], cwd=repo, check=True)
    (repo / "index.md").write_text("# Release docs\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "release"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    return repo


def test_fetch_git_branch_flag(home, tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = _git_repo_with_branches(tmp_path)
    assert main(["fetch", "--repo", str(repo), "--name", "branchlib"]) == 0
    assert "# Main docs" in (cache_path("branchlib") / "index.md").read_text(encoding="utf-8")
    # explicit --branch overrides the repo default, on clone and on re-fetch
    args = ["fetch", "--repo", str(repo), "--name", "branchlib", "--branch", "release"]
    assert main(args) == 1  # cache holds main, library exists: --replace required
    assert main([*args, "--replace"]) == 0
    assert "# Release docs" in (cache_path("branchlib") / "index.md").read_text(encoding="utf-8")
    assert main([*args, "--replace"]) == 0
    assert "# Release docs" in (cache_path("branchlib") / "index.md").read_text(encoding="utf-8")


def test_fetch_git_branch_from_config(home, tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = _git_repo_with_branches(tmp_path)
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text('[fetch]\nbranch = "release"\n', encoding="utf-8")
    assert main(["fetch", "--repo", str(repo), "--name", "cfglib"]) == 0
    assert "# Release docs" in (cache_path("cfglib") / "index.md").read_text(encoding="utf-8")


def test_fetch_git_unknown_branch_fails(home, tmp_path, capsys):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = _git_repo_with_branches(tmp_path)
    assert main(["fetch", "--repo", str(repo), "--name", "badbranch", "--branch", "nope"]) == 1
    assert "git clone failed" in capsys.readouterr().err


def test_fetch_git_failure_is_clean_error(home, tmp_path, capsys):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    assert main(["fetch", "--repo", str(tmp_path / "nonexistent.git"), "--name", "bad"]) == 1
    assert "git clone failed" in capsys.readouterr().err


def test_fetch_duplicate_name_requires_replace(home, tmp_path, capsys):
    archive = _docs_zip(tmp_path)
    assert main(["fetch", "--repo", archive.as_uri(), "--name", "dupe"]) == 0
    capsys.readouterr()
    assert main(["fetch", "--repo", archive.as_uri(), "--name", "dupe"]) == 1
    assert "already exists" in capsys.readouterr().err


def test_fetch_cache_conflict_message_names_the_fix(home):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    dest = cache_path("conflict")
    dest.mkdir(parents=True)
    (dest / "index.md").write_text("# stale\n", encoding="utf-8")
    with pytest.raises(FetchError, match=f"rm -rf {dest}"):
        fetch_source("https://example.com/repo.git", "conflict")
