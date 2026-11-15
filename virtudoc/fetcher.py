from __future__ import annotations

import shutil
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from .config import data_dir
from .indexer.indexer import validate_name

ARCHIVE_SUFFIXES = (".zip", ".tar.gz", ".tgz", ".tar")

DOWNLOAD_TIMEOUT = 120

GIT_TIMEOUT = 300

MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024

_DOWNLOAD_CHUNK = 1024 * 1024


class FetchError(Exception):
    pass


def cache_path(name: str) -> Path:
    return data_dir() / "fetch" / name


def fetch_source(repo_url: str, name: str, branch: str | None = None) -> Path:
    """Download or clone repo_url into the local fetch cache and return the path.

    This is the only command allowed to touch the network (NFR-01).
    For git URLs, branch selects a non-default branch; archive URLs encode
    the branch in the URL already.
    """
    validate_name(name)
    dest = cache_path(name)
    lower = repo_url.lower().split("?", 1)[0]
    if lower.endswith(".zip"):
        _fetch_archive(repo_url, dest, kind="zip")
    elif lower.endswith((".tar.gz", ".tgz", ".tar")):
        _fetch_archive(repo_url, dest, kind="tar")
    else:
        _fetch_git(repo_url, dest, branch)
    return dest


def _read_capped(response, cap: int) -> bytes:
    parts: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(_DOWNLOAD_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise FetchError(f"download exceeds the size limit of {cap} bytes")
        parts.append(chunk)
    return b"".join(parts)


def _fetch_archive(url: str, dest: Path, *, kind: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    tmp = dest.with_suffix(dest.suffix + ".download")
    if tmp.exists():
        shutil.rmtree(tmp) if tmp.is_dir() else tmp.unlink()
    tmp.mkdir(parents=True)
    archive_file = tmp / f"archive.{kind}"
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response:
            archive_file.write_bytes(_read_capped(response, MAX_DOWNLOAD_BYTES))
    except (OSError, ValueError, FetchError) as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        if isinstance(exc, FetchError):
            raise
        raise FetchError(f"failed to download {url}: {exc}") from exc
    try:
        if kind == "zip":
            with zipfile.ZipFile(archive_file) as zf:
                _safe_extract_zip(zf, tmp / "extracted")
        else:
            with tarfile.open(archive_file) as tf:
                _safe_extract_tar(tf, tmp / "extracted")
    except (zipfile.BadZipFile, tarfile.TarError) as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise FetchError(f"failed to extract archive: {exc}") from exc
    root = _single_root(tmp / "extracted")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(root), str(dest))
    shutil.rmtree(tmp, ignore_errors=True)


def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    base = dest.resolve()
    for member in zf.infolist():
        if (member.external_attr >> 16) & 0o170000 == 0o120000:  # symlink mode bits
            raise FetchError(f"unsafe path in archive: {member.filename!r}")
        target = (dest / member.filename).resolve()
        if not str(target).startswith(str(base) + "/"):
            raise FetchError(f"unsafe path in archive: {member.filename!r}")
    zf.extractall(dest)


def _safe_extract_tar(tf: tarfile.TarFile, dest: Path) -> None:
    base = dest.resolve()
    for member in tf.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(base) + "/"):
            raise FetchError(f"unsafe path in archive: {member.name!r}")
    try:
        tf.extractall(dest, filter="data")  # Python 3.12+
    except TypeError:  # Python 3.11: member checks above are the safeguard
        tf.extractall(dest)


def _single_root(extracted: Path) -> Path:
    entries = list(extracted.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return extracted


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
            return
        result = _run_git(["git", "-C", str(dest), "pull", "--ff-only"])
        if result.returncode != 0:
            raise FetchError(f"git pull failed: {result.stderr.strip()}")
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
