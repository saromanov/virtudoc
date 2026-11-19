import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from virtudoc import fetcher as fetcher_module
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


def test_fetch_rejects_bad_name(home, tmp_path, capsys):
    archive = _docs_zip(tmp_path)
    with pytest.raises(ValueError, match="invalid library name"):
        fetch_source(archive.as_uri(), "../escape")
    assert main(["fetch", "--repo", archive.as_uri(), "--name", "../escape"]) == 1
    assert "invalid library name" in capsys.readouterr().err
    assert not (home / "escape").exists()


def test_fetch_rejects_zip_symlink(home, tmp_path):
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        info = zipfile.ZipInfo("docs-repo/passwd")
        info.external_attr = 0o120777 << 16  # symlink file mode
        zf.writestr(info, "/etc/passwd")
        zf.writestr("docs-repo/index.md", "# Linked\n")
    with pytest.raises(FetchError, match="unsafe path"):
        fetch_source(archive.as_uri(), "evil-link")
    assert not (home / "fetch" / "evil-link").exists()


def test_fetch_download_size_cap(home, tmp_path, monkeypatch):
    archive = _docs_zip(tmp_path)
    monkeypatch.setattr(fetcher_module, "MAX_DOWNLOAD_BYTES", 16)
    with pytest.raises(FetchError, match="size limit"):
        fetch_source(archive.as_uri(), "capped")
    assert not (home / "fetch" / "capped").exists()


def test_fetch_extract_size_cap_preserves_existing_cache(home, tmp_path):
    archive = _docs_zip(tmp_path)
    dest = cache_path("extract-cap")
    dest.mkdir(parents=True)
    (dest / "old.md").write_text("# Keep me\n", encoding="utf-8")

    with pytest.raises(FetchError, match="extracted-size limit"):
        fetch_source(archive.as_uri(), "extract-cap", max_extract_bytes=16)

    assert (dest / "old.md").read_text(encoding="utf-8") == "# Keep me\n"
    assert not list(dest.parent.glob(".extract-cap.download-*"))
    assert not list(dest.parent.glob(".extract-cap.backup-*"))


def test_fetch_extract_file_count_cap(home, tmp_path):
    archive = _docs_zip(tmp_path)
    with pytest.raises(FetchError, match="file-count limit"):
        fetch_source(archive.as_uri(), "too-many", max_extract_files=1)
    assert not cache_path("too-many").exists()


def test_fetch_archive_success_replaces_old_cache(home, tmp_path):
    archive = _docs_zip(tmp_path)
    dest = cache_path("archive-replace")
    dest.mkdir(parents=True)
    (dest / "old.md").write_text("# Remove me\n", encoding="utf-8")

    fetch_source(archive.as_uri(), "archive-replace")

    assert not (dest / "old.md").exists()
    assert (dest / "index.md").is_file()


@pytest.mark.parametrize("link_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_fetch_rejects_tar_links(home, tmp_path, link_type):
    archive = tmp_path / f"link-{link_type.decode()}.tar"
    with tarfile.open(archive, "w") as tf:
        info = tarfile.TarInfo("docs-repo/passwd")
        info.type = link_type
        info.linkname = "/etc/passwd"
        tf.addfile(info)

    with pytest.raises(FetchError, match="unsafe member"):
        fetch_source(archive.as_uri(), "tar-link")
    assert not cache_path("tar-link").exists()


def test_fetch_git_timeout_is_clean_error(home, monkeypatch, capsys):
    if shutil.which("git") is None:
        pytest.skip("git not available")

    def expired(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=fetcher_module.GIT_TIMEOUT)

    monkeypatch.setattr(fetcher_module.subprocess, "run", expired)
    with pytest.raises(FetchError, match="timed out"):
        fetch_source("https://example.com/repo.git", "slow")
    assert not (home / "fetch" / "slow").exists()


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


def test_fetch_records_pinned_branch(home, tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = _git_repo_with_branches(tmp_path)
    assert main(["fetch", "--repo", str(repo), "--name", "pinlib", "--branch", "release"]) == 0
    from virtudoc.fetcher import read_pinned_branch

    assert read_pinned_branch(cache_path("pinlib")) == "release"


def test_malformed_pin_sidecar_is_ignored(home):
    from virtudoc.fetcher import PIN_FILE, ensure_pinned_checkout, read_pinned_branch

    dest = cache_path("badpin")
    dest.mkdir(parents=True)
    (dest / PIN_FILE).write_text("[]", encoding="utf-8")
    assert read_pinned_branch(dest) is None
    assert ensure_pinned_checkout(dest) is None


def test_ensure_pinned_checkout_resets_drifted_clone(home, tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = _git_repo_with_branches(tmp_path)
    fetch_source(str(repo), "driftlib", branch="release")
    dest = cache_path("driftlib")
    subprocess.run(["git", "-C", str(dest), "checkout", "-q", "--detach", "HEAD"], check=True)
    head = subprocess.run(
        ["git", "-C", str(dest), "symbolic-ref", "--short", "-q", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == ""  # detached: no branch ref

    from virtudoc.fetcher import ensure_pinned_checkout

    assert ensure_pinned_checkout(dest) == "release"
    head = subprocess.run(
        ["git", "-C", str(dest), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == "release"


def test_ensure_pinned_checkout_leaves_dirty_tree(home, tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = _git_repo_with_branches(tmp_path)
    fetch_source(str(repo), "dirtylib", branch="release")
    dest = cache_path("dirtylib")
    subprocess.run(["git", "-C", str(dest), "checkout", "-q", "--detach", "HEAD"], check=True)
    (dest / "work.md").write_text("# Work in progress\n", encoding="utf-8")

    from virtudoc.fetcher import ensure_pinned_checkout

    assert ensure_pinned_checkout(dest) == "release"
    head = subprocess.run(
        ["git", "-C", str(dest), "symbolic-ref", "--short", "-q", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == ""  # dirty detached tree is never reset
    assert (dest / "work.md").exists()


def test_prepare_serve_resets_to_pinned_branch(home, tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    import virtudoc.mcp_server as mcp_server_module
    from virtudoc.config import Config
    from virtudoc.db import open_db
    from virtudoc.indexer.indexer import add_library

    repo = _git_repo_with_branches(tmp_path)
    fetch_source(str(repo), "servelib", branch="release")
    subprocess.run(
        ["git", "-C", cache_path("servelib"), "checkout", "-q", "--detach", "HEAD"], check=True
    )
    with open_db(Config().db_path) as conn:
        add_library(conn, cache_path("servelib"), "servelib")
    mcp_server_module.prepare_serve(Config())
    head = subprocess.run(
        ["git", "-C", cache_path("servelib"), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == "release"
    content = (cache_path("servelib") / "index.md").read_text(encoding="utf-8")
    assert "# Release docs" in content


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


def test_fetch_rejects_non_positive_extract_limits(home, tmp_path):
    archive = _docs_zip(tmp_path)
    with pytest.raises(ValueError, match="must be positive"):
        fetch_source(archive.as_uri(), "limits", max_extract_bytes=0)
    with pytest.raises(ValueError, match="must be positive"):
        fetch_source(archive.as_uri(), "limits", max_extract_files=0)


def test_fetch_download_failure_is_clean(home, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(fetcher_module.urllib.request, "urlopen", broken)
    with pytest.raises(FetchError, match="failed to download"):
        fetch_source("https://example.com/docs.zip", "broken-download")


def test_fetch_corrupt_archive_is_clean_error(home, tmp_path):
    archive = tmp_path / "corrupt.zip"
    archive.write_text("not a zip", encoding="utf-8")
    with pytest.raises(FetchError, match="failed to extract archive"):
        fetch_source(archive.as_uri(), "corrupt")


def test_fetch_extracts_explicit_directory_entries(home, tmp_path):
    archive = tmp_path / "with-dir.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("docs-repo/", "")
        zf.writestr("docs-repo/index.md", "# Index\n")
    fetch_source(archive.as_uri(), "with-dir")
    assert (cache_path("with-dir") / "index.md").is_file()


def test_replace_cache_restores_backup_when_swap_fails(home, tmp_path, monkeypatch):
    from virtudoc.fetcher import _replace_cache

    dest = tmp_path / "cache"
    dest.mkdir()
    (dest / "old.md").write_text("old", encoding="utf-8")
    root = tmp_path / "new"
    root.mkdir()
    (root / "new.md").write_text("new", encoding="utf-8")

    real_replace = fetcher_module.os.replace
    calls = {"count": 0}

    def flaky(source, target):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("swap failed")
        return real_replace(source, target)

    monkeypatch.setattr(fetcher_module.os, "replace", flaky)
    with pytest.raises(RuntimeError, match="swap failed"):
        _replace_cache(root, dest)

    assert (dest / "old.md").read_text(encoding="utf-8") == "old"
    assert not (dest / "new.md").exists()


def test_single_root_returns_extracted_for_multiple_entries(home, tmp_path):
    from virtudoc.fetcher import _single_root

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    (extracted / "a.md").write_text("# A\n", encoding="utf-8")
    (extracted / "b.md").write_text("# B\n", encoding="utf-8")
    assert _single_root(extracted) == extracted

    nested = tmp_path / "nested"
    (nested / "only").mkdir(parents=True)
    assert _single_root(nested) == nested / "only"


def test_write_pinned_branch_handles_empty_and_unwritable(home, tmp_path):
    from virtudoc.fetcher import _write_pinned_branch

    _write_pinned_branch(tmp_path, None)  # no branch: nothing written
    assert not (tmp_path / ".virtudoc-fetch.json").exists()

    _write_pinned_branch(tmp_path / "missing", "main")  # OSError is ignored


def test_ensure_pinned_checkout_keeps_current_branch(home, tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    from virtudoc.fetcher import ensure_pinned_checkout

    repo = _git_repo_with_branches(tmp_path)
    fetch_source(str(repo), "pin-fresh", branch="release")
    assert ensure_pinned_checkout(cache_path("pin-fresh")) == "release"


def test_ensure_pinned_checkout_swallows_git_errors(home, monkeypatch):
    from virtudoc.fetcher import PIN_FILE, ensure_pinned_checkout

    dest = cache_path("pin-errors")
    (dest / ".git").mkdir(parents=True)
    (dest / PIN_FILE).write_text('{"branch": "release"}', encoding="utf-8")

    def broken(args):
        raise FetchError("git broke")

    monkeypatch.setattr(fetcher_module.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(fetcher_module, "_run_git", broken)
    assert ensure_pinned_checkout(dest) == "release"


def test_fetch_requires_git(home, monkeypatch):
    monkeypatch.setattr(fetcher_module.shutil, "which", lambda name: None)
    with pytest.raises(FetchError, match="git is not installed"):
        fetch_source("https://example.com/repo.git", "nogit")


def test_fetch_existing_clone_missing_branch(home, tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = _git_repo_with_branches(tmp_path)
    fetch_source(str(repo), "branch-fail")
    with pytest.raises(FetchError, match="git fetch of branch 'nope' failed"):
        fetch_source(str(repo), "branch-fail", branch="nope")


def test_fetch_checkout_failure_is_clean(home, monkeypatch):
    from virtudoc.fetcher import _fetch_git

    if shutil.which("git") is None:
        pytest.skip("git not available")
    dest = cache_path("checkout-fail")
    (dest / ".git").mkdir(parents=True)

    def fake_run(args):
        if "fetch" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", "checkout boom")

    monkeypatch.setattr(fetcher_module, "_run_git", fake_run)
    with pytest.raises(FetchError, match="git checkout x failed"):
        _fetch_git("https://example.com/repo.git", dest, branch="x")


def test_fetch_pull_failure_is_clean(home, monkeypatch):
    from virtudoc.fetcher import _fetch_git

    if shutil.which("git") is None:
        pytest.skip("git not available")
    dest = cache_path("pull-fail")
    (dest / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        fetcher_module,
        "_run_git",
        lambda args: subprocess.CompletedProcess(args, 1, "", "pull broke"),
    )
    with pytest.raises(FetchError, match="git pull failed"):
        _fetch_git("https://example.com/repo.git", dest)


def test_tar_member_without_stream_is_clean_error(home, tmp_path, monkeypatch):
    from virtudoc.fetcher import _safe_extract_tar

    archive = tmp_path / "stream.tar"
    with tarfile.open(archive, "w") as tf:
        info = tarfile.TarInfo("a.md")
        info.size = 0
        tf.addfile(info)

    monkeypatch.setattr(tarfile.TarFile, "extractfile", lambda self, member: None)
    with tarfile.open(archive) as tf:
        with pytest.raises(FetchError, match="cannot extract archive member"):
            _safe_extract_tar(tf, tmp_path / "out")
