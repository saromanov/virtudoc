from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .config import data_dir
from .indexer.indexer import validate_name

ARCHIVE_SUFFIXES = (".zip", ".tar.gz", ".tgz", ".tar")

DOWNLOAD_TIMEOUT = 120

GIT_TIMEOUT = 300

MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024

MAX_EXTRACT_BYTES = 2 * 1024 * 1024 * 1024

MAX_EXTRACT_FILES = 100_000

_DOWNLOAD_CHUNK = 1024 * 1024

PIN_FILE = ".virtudoc-fetch.json"


class FetchError(Exception):
    pass


def cache_path(name: str) -> Path:
    return data_dir() / "fetch" / name


def fetch_source(
    repo_url: str,
    name: str,
    branch: str | None = None,
    *,
    max_extract_bytes: int | None = None,
    max_extract_files: int | None = None,
) -> Path:
    """Download or clone repo_url into the local fetch cache and return the path.

    This is the only command allowed to touch the network (NFR-01).
    For git URLs, branch selects a non-default branch; archive URLs encode
    the branch in the URL already.
    """
    validate_name(name)
    dest = cache_path(name)
    lower = repo_url.lower().split("?", 1)[0]
    if lower.endswith((".zip", ".tar.gz", ".tgz", ".tar")):
        max_extract_bytes = MAX_EXTRACT_BYTES if max_extract_bytes is None else max_extract_bytes
        max_extract_files = MAX_EXTRACT_FILES if max_extract_files is None else max_extract_files
        if max_extract_bytes <= 0 or max_extract_files <= 0:
            raise ValueError("archive extraction limits must be positive")
        kind = "zip" if lower.endswith(".zip") else "tar"
        _fetch_archive(
            repo_url,
            dest,
            kind=kind,
            max_extract_bytes=max_extract_bytes,
            max_extract_files=max_extract_files,
        )
    else:
        _fetch_git(repo_url, dest, branch)
    return dest


def _download_capped(response, dest: Path, cap: int) -> None:
    total = 0
    with dest.open("wb") as output:
        while True:
            chunk = response.read(_DOWNLOAD_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise FetchError(f"download exceeds the size limit of {cap} bytes")
            output.write(chunk)


def _fetch_archive(
    url: str,
    dest: Path,
    *,
    kind: str,
    max_extract_bytes: int,
    max_extract_files: int,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{dest.name}.download-", dir=dest.parent))
    archive_file = tmp / f"archive.{kind}"
    try:
        try:
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response:
                _download_capped(response, archive_file, MAX_DOWNLOAD_BYTES)
        except FetchError:
            raise
        except (OSError, ValueError) as exc:
            raise FetchError(f"failed to download {url}: {exc}") from exc
        try:
            if kind == "zip":
                with zipfile.ZipFile(archive_file) as zf:
                    _safe_extract_zip(
                        zf,
                        tmp / "extracted",
                        max_extract_bytes=max_extract_bytes,
                        max_extract_files=max_extract_files,
                    )
            else:
                with tarfile.open(archive_file) as tf:
                    _safe_extract_tar(
                        tf,
                        tmp / "extracted",
                        max_extract_bytes=max_extract_bytes,
                        max_extract_files=max_extract_files,
                    )
        except FetchError:
            raise
        except (zipfile.BadZipFile, tarfile.TarError, OSError, RuntimeError) as exc:
            raise FetchError(f"failed to extract archive: {exc}") from exc
        _replace_cache(_single_root(tmp / "extracted"), dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _check_archive_limits(size: int, files: int, max_bytes: int, max_files: int) -> None:
    if files > max_files:
        raise FetchError(f"archive exceeds the file-count limit of {max_files}")
    if size > max_bytes:
        raise FetchError(f"archive exceeds the extracted-size limit of {max_bytes} bytes")


def _member_target(dest: Path, name: str) -> Path:
    base = dest.resolve()
    target = (dest / name).resolve()
    if target == base or base not in target.parents:
        raise FetchError(f"unsafe path in archive: {name!r}")
    return target


def _copy_member(source, target: Path, total: int, max_bytes: int) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as output:
        while True:
            chunk = source.read(_DOWNLOAD_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            _check_archive_limits(total, 0, max_bytes, 0)
            output.write(chunk)
    return total


def _safe_extract_zip(
    zf: zipfile.ZipFile,
    dest: Path,
    *,
    max_extract_bytes: int = MAX_EXTRACT_BYTES,
    max_extract_files: int = MAX_EXTRACT_FILES,
) -> None:
    members = zf.infolist()
    total = 0
    files = 0
    for member in members:
        if (member.external_attr >> 16) & 0o170000 == 0o120000:
            raise FetchError(f"unsafe path in archive: {member.filename!r}")
        _member_target(dest, member.filename)
        if not member.is_dir():
            files += 1
            total += member.file_size
            _check_archive_limits(total, files, max_extract_bytes, max_extract_files)

    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    files = 0
    for member in members:
        target = _member_target(dest, member.filename)
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        files += 1
        _check_archive_limits(total, files, max_extract_bytes, max_extract_files)
        with zf.open(member) as source:
            total = _copy_member(source, target, total, max_extract_bytes)


def _safe_extract_tar(
    tf: tarfile.TarFile,
    dest: Path,
    *,
    max_extract_bytes: int = MAX_EXTRACT_BYTES,
    max_extract_files: int = MAX_EXTRACT_FILES,
) -> None:
    members = tf.getmembers()
    total = 0
    files = 0
    for member in members:
        if not member.isfile() and not member.isdir():
            raise FetchError(f"unsafe member in archive: {member.name!r}")
        _member_target(dest, member.name)
        if member.isfile():
            files += 1
            total += member.size
            _check_archive_limits(total, files, max_extract_bytes, max_extract_files)

    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    files = 0
    for member in members:
        target = _member_target(dest, member.name)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        files += 1
        _check_archive_limits(total, files, max_extract_bytes, max_extract_files)
        source = tf.extractfile(member)
        if source is None:
            raise FetchError(f"cannot extract archive member: {member.name!r}")
        with source:
            total = _copy_member(source, target, total, max_extract_bytes)


def _replace_cache(root: Path, dest: Path) -> None:
    backup_parent: Path | None = None
    backup: Path | None = None
    succeeded = False
    try:
        if dest.exists():
            backup_parent = Path(tempfile.mkdtemp(prefix=f".{dest.name}.backup-", dir=dest.parent))
            backup = backup_parent / "cache"
            os.replace(dest, backup)
        try:
            os.replace(root, dest)
        except Exception:
            if backup is not None:
                os.replace(backup, dest)
            raise
        succeeded = True
    finally:
        if backup_parent is not None and (succeeded or not backup.exists()):
            shutil.rmtree(backup_parent, ignore_errors=True)


def _single_root(extracted: Path) -> Path:
    entries = list(extracted.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return extracted


def read_pinned_branch(dest: Path) -> str | None:
    try:
        data = json.loads((dest / PIN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    branch = data.get("branch")
    return str(branch) if branch else None


def _write_pinned_branch(dest: Path, branch: str | None) -> None:
    if not branch:
        return
    try:
        (dest / PIN_FILE).write_text(json.dumps({"branch": branch}), encoding="utf-8")
    except OSError:
        pass


def _pin_current_branch(dest: Path, branch: str | None = None) -> None:
    pinned = branch
    if not pinned:
        result = _run_git(["git", "-C", str(dest), "symbolic-ref", "--short", "HEAD"])
        pinned = result.stdout.strip() or None
    _write_pinned_branch(dest, pinned)


def ensure_pinned_checkout(dest: Path) -> str | None:
    """Reset a fetch-cache clone to its pinned branch before reindexing.

    Local operations only (NFR-01: nothing but `fetch` touches the
    network): the checkout goes back to the local `origin/<branch>` ref
    recorded by the last fetch. A dirty working tree is left untouched —
    never destroy work in progress silently. Returns the pinned branch
    when one is recorded, None otherwise.
    """
    pinned = read_pinned_branch(dest)
    if not pinned or shutil.which("git") is None or not (dest / ".git").is_dir():
        return pinned
    try:
        head = _run_git(["git", "-C", str(dest), "symbolic-ref", "--short", "-q", "HEAD"])
        branch = head.stdout.strip() or None
        if branch == pinned:
            return pinned
        status = _run_git(
            [
                "git",
                "-C",
                str(dest),
                "status",
                "--porcelain",
                "--",
                ".",
                f":(exclude){PIN_FILE}",
            ]
        )
        if status.stdout.strip():
            return pinned
        _run_git(["git", "-C", str(dest), "checkout", "-q", "-B", pinned, f"origin/{pinned}"])
    except FetchError:
        pass
    return pinned


def _run_git(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise FetchError(
            f"git command timed out after {GIT_TIMEOUT}s: {' '.join(args[:4])}"
        ) from exc


def _fetch_git(url: str, dest: Path, branch: str | None = None) -> None:
    if shutil.which("git") is None:
        raise FetchError("git is not installed; cannot clone repositories")
    if (dest / ".git").is_dir():
        if branch:
            result = _run_git(["git", "-C", str(dest), "fetch", "--depth", "1", "origin", branch])
            if result.returncode != 0:
                raise FetchError(f"git fetch of branch '{branch}' failed: {result.stderr.strip()}")
            result = _run_git(
                ["git", "-C", str(dest), "checkout", "-q", "-B", branch, "FETCH_HEAD"]
            )
            if result.returncode != 0:
                raise FetchError(f"git checkout {branch} failed: {result.stderr.strip()}")
            _pin_current_branch(dest, branch)
            return
        result = _run_git(["git", "-C", str(dest), "pull", "--ff-only"])
        if result.returncode != 0:
            raise FetchError(f"git pull failed: {result.stderr.strip()}")
        _pin_current_branch(dest)
        return
    if dest.exists():
        raise FetchError(
            f"fetch cache already exists and is not a git clone: {dest}; "
            f"remove it to fetch again: rm -rf {dest}"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [url, str(dest)]
    try:
        result = _run_git(cmd)
    except FetchError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    if result.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise FetchError(f"git clone failed: {result.stderr.strip()}")
    _pin_current_branch(dest, branch)
