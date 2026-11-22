from virtudoc.cli import main
from virtudoc.config import Config
from virtudoc.db import open_db
from virtudoc.versions import get_link


def _add_demo(home, docs_dir, version="1.0"):
    assert main(["add", str(docs_dir), "--name", "demo", "--version", version]) == 0


def test_init_reports_matches_without_linking(home, docs_dir, tmp_path, capsys):
    _add_demo(home, docs_dir)
    capsys.readouterr()
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==2.0.0\nrequests==2.31.0\n", encoding="utf-8")

    assert main(["init", str(project)]) == 0
    out = capsys.readouterr().out
    assert "manifests scanned: requirements.txt" in out
    assert "dependencies found: 2" in out
    assert "local/demo" in out
    assert "docs 1.0" in out
    assert "project 2.0.0" in out
    assert "mismatch" in out
    assert "unmatched dependencies (1)" in out
    assert "requests 2.31.0" in out
    assert "virtudoc add <docs-path> --name <dependency>" in out
    assert "links: unchanged" in out

    with open_db(Config().db_path) as conn:
        assert get_link(conn, "local/demo") is None


def test_init_links_only_with_explicit_flag(home, docs_dir, tmp_path, capsys):
    _add_demo(home, docs_dir)
    capsys.readouterr()
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==2.0.0\n", encoding="utf-8")

    assert main(["init", str(project), "--link"]) == 0
    out = capsys.readouterr().out
    assert "linked:" in out
    assert "local/demo ->" in out
    assert "2.0.0" in out

    with open_db(Config().db_path) as conn:
        link = get_link(conn, "local/demo")
    assert link is not None
    assert link["dep_version"] == "2.0.0"
    assert link["manifest_path"].endswith("requirements.txt")


def test_init_agents_hint_writes_the_project_file(home, docs_dir, tmp_path, capsys):
    _add_demo(home, docs_dir)
    capsys.readouterr()
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==2.0.0\n", encoding="utf-8")

    assert main(["init", str(project), "--agents-hint"]) == 0
    out = capsys.readouterr().out
    assert f"agents hint written to {project / 'AGENTS.md'}" in out
    assert "find-library" in (project / "AGENTS.md").read_text(encoding="utf-8")


def test_init_scans_every_manifest(home, docs_dir, tmp_path, capsys):
    _add_demo(home, docs_dir, version="2.0.0")
    capsys.readouterr()
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\ndependencies = ["demo>=2.0.0"]\n', encoding="utf-8"
    )
    (project / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")

    assert main(["init", str(project)]) == 0
    out = capsys.readouterr().out
    assert "pyproject.toml, requirements.txt" in out
    assert "docs 2.0.0  project 2.0.0  match" in out
    assert "requests 2.31.0 (requirements.txt)" in out


def test_init_reports_unknown_version_status(home, docs_dir, tmp_path, capsys):
    assert main(["add", str(docs_dir), "--name", "demo"]) == 0
    capsys.readouterr()
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==1.0.0\n", encoding="utf-8")

    assert main(["init", str(project)]) == 0
    out = capsys.readouterr().out
    assert "docs unknown  project 1.0.0  unknown" in out


def test_init_defaults_to_current_directory(home, docs_dir, tmp_path, capsys, monkeypatch):
    _add_demo(home, docs_dir)
    capsys.readouterr()
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("demo==1.0.0\n", encoding="utf-8")
    monkeypatch.chdir(project)

    assert main(["init"]) == 0
    out = capsys.readouterr().out
    assert f"project: {project}" in out
    assert "match" in out


def test_init_without_registered_libraries(home, tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    (project / "package.json").write_text(
        '{"dependencies": {"demo-lib": "^1.2.0"}}', encoding="utf-8"
    )

    assert main(["init", str(project)]) == 0
    out = capsys.readouterr().out
    assert "matched libraries: none" in out
    assert "demo-lib 1.2.0" in out
    assert "no registered library matches this project" in out


def test_init_missing_project_directory(home, tmp_path, capsys):
    assert main(["init", str(tmp_path / "nope")]) == 1
    assert "project directory not found" in capsys.readouterr().err


def test_init_without_manifest(home, tmp_path, capsys):
    project = tmp_path / "empty"
    project.mkdir()

    assert main(["init", str(project)]) == 1
    err = capsys.readouterr().err
    assert "no dependency manifest found" in err
    assert "pyproject.toml" in err


def test_link_matches_reports_errors(conn, tmp_path, monkeypatch):
    from virtudoc.onboarding import (
        DependencyEntry,
        LibraryMatch,
        ProjectScan,
        link_matches,
    )

    manifest = tmp_path / "requirements.txt"
    manifest.write_text("demo==1.0\n", encoding="utf-8")
    scan = ProjectScan(
        project_path=tmp_path,
        matches=[
            LibraryMatch(
                library_id="local/demo",
                library_name="demo",
                doc_version="1.0",
                dependency=DependencyEntry("demo", "1.0", manifest),
                status="match",
            )
        ],
    )

    def fail(*args, **kwargs):
        raise ValueError("manifest broke")

    monkeypatch.setattr("virtudoc.onboarding.link_library", fail)
    linked, errors = link_matches(conn, scan)
    assert linked == []
    assert errors == ["local/demo: manifest broke"]


def test_format_report_without_manifests_or_dependencies(tmp_path):
    from virtudoc.onboarding import ProjectScan, format_report

    lines = format_report(ProjectScan(project_path=tmp_path), [], False)
    assert "manifests scanned: none" in lines
    assert "matched libraries: none" in lines
    assert "unmatched dependencies: none" in lines
    assert "links: unchanged (pass --link to link matched libraries)" in lines
    assert "linked: none" in format_report(ProjectScan(project_path=tmp_path), [], True)


def test_format_report_limits_unmatched_hints(tmp_path):
    from virtudoc.onboarding import DependencyEntry, ProjectScan, format_report

    manifest = tmp_path / "requirements.txt"
    scan = ProjectScan(
        project_path=tmp_path,
        unmatched=[DependencyEntry(f"dep{index}", "1.0", manifest) for index in range(12)],
    )
    lines = format_report(scan, [], False)
    assert "unmatched dependencies (12):" in lines
    assert "  ... and 2 more" in lines
