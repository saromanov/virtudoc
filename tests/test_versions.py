import json

import pytest

from virtudoc.indexer.chunker import Chunker
from virtudoc.indexer.indexer import add_library, index_library
from virtudoc.versions import (
    detect_manifest,
    extract_dep_version,
    extract_dependencies,
    extract_dependency_specs,
    get_link,
    link_library,
    list_manifests,
    mismatch_payload,
    range_status,
    resolve_project_version,
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


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("package.json", "[]"),
        ("pyproject.toml", 'project = "not-a-table"\n'),
        ("Pipfile.lock", '{"default": []}'),
        ("conan.lock", "{not json"),
    ],
)
def test_malformed_manifest_yields_empty_mapping(tmp_path, filename, content):
    manifest = tmp_path / filename
    manifest.write_text(content, encoding="utf-8")
    assert extract_dependencies(manifest) == {}


def test_extract_from_go_mod(tmp_path):
    manifest = tmp_path / "go.mod"
    manifest.write_text(
        "module example.com/app\n\nrequire github.com/acme/demo v1.2.3\n",
        encoding="utf-8",
    )
    assert extract_dep_version(manifest, "demo") == "v1.2.3"
    assert extract_dep_version(manifest, "other") is None


def test_extract_from_poetry_pyproject(tmp_path):
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(
        """
[tool.poetry.dependencies]
python = "^3.11"
demo-lib = "^1.2"
complex = {version = ">=2.0,<3"}
""",
        encoding="utf-8",
    )
    assert extract_dep_version(manifest, "demo-lib") == "1.2"
    assert extract_dep_version(manifest, "complex") == "2.0"
    assert extract_dep_version(manifest, "python") == "3.11"
    assert extract_dep_version(manifest, "absent") is None


def test_extract_from_uv_lock(tmp_path):
    manifest = tmp_path / "uv.lock"
    manifest.write_text(
        """
version = 1
[[package]]
name = "click"
version = "8.1.7"
[[package]]
name = "demo-lib"
version = "2.4.0"
""",
        encoding="utf-8",
    )
    assert extract_dep_version(manifest, "demo-lib") == "2.4.0"
    assert extract_dep_version(manifest, "click") == "8.1.7"
    assert extract_dep_version(manifest, "absent") is None


def test_extract_from_poetry_lock(tmp_path):
    manifest = tmp_path / "poetry.lock"
    manifest.write_text(
        """
[[package]]
name = "demo-lib"
version = "3.0.1"
""",
        encoding="utf-8",
    )
    assert extract_dep_version(manifest, "demo-lib") == "3.0.1"


def test_extract_from_pipfile_lock(tmp_path):
    manifest = tmp_path / "Pipfile.lock"
    manifest.write_text(
        json.dumps(
            {
                "default": {"demo-lib": {"version": "==1.5.2"}},
                "develop": {"pytest": {"version": "==8.0.0"}},
            }
        ),
        encoding="utf-8",
    )
    assert extract_dep_version(manifest, "demo-lib") == "1.5.2"
    assert extract_dep_version(manifest, "pytest") == "8.0.0"
    assert extract_dep_version(manifest, "absent") is None


def test_detect_manifest_lockfiles_last(tmp_path):
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    assert detect_manifest(tmp_path).name == "uv.lock"
    (tmp_path / "requirements.txt").write_text("demo==1.0\n", encoding="utf-8")
    assert detect_manifest(tmp_path).name == "requirements.txt"


def test_link_uses_uv_lock(conn, docs_dir, tmp_path):
    project = tmp_path / "uvproject"
    project.mkdir()
    (project / "uv.lock").write_text(
        '[[package]]\nname = "demo"\nversion = "4.1.0"\n', encoding="utf-8"
    )
    lib_id = add_library(conn, docs_dir, "demo", version="4.0")
    index_library(conn, lib_id, Chunker())
    info = link_library(conn, lib_id, project)
    assert info.dep_version == "4.1.0"
    assert mismatch_payload(conn, lib_id) is not None


def test_version_status():
    assert version_status("1.2.0", "1.2.0") == "match"
    assert version_status("2.0.0", "1.9.9") == "mismatch"
    assert version_status(None, "1.0.0") == "unknown"
    assert version_status("1.0", "garbage") == "unknown"


def test_version_status_ignores_trailing_zeros():
    assert version_status("3.4", "3.4.0") == "match"
    assert version_status("3.4.0", "3.4") == "match"
    assert version_status("1", "1.0.0") == "match"
    assert version_status("2.0.0.0", "2") == "match"
    assert version_status("1.0.0", "1.0.1") == "mismatch"
    assert version_status("1.0", "1.1") == "mismatch"
    assert version_status("0.0", "0") == "match"


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


def test_mismatch_payload_none_for_trailing_zero_versions(conn, docs_dir, tmp_path):
    lib_id = add_library(conn, docs_dir, "demo", version="3.4")
    index_library(conn, lib_id, Chunker())
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==3.4.0\n", encoding="utf-8")
    info = link_library(conn, lib_id, project)
    assert info.dep_version == "3.4.0"
    assert mismatch_payload(conn, lib_id) is None


def test_mismatch_payload_reports_meaningful_difference(conn, docs_dir, tmp_path):
    lib_id = add_library(conn, docs_dir, "demo", version="3.4")
    index_library(conn, lib_id, Chunker())
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==3.4.1\n", encoding="utf-8")
    link_library(conn, lib_id, project)
    payload = mismatch_payload(conn, lib_id)
    assert payload is not None
    assert payload["documentation_version"] == "3.4"
    assert payload["project_version"] == "3.4.1"


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


def test_list_manifests_returns_all_present(tmp_path):
    (tmp_path / "requirements.txt").write_text("demo==1.0\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "demo"\nversion = "1.0"\n')
    assert [path.name for path in list_manifests(tmp_path)] == ["requirements.txt", "uv.lock"]
    assert list_manifests(tmp_path / "missing") == []


def test_extract_dependencies_merges_sources(tmp_path):
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(
        """
[project]
dependencies = ["requests>=2.0", "Demo_Tool~=1.4.0"]
[project.optional-dependencies]
test = ["pytest>=8"]
[tool.poetry.dependencies]
python = "^3.11"
legacy = "^0.9"
""",
        encoding="utf-8",
    )
    assert extract_dependencies(manifest) == {
        "requests": "2.0",
        "demo-tool": "1.4.0",
        "pytest": "8",
        "python": "3.11",
        "legacy": "0.9",
    }


def test_extract_dependencies_handles_broken_manifest(tmp_path):
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text("[project\n", encoding="utf-8")
    assert extract_dependencies(manifest) == {}
    assert extract_dependencies(tmp_path / "unknown.txt") == {}


def test_extract_dependency_specs_skips_non_string_versions(tmp_path):
    manifest = tmp_path / "package.json"
    manifest.write_text(
        '{"dependencies": {"demo": 2, "other": null, "third": "^1.0"}}', encoding="utf-8"
    )
    assert extract_dependency_specs(manifest) == {"demo": None, "other": None, "third": "^1.0"}
    assert extract_dependencies(manifest) == {"demo": None, "other": None, "third": "1.0"}


def test_extract_dependency_specs_skips_non_string_pipfile_lock_versions(tmp_path):
    manifest = tmp_path / "Pipfile.lock"
    manifest.write_text(
        json.dumps({"default": {"demo": {"version": 2}, "other": {"version": "==1.0"}}}),
        encoding="utf-8",
    )
    assert extract_dependency_specs(manifest) == {"demo": None, "other": "==1.0"}
    assert extract_dependencies(manifest) == {"demo": None, "other": "1.0"}


def test_extract_dependencies_from_go_mod_skips_directives(tmp_path):
    manifest = tmp_path / "go.mod"
    manifest.write_text(
        "module example.com/app\n\ngo 1.22\n\nrequire github.com/acme/demo v1.2.3\n",
        encoding="utf-8",
    )
    assert extract_dependencies(manifest) == {"demo": "v1.2.3"}


def test_link_with_explicit_manifest(conn, docs_dir, tmp_path):
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\ndependencies = []\n", encoding="utf-8")
    lock = project / "uv.lock"
    lock.write_text('[[package]]\nname = "demo"\nversion = "5.0.0"\n', encoding="utf-8")
    info = link_library(conn, lib_id, project, manifest=lock)
    assert info.manifest_path == lock
    assert info.dep_version == "5.0.0"
    assert get_link(conn, lib_id)["manifest_path"] == str(lock)


def test_extract_pyproject_dependency_without_version(tmp_path):
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text('[project]\ndependencies = ["demo"]\n', encoding="utf-8")
    assert extract_dependencies(manifest) == {"demo": None}


def test_extract_pyproject_ignores_non_string_requirement(tmp_path):
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text("[project]\ndependencies = [123]\n", encoding="utf-8")
    assert extract_dependencies(manifest) == {}


def test_extract_pyproject_ignores_non_table_poetry_dependencies(tmp_path):
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text("[tool.poetry]\ndependencies = 'bad'\n", encoding="utf-8")
    assert extract_dependencies(manifest) == {}


def test_extract_requirements_skips_option_lines(tmp_path):
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("-e .\ndemo==1.0\n", encoding="utf-8")
    assert extract_dependencies(manifest) == {"demo": "1.0"}


def test_extract_package_lock_ignores_non_table_entries(tmp_path):
    manifest = tmp_path / "uv.lock"
    manifest.write_text('package = ["bad"]\n', encoding="utf-8")
    assert extract_dependencies(manifest) == {}


def test_link_with_missing_explicit_manifest(conn, docs_dir, tmp_path):
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ValueError, match="dependency manifest not found"):
        link_library(conn, lib_id, project, manifest=project / "nope.toml")


def test_normalize_name_follows_pep_503():
    from virtudoc.versions import normalize_name

    assert normalize_name(" Zope.Interface ") == "zope-interface"
    assert normalize_name("Demo_Tool") == "demo-tool"
    assert normalize_name("a..b") == "a-b"
    assert normalize_name("@scope/pkg") == "@scope/pkg"


def test_detect_manifest_prefers_conan_lock(tmp_path):
    (tmp_path / "conanfile.py").write_text("requires = ()\n", encoding="utf-8")
    assert detect_manifest(tmp_path).name == "conanfile.py"
    (tmp_path / "conan.lock").write_text('{"version": "0.5"}\n', encoding="utf-8")
    assert detect_manifest(tmp_path).name == "conan.lock"


def test_extract_from_conan_lock(tmp_path):
    manifest = tmp_path / "conan.lock"
    manifest.write_text(
        json.dumps(
            {
                "version": "0.5",
                "requires": [
                    "fmt/7.1.3#3f2a1b%1690000000",
                    "mongoose.proto.licenseplate/3.0.19"
                    "@mongoose+protobuf-schemas+licenseplate_schema/proto",
                    "zlib/1.2.13",
                    "openssl/[>=1.2 <2]@user/channel",
                ],
                "build_requires": ["cmake/3.20.0"],
                "python_requires": ["conan-tools/1.0.0@user/channel"],
            }
        ),
        encoding="utf-8",
    )
    assert extract_dependencies(manifest) == {
        "fmt": "7.1.3",
        "mongoose-proto-licenseplate": "3.0.19",
        "zlib": "1.2.13",
        "openssl": "1.2",
        "cmake": "3.20.0",
        "conan-tools": "1.0.0",
    }
    assert extract_dep_version(manifest, "absent") is None


def test_extract_from_conan_lock_graph_lock_nodes(tmp_path):
    manifest = tmp_path / "conan.lock"
    manifest.write_text(
        json.dumps(
            {
                "graph_lock": {
                    "nodes": {
                        "0": {"ref": "app/1.0.0", "path": "conanfile.py", "requires": ["1"]},
                        "1": {"ref": "fmt/7.1.3#rrev", "requires": ["2"]},
                        "2": {"ref": "zlib/1.2.13"},
                    }
                },
                "version": "0.4",
            }
        ),
        encoding="utf-8",
    )
    assert extract_dependencies(manifest) == {"fmt": "7.1.3", "zlib": "1.2.13"}


def test_extract_from_conanfile_py_requires_attribute(tmp_path):
    manifest = tmp_path / "conanfile.py"
    manifest.write_text(
        '''
from conans import ConanFile


class DemoConan(ConanFile):
    settings = "os"
    requires = (
        "mongoose.proto.licenseplate/3.0.19@mongoose/proto",
        "fmt/7.1.3",
        "mongoose.cpp-style-guide/0.0.2@mongoose+cpp-style-guide/config"
    )

    generators = "cmake", "gcc"
''',
        encoding="utf-8",
    )
    assert extract_dependencies(manifest) == {
        "mongoose-proto-licenseplate": "3.0.19",
        "fmt": "7.1.3",
        "mongoose-cpp-style-guide": "0.0.2",
    }


def test_extract_from_conanfile_py_single_requires_string(tmp_path):
    manifest = tmp_path / "conanfile.py"
    manifest.write_text(
        'from conan import ConanFile\n\n\nclass Demo(ConanFile):\n    requires = "fmt/7.1.3"\n',
        encoding="utf-8",
    )
    assert extract_dependencies(manifest) == {"fmt": "7.1.3"}


def test_extract_from_conanfile_py_self_requires(tmp_path):
    manifest = tmp_path / "conanfile.py"
    manifest.write_text(
        """
from conan import ConanFile


class Demo(ConanFile):
    def requirements(self):
        self.requires("fmt/8.1.1")
        self.requires("zlib/1.2.13@user/channel")
""",
        encoding="utf-8",
    )
    assert extract_dependencies(manifest) == {"fmt": "8.1.1", "zlib": "1.2.13"}


def test_extract_from_conanfile_txt(tmp_path):
    manifest = tmp_path / "conanfile.txt"
    manifest.write_text(
        """
[requires]
fmt/7.1.3

# zlib/1.2.11

zlib/1.2.13@user/channel
[generators]
cmake
""",
        encoding="utf-8",
    )
    assert extract_dependencies(manifest) == {"fmt": "7.1.3", "zlib": "1.2.13"}


def test_conan_lock_wins_over_conanfile_py(tmp_path):
    project = tmp_path / "conanproject"
    project.mkdir()
    (project / "conanfile.py").write_text(
        'from conan import ConanFile\n\n\nclass Demo(ConanFile):\n    requires = "fmt/8.1.1"\n',
        encoding="utf-8",
    )
    (project / "conan.lock").write_text(
        json.dumps({"version": "0.5", "requires": ["fmt/7.1.3#rrev"]}),
        encoding="utf-8",
    )
    resolved = resolve_project_version(project, "fmt")
    assert resolved is not None
    assert resolved.version == "7.1.3"
    assert resolved.manifest_path.name == "conan.lock"


def test_resolve_project_version_from_conanfile_py(tmp_path):
    project = tmp_path / "conanproject"
    project.mkdir()
    (project / "conanfile.py").write_text(
        """
from conans import ConanFile


class VehicleDetectorConan(ConanFile):
    requires = (
        "mongoose.cpp-libs.rabbitmq-lib/2.0.3@mongoose/stable",
        "mongoose.proto.licenseplate/3.0.19@mongoose/proto",
    )
""",
        encoding="utf-8",
    )
    resolved = resolve_project_version(project, "mongoose.proto.licenseplate")
    assert resolved is not None
    assert resolved.version == "3.0.19"
    assert resolved.manifest_path.name == "conanfile.py"
    assert resolve_project_version(project, "mongoose.cpp-libs.tracking") is None


def test_range_status_matches_a_version_inside_the_declared_range():
    assert range_status(">=2.0", "2.5") == "match"
    assert range_status(">=2.0,<3", "2.5") == "match"
    assert range_status("~=2.5", "2.9") == "match"
    assert range_status("!=1.9", "2.0") == "match"
    assert range_status(">=2.0", "2.0.0") == "match"


def test_range_status_flags_a_version_outside_the_declared_range():
    assert range_status(">=2.0", "1.9") == "mismatch"
    assert range_status(">=2.0,<3", "3.1") == "mismatch"
    assert range_status("~=2.5", "3.0") == "mismatch"


def test_range_status_keeps_exact_pin_semantics():
    assert range_status("==2.0", "2.0.0") == "match"
    assert range_status("==2.0", "2.5") == "mismatch"
    assert range_status("2.0", "2.5") == "mismatch"


def test_range_status_is_unknown_for_unevaluable_constraints():
    assert range_status("*", "2.5") == "unknown"
    assert range_status("latest", "2.5") == "unknown"
    assert range_status("git+https://example.com/demo.git", "2.5") == "unknown"
    assert range_status(">=2.0", "garbage") == "unknown"
    assert range_status(None, "2.5") == "unknown"


def test_resolve_project_version_keeps_the_declared_spec(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["demo>=2.0,<3"]\n', encoding="utf-8"
    )
    resolved = resolve_project_version(tmp_path, "demo")
    assert resolved is not None
    assert resolved.version == "2.0"
    assert resolved.spec == ">=2.0,<3"


def _project_with_requirement(tmp_path, requirement):
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text(f"{requirement}\n", encoding="utf-8")
    return project


def test_mismatch_payload_accepts_docs_inside_the_declared_range(conn, docs_dir, tmp_path):
    lib_id = add_library(conn, docs_dir, "demo", version="2.5")
    index_library(conn, lib_id, Chunker())
    project = _project_with_requirement(tmp_path, "demo>=2.0")
    assert mismatch_payload(conn, lib_id, project_path=project) is None


def test_mismatch_payload_accepts_docs_inside_a_compound_range(conn, docs_dir, tmp_path):
    lib_id = add_library(conn, docs_dir, "demo", version="2.5")
    index_library(conn, lib_id, Chunker())
    project = _project_with_requirement(tmp_path, "demo>=2.0,<3")
    assert mismatch_payload(conn, lib_id, project_path=project) is None


def test_mismatch_payload_warns_when_docs_fall_outside_the_range(conn, docs_dir, tmp_path):
    lib_id = add_library(conn, docs_dir, "demo", version="1.9")
    index_library(conn, lib_id, Chunker())
    project = _project_with_requirement(tmp_path, "demo>=2.0")
    payload = mismatch_payload(conn, lib_id, project_path=project)
    assert payload is not None
    assert payload["documentation_version"] == "1.9"
    assert payload["project_version"] == "2.0"


def test_mismatch_payload_keeps_exact_pin_semantics(conn, docs_dir, tmp_path):
    lib_id = add_library(conn, docs_dir, "demo", version="2.5")
    index_library(conn, lib_id, Chunker())
    project = _project_with_requirement(tmp_path, "demo==2.0")
    payload = mismatch_payload(conn, lib_id, project_path=project)
    assert payload is not None
    assert payload["documentation_version"] == "2.5"


def test_mismatch_payload_ignores_an_unevaluable_constraint(conn, docs_dir, tmp_path):
    lib_id = add_library(conn, docs_dir, "demo", version="2.5")
    index_library(conn, lib_id, Chunker())
    project = tmp_path / "project"
    project.mkdir()
    (project / "package.json").write_text(
        json.dumps({"dependencies": {"demo": "*"}}), encoding="utf-8"
    )
    assert mismatch_payload(conn, lib_id, project_path=project) is None


def test_mismatch_payload_range_survives_the_link(conn, docs_dir, tmp_path):
    lib_id = add_library(conn, docs_dir, "demo", version="2.5")
    index_library(conn, lib_id, Chunker())
    project = _project_with_requirement(tmp_path, "demo>=2.0")
    info = link_library(conn, lib_id, project)
    assert info.dep_version == "2.0"
    assert mismatch_payload(conn, lib_id) is None
