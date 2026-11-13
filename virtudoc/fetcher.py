from __future__ import annotations

import shutil
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from .config import data_dir

ARCHIVE_SUFFIXES = (".zip", ".tar.gz", ".tgz", ".tar")

DOWNLOAD_TIMEOUT = 120


class FetchError(Exception):
    pass


def cache_path(name: str) -> Path:
    return data_dir() / "fetch" / name


def fetch_source(repo_url: str, name: str) -> Path:
    """Download or clone repo_url into the local fetch cache and return the path.

    This is the only command allowed to touch the network (NFR-01).
    """
    dest = cache_path(name)
    lower = repo_url.lower().split("?", 1)[0]
    if lower.endswith(".zip"):
        _fetch_archive(repo_url, dest, kind="zip")
    elif lower.endswith((".tar.gz", ".tgz", ".tar")):
        _fetch_archive(repo_url, dest, kind="tar")
    else:
        _fetch_git(repo_url, dest)
    return dest


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
            archive_file.write_bytes(response.read())
    except (OSError, ValueError) as exc:
        shutil.rmtree(tmp, ignore_errors=True)
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


def _fetch_git(url: str, dest: Path) -> None:
    if shutil.which("git") is None:
        raise FetchError("git is not installed; cannot clone repositories")
    if (dest / ".git").is_dir():
        result = subprocess.run(
            ["git", "-C", str(dest), "pull", "--ff-only"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise FetchError(f"git pull failed: {result.stderr.strip()}")
        return
    if dest.exists():
        raise FetchError(
            f"fetch cache already exists and is not a git clone: {dest}; remove it to fetch again"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", "--depth", "1", url, str(dest)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise FetchError(f"git clone failed: {result.stderr.strip()}")
