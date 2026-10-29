"""Restricted issues stay private, and ids from the server never become arbitrary paths."""

from __future__ import annotations

import copy

import pytest

from specivo_jira_migration.state import StateDir, StateError


def _issue_response(cloud, issue_id: str) -> dict:
    for interaction in cloud.interactions:
        if interaction.method == "GET" and interaction.path == f"/rest/api/3/issue/{issue_id}":
            return copy.deepcopy(interaction.responses[0])
    raise AssertionError(f"no fixture for issue {issue_id}")


class TestRestrictedIssues:
    async def test_an_issue_with_a_security_level_is_private(self, cloud, run_export):
        response = _issue_response(cloud, "10001")
        response["json"]["fields"]["security"] = {"id": "10100", "name": "Staff only"}
        cloud.override("GET", "/rest/api/3/issue/10001", response)
        _, bundle = await run_export()
        issue = bundle.by_ref("issues")["10001"]
        assert issue["is_private"] is True
        assert issue["extra"]["restriction"] == {"security_level": {"id": "10100", "name": "Staff only"}}

    async def test_an_issue_with_restrictions_is_private(self, cloud, run_export):
        response = _issue_response(cloud, "10001")
        response["json"]["fields"]["issuerestriction"] = {
            "issuerestrictions": {"projectrole": [{"id": "10002"}]},
            "shouldDisplay": True,
        }
        cloud.override("GET", "/rest/api/3/issue/10001", response)
        _, bundle = await run_export()
        assert bundle.by_ref("issues")["10001"]["is_private"] is True

    async def test_an_unrestricted_issue_is_not_private(self, cloud, run_export):
        response = _issue_response(cloud, "10001")
        response["json"]["fields"]["security"] = None
        response["json"]["fields"]["issuerestriction"] = {"issuerestrictions": {}, "shouldDisplay": True}
        cloud.override("GET", "/rest/api/3/issue/10001", response)
        _, bundle = await run_export()
        issue = bundle.by_ref("issues")["10001"]
        assert issue.get("is_private", False) is False
        assert "restriction" not in issue["extra"]


class TestIssueIds:
    @pytest.mark.parametrize("bad", ["../escape", "10001/../../x", "", "1e3", "10001.json", "-1"])
    def test_the_state_refuses_a_name_that_is_not_a_numeric_id(self, tmp_path, bad):
        state = StateDir(tmp_path / "state")
        with pytest.raises(StateError):
            state.issue_name(bad)

    async def test_a_search_result_with_a_bad_id_is_skipped(self, cloud, run_export, tmp_path):
        cloud.override(
            "POST",
            "/rest/api/3/search/jql",
            {"status": 200, "json": {"issues": [{"id": "../../outside"}, {"id": "10001"}], "isLast": True}},
        )
        _, bundle = await run_export()
        assert "10001" in bundle.by_ref("issues")
        assert not list(tmp_path.rglob("outside*"))
