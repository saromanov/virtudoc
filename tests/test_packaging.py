from __future__ import annotations

import tomllib
from pathlib import Path

import virtudoc

ROOT = Path(__file__).resolve().parents[1]


def test_version_has_one_source():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "version" not in pyproject["project"]
    assert pyproject["project"]["dynamic"] == ["version"]
    assert pyproject["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "virtudoc.__version__"}
    assert virtudoc.__version__


def test_release_workflow_uses_trusted_publishing():
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "pypa/gh-action-pypi-publish" in workflow
    assert "id-token: write" in workflow
    assert "environment:" in workflow
    assert "PYPI_TOKEN" not in workflow
    assert "password:" not in workflow
