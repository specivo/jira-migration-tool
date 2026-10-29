"""Packaging rules the bundle's content depends on."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _requirements() -> dict[str, Requirement]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    return {Requirement(line).name: Requirement(line) for line in project["dependencies"]}


def test_the_converter_is_capped_below_its_next_version():
    """A new jira2markdown may change every converted text, and the tool uses its grammar directly."""
    specifier = _requirements()["jira2markdown"].specifier
    assert any(spec.operator == "<" and spec.version == "0.6" for spec in specifier), str(specifier)


def test_no_jira_sdk_dependency():
    """The tool talks to Jira's REST API with its own small client."""
    assert set(_requirements()) == {"specivo-migration-sdk", "httpx", "jira2markdown"}


def test_ci_workflow_can_only_read_the_repository():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert workflow["permissions"] == {"contents": "read"}


def test_the_sdk_is_pinned_to_a_release_tag():
    """A release depends on a tagged SDK, never on a path or a moving branch."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    sdk = _requirements()["specivo-migration-sdk"]
    assert sdk.url is not None and sdk.url.startswith("git+https://github.com/specivo/migration-sdk@v"), sdk
    assert "sources" not in pyproject.get("tool", {}).get("uv", {})
