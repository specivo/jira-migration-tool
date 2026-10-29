"""The command line: options, environment, exit status, and what it prints."""

from __future__ import annotations

import asyncio
import json

import pytest

from specivo_jira_migration.cli import build_parser, main
from tests.conftest import CLOUD_URL, DC_URL
from tests.fake_jira import no_sleep

SECRET = "cloud-token-for-tests"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PAT", "JIRA_PROJECTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)


def _cloud_env(monkeypatch):
    monkeypatch.setenv("JIRA_URL", CLOUD_URL)
    monkeypatch.setenv("JIRA_EMAIL", "admin@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", SECRET)


class TestUsage:
    def test_a_url_is_needed(self, tmp_path, capsys):
        assert main(["--out", str(tmp_path / "b"), "--project", "ACME"]) == 2
        assert "give --url, or set JIRA_URL" in capsys.readouterr().err

    def test_credentials_are_needed(self, tmp_path, capsys):
        assert main(["--url", CLOUD_URL, "--out", str(tmp_path / "b"), "--project", "ACME"]) == 2
        assert "--email and --api-token" in capsys.readouterr().err

    def test_a_project_is_needed(self, tmp_path, monkeypatch, capsys):
        _cloud_env(monkeypatch)
        assert main(["--out", str(tmp_path / "b")]) == 2
        assert "--project KEY" in capsys.readouterr().err

    def test_both_kinds_of_credentials_are_refused(self, tmp_path, capsys):
        argv = ["--url", CLOUD_URL, "--out", str(tmp_path / "b"), "--project", "ACME"]
        assert main([*argv, "--email", "admin@example.com", "--api-token", "x", "--pat", "y"]) == 2
        assert "not both" in capsys.readouterr().err

    def test_both_kinds_in_the_environment_are_refused(self, tmp_path, monkeypatch, capsys):
        _cloud_env(monkeypatch)
        monkeypatch.setenv("JIRA_PAT", "y")
        assert main(["--out", str(tmp_path / "b"), "--project", "ACME"]) == 2
        assert "unset the ones for the other kind" in capsys.readouterr().err

    def test_a_bad_status_category(self, tmp_path):
        with pytest.raises(SystemExit):
            main(["--out", str(tmp_path / "b"), "--status-category-map", "Done=finished"])

    def test_an_unknown_project_is_a_usage_error(self, cloud, tmp_path, monkeypatch, capsys):
        _cloud_env(monkeypatch)
        assert main(["--out", str(tmp_path / "b"), "--project", "NOPE"]) == 2
        assert "no project NOPE" in capsys.readouterr().err


class TestAttachmentSize:
    @pytest.mark.parametrize(("value", "size"), [("100M", 100 * 1024 * 1024), ("2G", 2 * 1024**3), ("0", None)])
    def test_sizes(self, tmp_path, value, size):
        args = build_parser().parse_args(["--out", str(tmp_path), "--max-attachment-size", value])
        assert args.max_attachment_size == size

    def test_the_default_is_100_megabytes(self, tmp_path):
        assert build_parser().parse_args(["--out", str(tmp_path)]).max_attachment_size == 100 * 1024 * 1024

    def test_a_bad_size(self, tmp_path):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--out", str(tmp_path), "--max-attachment-size", "big"])


class TestRun:
    def test_cloud_from_the_environment(self, cloud, tmp_path, monkeypatch, capsys):
        _cloud_env(monkeypatch)
        monkeypatch.setenv("JIRA_PROJECTS", "ACME,SCRUM")
        out = tmp_path / "bundle"
        assert main(["--out", str(out), "--validate"]) == 0
        captured = capsys.readouterr()
        assert captured.out.splitlines()[0] == str(out)
        assert "The bundle is valid." in captured.out
        assert "ACME: listing issues" in captured.err
        assert "SCRUM: 2 issues read" in captured.err
        assert "Users without a visible email address: 2" in captured.err
        assert "Downloading 8 of 8 attachments" in captured.err
        assert "Attachment files: 0.0 MB" in captured.err
        assert SECRET not in captured.out + captured.err
        assert "synthetic-media-token" not in captured.out + captured.err
        assert json.loads((out / "manifest.json").read_text())["counts"]["issues"] == 6

    def test_data_center_with_a_token(self, datacenter, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("JIRA_PAT", "dc-token-for-tests")
        out = tmp_path / "bundle"
        assert main(["--url", DC_URL, "--project", "sup", "--out", str(out), "--quiet"]) == 0
        assert capsys.readouterr().out == ""
        assert json.loads((out / "manifest.json").read_text())["source"]["instance"] == "jira.example.com"

    def test_instance_override(self, cloud, tmp_path, monkeypatch):
        _cloud_env(monkeypatch)
        out = tmp_path / "bundle"
        assert main(["--out", str(out), "--project", "ACME", "--instance", "jira-old.example.com", "--quiet"]) == 0
        assert json.loads((out / "manifest.json").read_text())["source"]["instance"] == "jira-old.example.com"

    def test_a_failure_says_how_to_continue_and_keeps_secrets_out(self, cloud, tmp_path, monkeypatch, capsys):
        _cloud_env(monkeypatch)
        cloud.override("GET", "/rest/api/3/issue/10006", {"status": 500, "json": {}})
        out = tmp_path / "bundle"
        assert main(["--out", str(out), "--project", "ACME"]) == 1
        captured = capsys.readouterr()
        assert "HTTP 500" in captured.err
        assert "Run the same command again to continue" in captured.err
        assert SECRET not in captured.out + captured.err
        assert main(["--out", str(out), "--project", "ACME", "--quiet"]) == 0

    def test_credentials_in_the_url_are_refused(self, tmp_path, monkeypatch, capsys):
        _cloud_env(monkeypatch)
        url = "https://admin:secret@example.atlassian.net"
        assert main(["--url", url, "--out", str(tmp_path / "b"), "--project", "ACME"]) == 2
        assert "never in the URL" in capsys.readouterr().err


def test_the_fixture_bundle_script(tmp_path, capsys):
    from tests.write_fixture_bundle import main as write

    assert write([str(tmp_path / "bundle")]) == 0
    assert "The bundle is valid." in capsys.readouterr().out
