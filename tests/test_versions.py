import json

import pytest

from virtudoc.indexer.chunker import Chunker
from virtudoc.indexer.indexer import add_library, index_library
from virtudoc.versions import (
    detect_manifest,
    extract_dep_version,
    link_library,
    mismatch_payload,
    unlink_library,
    version_status,
)


def test_detect_manifest_priority(tmp_path):
    (tmp_path / "requirements.txt").write_text("a==1.0\n", encoding="utf-8")
    assert detect_manifest(tmp_path).name == "requirements.txt"
    (tmp_path / "pyproject.toml").write_text("[project]\ndependencies=[]\n", encoding="utf-8")
    assert detect_manifest(tmp_path).name == "pyproject.toml"


def test_extract_from_pyproject(tmp_path):
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(
        """
[project]
dependencies = [
    "requests>=2.0",
    "Demo_Tool~=1.4.0",
]
[project.optional-dependencies]
test = ["pytest>=8"]
""",
        encoding="utf-8",
    )
    assert extract_dep_version(manifest, "demo-tool") == "1.4.0"
    assert extract_dep_version(manifest, "requests") == "2.0"
    assert extract_dep_version(manifest, "absent") is None


def test_extract_from_package_json(tmp_path):
    manifest = tmp_path / "package.json"
    manifest.write_text(
        json.dumps({"dependencies": {"demo-lib": "^2.1.0"}, "devDependencies": {"x": "1.0.0"}}),
        encoding="utf-8",
    )
    assert extract_dep_version(manifest, "demo-lib") == "2.1.0"
    assert (
        extract_dep_version(manifest, "x") is None or extract_dep_version(manifest, "x") == "1.0.0"
    )


def test_extract_from_requirements(tmp_path):
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("demo==3.2.1\nother>=1.0\n", encoding="utf-8")
    assert extract_dep_version(manifest, "demo") == "3.2.1"
    assert extract_dep_version(manifest, "other") == "1.0"


def test_extract_from_go_mod(tmp_path):
    manifest = tmp_path / "go.mod"
    manifest.write_text(
        "module example.com/app\n\nrequire github.com/acme/demo v1.2.3\n",
        encoding="utf-8",
    )
    assert extract_dep_version(manifest, "demo") == "v1.2.3"
    assert extract_dep_version(manifest, "other") is None


def test_version_status():
    assert version_status("1.2.0", "1.2.0") == "match"
    assert version_status("2.0.0", "1.9.9") == "mismatch"
    assert version_status(None, "1.0.0") == "unknown"
    assert version_status("1.0", "garbage") == "unknown"


@pytest.fixture
def linked(conn, docs_dir, tmp_path):
    lib_id = add_library(conn, docs_dir, "demo", version="1.0")
    index_library(conn, lib_id, Chunker())
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==2.0.0\n", encoding="utf-8")
    info = link_library(conn, lib_id, project)
    return lib_id, project, info


def test_link_library_stores_manifest(conn, linked):
    lib_id, project, info = linked
    assert info.dep_version == "2.0.0"
    assert info.manifest_path == project / "requirements.txt"
    assert unlink_library(conn, lib_id) is True
    assert unlink_library(conn, lib_id) is False


def test_mismatch_payload_warns_on_drift(conn, linked):
    lib_id, _, _ = linked
    payload = mismatch_payload(conn, lib_id)
    assert payload is not None
    assert payload["documentation_version"] == "1.0"
    assert payload["project_version"] == "2.0.0"
    assert "requirements.txt" in payload["manifest"]


def test_mismatch_payload_none_when_match(conn, docs_dir, tmp_path):
    lib_id = add_library(conn, docs_dir, "demo", version="2.0.0")
    index_library(conn, lib_id, Chunker())
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==2.0.0\n", encoding="utf-8")
    link_library(conn, lib_id, project)
    assert mismatch_payload(conn, lib_id) is None


def test_link_errors(conn, docs_dir, tmp_path):
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="no dependency manifest"):
        link_library(conn, lib_id, empty)
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("other==1.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not found"):
        link_library(conn, lib_id, project)
    with pytest.raises(ValueError, match="project directory not found"):
        link_library(conn, lib_id, tmp_path / "nope")
