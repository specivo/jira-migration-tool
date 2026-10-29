"""The whole export against the hand-written fixture sites (tests/fixtures)."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

import pytest
from specivo_migration_sdk import validate_bundle
from specivo_migration_sdk.format import REFERENCE_ATTRIBUTES
from specivo_migration_sdk.report import ProblemCode, Severity, ValidationReport

from specivo_jira_migration.convert import MEDIA_NOT_IN_BUNDLE
from specivo_jira_migration.export import ExportError
from specivo_jira_migration.mapping import LINKS_GONE, PARENTS_GONE, PARENTS_UNKNOWN, REMOTE_LINKS
from specivo_jira_migration.state import state_path_for
from tests.conftest import DC_AUTH, DC_URL
from tests.fake_jira import FakeJira

ALICE = "712020:00000000-0000-4000-8000-00000000a11c"
BOB = "712020:00000000-0000-4000-8000-000000000b0b"
CAROL = "712020:00000000-0000-4000-8000-00000000ca01"


def _values(detail: dict) -> tuple[str, str | None, str | None]:
    """A journal detail's values: the refs of a change naming a record, otherwise its values."""
    if detail["property"] == "attr" and detail["prop_key"] in REFERENCE_ATTRIBUTES:
        assert detail.get("old_ref") or not detail.get("old_value"), detail
        assert detail.get("new_ref") or not detail.get("new_value"), detail
        return detail["prop_key"], detail.get("old_ref"), detail.get("new_ref")
    return detail["prop_key"], detail.get("old_value"), detail.get("new_value")


DAN = "712020:00000000-0000-4000-8000-000000000da9"
ERIN = "712020:00000000-0000-4000-8000-00000000e417"
BOT = "557058:00000000-0000-4000-8000-0000000a0700"


def _only_external_refs(report: ValidationReport) -> list[str]:
    """The files with an ``external_ref`` warning; fails on any other problem."""
    assert report.ok, report.format_text()
    others = [p for p in report.problems if (p.severity, p.code) != (Severity.WARNING, ProblemCode.EXTERNAL_REF)]
    assert others == [], report.format_text()
    return sorted({p.file for p in report.problems})


def _issue_json(fake: FakeJira, issue_id: str, api: str = "3") -> dict:
    """A copy of what the cassette answers for one issue, to change and play back with ``override``."""
    for interaction in fake.interactions:
        if interaction.method == "GET" and interaction.path == f"/rest/api/{api}/issue/{issue_id}":
            return copy.deepcopy(interaction.responses[-1]["json"])
    raise KeyError(issue_id)


class TestCloud:
    async def test_the_bundle_is_valid(self, cloud, run_export):
        summary, bundle = await run_export()
        report = validate_bundle(bundle.root)
        # ACME-3's parent and a link of ACME-5 are OPS issues, in another bundle.
        assert _only_external_refs(report) == ["issues.jsonl", "relations.jsonl"]
        assert summary.counts["issues"] == 6
        assert summary.counts["journals"] == 11

    async def test_a_bundle_of_every_project_refers_to_nothing_outside(self, cloud, run_export):
        _, bundle = await run_export(["ACME", "SCRUM", "OPS"])
        report = validate_bundle(bundle.root)
        assert report.ok and not report.problems, report.format_text()

    async def test_manifest(self, cloud, run_export):
        _, bundle = await run_export()
        assert bundle.manifest["source"] == {
            "system": "jira",
            "instance": "example.atlassian.net",
            "version": "1001.0.0-SNAPSHOT",
        }
        assert bundle.manifest["generator"]["name"] == "specivo-jira-migration"

    async def test_projects_of_both_styles(self, cloud, run_export):
        _, bundle = await run_export()
        projects = bundle.by_ref("projects")
        assert projects["10000"]["identifier"] == "acme"
        assert projects["10000"]["name"] == "Acme Web Shop"
        assert projects["10000"]["description"] == "The public web shop."
        assert projects["10000"]["extra"]["style"] == "classic"
        assert projects["10001"]["extra"]["style"] == "next-gen"
        assert "description" not in projects["10001"]

    async def test_issues_are_keyed_by_id_and_ordered_by_number(self, cloud, run_export):
        _, bundle = await run_export()
        issues = bundle.get("issues")
        assert [i["extra"]["jira_key"] for i in issues] == [
            "ACME-1",
            "ACME-2",
            "ACME-3",
            "ACME-5",
            "SCRUM-1",
            "SCRUM-2",
        ]
        assert [i["source_ref"] for i in issues] == ["10001", "10002", "10003", "10006", "10101", "10102"]

    async def test_issue_fields(self, cloud, run_export):
        _, bundle = await run_export()
        issue = bundle.by_ref("issues")["10001"]
        assert issue["subject"] == "Login fails on Safari"
        assert issue["tracker_ref"] == "10004"
        assert issue["status_ref"] == "10001"
        assert issue["priority_ref"] == "2"
        assert issue["author_ref"] == ALICE
        assert issue["assigned_to_ref"] == BOB
        assert issue["created_at"] == "2024-02-01T10:00:00+07:00"
        assert issue["updated_at"] == "2024-02-05T16:30:00+07:00"
        assert issue["closed_at"] == "2024-02-05T16:30:00+07:00"
        assert issue["due_date"] == "2024-02-10"
        assert issue["extra"] == {
            "jira_key": "ACME-1",
            # Labels are tags; one too long for a tag is kept here.
            "labels": ["x" * 70],
            "resolution": "Done",
            "components": ["Web", "API"],
            "fix_versions": ["2.0", "1.0", "0.9"],
            "sprints": ["ACME Sprint 1", "ACME Sprint 2"],
        }

    async def test_description_markup_and_references(self, cloud, run_export):
        _, bundle = await run_export()
        text = bundle.by_ref("issues")["10001"]["description"]
        assert text.startswith("## Steps to reproduce\n\n1. Open the login page in Safari\n2. Enter")
        assert "3. Press **Sign in**" in text
        assert '```javascript\nconsole.error("ACME-99 stays as it is inside code");\n```' in text
        assert "| Browser | Result |\n| --- | --- |\n| Safari 17 | fails |" in text
        assert "Related to {{issue:10002}} and {{issue:10101}}; see also {{issue:10003|the duplicate}}." in text
        # A renamed key Jira resolves to an issue of the bundle; a string that is no key stays.
        assert "The old key {{issue:10102}} still works, and UTF-8 is not a key." in text
        assert text.endswith(f"{{{{user:{ALICE}}}}} please check with {{{{user:{DAN}}}}}.")

    async def test_a_deleted_key_stays_text(self, cloud, run_export):
        _, bundle = await run_export()
        assert "- ACME-4 (deleted)" in bundle.by_ref("issues")["10006"]["description"]

    async def test_cloud_text_is_read_as_adf_through_v3(self, cloud, run_export):
        _, bundle = await run_export()
        assert cloud.calls("GET", "/rest/api/3/issue/10001")
        assert not [r for r in cloud.requests if r.url.path.startswith("/rest/api/2/issue/1")]
        # A smart link to an issue of the site is a reference, like a key.
        assert bundle.by_ref("issues")["10006"]["description"].startswith("- {{issue:10001}}\n- {{issue:10002}}\n")

    async def test_a_browse_link_outside_the_bundle_is_looked_up(self, cloud, run_export):
        _, bundle = await run_export(limit=1)
        # ACME-3 is linked only by its /browse/ page and is past the limit: Jira is asked for it.
        assert cloud.calls("GET", "/rest/api/2/issue/ACME-3")
        assert "{{issue:10003|the duplicate}}" in bundle.by_ref("issues")["10001"]["description"]

    async def test_an_issue_without_a_priority(self, cloud, run_export):
        _, bundle = await run_export()
        assert bundle.by_ref("issues")["10102"]["priority_ref"] == "none"
        assert {"source_ref": "none", "name": "No priority", "position": 3} in bundle.lookups["priorities"]

    async def test_an_issue_deleted_after_the_search_is_left_out(self, cloud, run_export):
        _, bundle = await run_export()
        assert "10004" not in bundle.by_ref("issues")

    async def test_lookups(self, cloud, run_export):
        _, bundle = await run_export()
        statuses = {s["source_ref"]: s for s in bundle.lookups["statuses"]}
        assert {ref: s["category"] for ref, s in statuses.items()} == {
            "3": "active",
            "10000": "backlog",
            "10001": "closed",
            # Only the change history names it.
            "10002": "active",
            "10010": "backlog",
            "10011": "active",
        }
        trackers = {t["source_ref"]: t for t in bundle.lookups["trackers"]}
        assert trackers["10004"]["name"] == "Bug"
        # A team-managed project's own types are separate trackers.
        assert trackers["10012"]["name"] == "Task"
        assert trackers["10012"]["extra"]["project_id"] == "10001"
        assert trackers["10013"]["extra"]["subtask"] is True
        # The status issues of a type started in.
        assert trackers["10004"]["default_status_ref"] == "10000"
        assert trackers["10012"]["default_status_ref"] == "10010"
        priorities = {p["source_ref"]: p for p in bundle.lookups["priorities"]}
        assert priorities["3"].get("is_default") is True
        assert [p["name"] for p in bundle.lookups["priorities"]] == ["High", "Medium", "Low", "No priority"]

    async def test_status_categories_can_be_overridden(self, cloud, run_export):
        _, bundle = await run_export(status_category_map={"Done": "done", "in review": "backlog"})
        statuses = {s["name"]: s["category"] for s in bundle.lookups["statuses"]}
        assert statuses["Done"] == "done"
        assert statuses["In Review"] == "backlog"


class TestCloudUsers:
    async def test_users(self, cloud, run_export):
        summary, bundle = await run_export()
        users = bundle.by_ref("users")
        # Erin is named only by a custom field.
        assert set(users) == {ALICE, BOB, CAROL, DAN, ERIN}
        assert users[ALICE]["login"] == "alice"
        assert users[ALICE]["email"] == "alice@example.com"
        assert summary.hidden_emails == 2

    async def test_a_hidden_email_is_left_out(self, cloud, run_export):
        _, bundle = await run_export()
        bob = bundle.by_ref("users")[BOB]
        assert "email" not in bob
        assert bob["login"] == "bob.example"
        assert bob["display_name"] == "Bob Example"

    async def test_an_inactive_user_is_carried_locked(self, cloud, run_export):
        _, bundle = await run_export()
        assert bundle.by_ref("users")[CAROL]["status"] == "locked"

    async def test_a_user_only_mentioned_is_looked_up(self, cloud, run_export):
        _, bundle = await run_export()
        assert cloud.calls("GET", "/rest/api/2/user")[0].url.params["accountId"] == DAN
        assert bundle.by_ref("users")[DAN]["language"] == "en"

    async def test_app_accounts_are_left_out(self, cloud, run_export):
        _, bundle = await run_export()
        assert BOT not in bundle.by_ref("users")
        comment = bundle.by_ref("journals")["comment:10101"]
        assert "user_ref" not in comment
        assert any(what.startswith("App accounts") for what in bundle.not_carried())


class TestCloudJournals:
    async def test_truncated_embedded_lists_are_read_in_full(self, cloud, run_export):
        _, bundle = await run_export()
        refs = [j["source_ref"] for j in bundle.get("journals") if j["issue_ref"] == "10001"]
        assert refs == [
            "history:20001",
            "comment:10100",
            "history:20002",
            "comment:10101",
            "history:20003",
            "comment:10102",
            "history:20004",
        ]
        assert [r.url.params["startAt"] for r in cloud.calls("GET", "/rest/api/3/issue/10001/changelog")] == ["0", "2"]
        assert [r.url.params["startAt"] for r in cloud.calls("GET", "/rest/api/3/issue/10001/comment")] == ["0", "2"]

    async def test_complete_embedded_lists_are_not_read_again(self, cloud, run_export):
        await run_export()
        assert cloud.calls("GET", "/rest/api/3/issue/10002/changelog") == []
        assert cloud.calls("GET", "/rest/api/3/issue/10002/comment") == []

    async def test_comments(self, cloud, run_export):
        _, bundle = await run_export()
        journals = bundle.by_ref("journals")
        first = journals["comment:10100"]
        assert first["user_ref"] == BOB
        assert first["created_at"] == "2024-02-01T11:00:00+07:00"
        assert first["notes"] == "I can reproduce this on Safari 17. The console log is attached."
        restricted = journals["comment:10102"]
        assert restricted["is_private"] is True
        assert restricted["extra"] == {
            "visibility": {"type": "role", "value": "Developers"},
            "updated_at": "2024-02-05T16:10:00+07:00",
        }

    async def test_change_history(self, cloud, run_export):
        _, bundle = await run_export()
        journals = bundle.by_ref("journals")

        def details(ref):
            return [_values(d) for d in journals[ref]["details"]]

        assert details("history:20001") == [("tracker_id", "10002", "10004"), ("priority_id", "3", "2")]
        # labels changed too, and is counted instead.
        assert details("history:20002")[:2] == [("status_id", "10000", "3"), ("assigned_to_id", ALICE, BOB)]
        assert details("history:20003")[:4] == [
            ("subject", "Login fails", "Login fails on Safari"),
            ("status_id", "3", "10002"),
            # Attachments added and removed, the removed one no longer in Jira.
            ("10201", None, "screenshot.png"),
            ("10199", "old-screenshot.png", None),
        ]
        assert [d["property"] for d in journals["history:20003"]["details"]][2:4] == ["attachment", "attachment"]
        assert details("history:20005") == [("status_id", "10000", "3"), ("assigned_to_id", CAROL, None)]
        assert journals["history:20004"]["user_ref"] == CAROL
        # The names Jira shows go with the refs, for a record that does not come across.
        texts = [(d.get("old_value"), d.get("new_value")) for d in journals["history:20002"]["details"]]
        assert texts[:2] == [("To Do", "In Progress"), ("Alice Example", "Bob Example")]
        assert journals["history:20004"]["created_at"] == "2024-02-05T16:30:00+07:00"

    async def test_what_is_not_carried_over_is_counted(self, cloud, run_export):
        _, bundle = await run_export()
        counted = bundle.not_carried()
        assert not any(what.startswith("Change history of attachment") for what in counted)
        assert "Issue links" not in counted
        assert "Worklogs" not in counted
        assert not any(what.startswith("Parent links (") or "Epic Link" in what for what in counted)
        assert not any(what.startswith(("Issue links to", "Parents", "Epic Links")) for what in counted)
        assert not any(what.startswith(("Components set", "Fix versions set")) for what in counted)
        # Carried: the Sprint field as the issue's sprint, Story point estimate as a custom field.
        assert not any(what.startswith(("Field Sprint", "Field Story point")) for what in counted)
        # Left out: Jira's own fields, and a field of an app's type.
        assert counted["Field Rank (customfield_10019): values on issues"] == 6
        assert counted["Field Team (customfield_10001): values on issues"] == 1
        assert counted["Field Development (customfield_10000): values on issues"] == 1
        assert counted["Field Gauge (customfield_10060): values on issues"] == 1
        assert counted["Change history of Rank"] == 1
        # Specivo keeps no history of tags.
        assert counted["Change history of labels"] == 1
        assert counted["Change history of resolution"] == 1


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _fixture_bytes(name: str, path: str) -> bytes:
    data = json.loads((Path(__file__).parent / "fixtures" / name).read_text())
    for item in data["interactions"]:
        if item["request"]["path"] == path:
            return base64.b64decode(item["responses"][0]["base64"])
    raise KeyError(path)


MEDIA_HOST = "api.media.example.com"


def _media_path(att_id: str) -> str:
    return f"/file/5e1f0000-0000-4000-8000-0000000{att_id}/binary"


class TestCloudLinks:
    async def test_each_link_is_one_relation_in_its_canonical_direction(self, cloud, run_export):
        _, bundle = await run_export()
        relations = bundle.by_ref("relations")
        # Listed on both of its issues, written once.
        assert [r["source_ref"] for r in bundle.get("relations")] == ["10300", "10301", "10302", "10303", "10304"]
        assert relations["10300"] == {
            "source_ref": "10300",
            "from_ref": "10001",
            "to_ref": "10002",
            "relation_type": "blocks",
        }
        # SCRUM-1 duplicates ACME-2: across projects, both in the bundle.
        assert (relations["10301"]["from_ref"], relations["10301"]["to_ref"]) == ("10101", "10002")
        assert relations["10301"]["relation_type"] == "duplicates"
        # ACME-3 clones ACME-1: ACME-1 was copied to ACME-3.
        assert (relations["10302"]["from_ref"], relations["10302"]["to_ref"]) == ("10001", "10003")
        assert relations["10302"]["relation_type"] == "copied_to"

    async def test_an_unknown_link_type_relates_and_keeps_its_names(self, cloud, run_export):
        _, bundle = await run_export()
        relation = bundle.by_ref("relations")["10303"]
        assert (relation["from_ref"], relation["to_ref"], relation["relation_type"]) == ("10006", "10002", "relates")
        names = {"jira_link_type": "Problem/Incident", "outward": "causes", "inward": "is caused by"}
        assert relation["extra"] == names
        assert bundle.not_carried()["Issue links of type Problem/Incident (carried as relates)"] == 1

    async def test_a_link_to_an_issue_of_another_bundle_is_a_relation(self, cloud, run_export):
        _, bundle = await run_export()
        # ACME-5 relates to OPS-9, which this bundle does not hold: the far end is its id.
        assert bundle.by_ref("relations")["10304"] == {
            "source_ref": "10304",
            "from_ref": "10006",
            "to_ref": "10999",
            "relation_type": "relates",
        }
        assert "jira_links" not in bundle.by_ref("issues")["10006"]["extra"]
        assert LINKS_GONE not in bundle.not_carried()

    async def test_a_link_between_two_bundles_is_written_into_both(self, cloud, run_export):
        _, acme = await run_export(["ACME"])
        _, scrum = await run_export(["SCRUM"])
        _, ops = await run_export(["OPS"])
        # SCRUM-1 duplicates ACME-2; ACME-5 relates to OPS-9: each in both bundles, the same record.
        assert acme.by_ref("relations")["10301"] == scrum.by_ref("relations")["10301"]
        assert acme.by_ref("relations")["10301"]["from_ref"] == "10101"
        assert acme.by_ref("relations")["10304"] == ops.by_ref("relations")["10304"]
        assert [r["source_ref"] for r in acme.get("relations")] == ["10300", "10301", "10302", "10303", "10304"]
        assert [r["source_ref"] for r in scrum.get("relations")] == ["10301"]
        assert [r["source_ref"] for r in ops.get("relations")] == ["10304"]
        for bundle in (acme, scrum, ops):
            assert "jira_links" not in str(bundle.get("issues"))
            assert not any("Issue links to" in what for what in bundle.not_carried())

    async def test_a_relation_never_has_both_ends_outside_the_bundle(self, cloud, run_export):
        for projects, options in ((["ACME"], {}), (["SCRUM"], {}), (["OPS"], {}), (["ACME", "SCRUM"], {"limit": 1})):
            _, bundle = await run_export(projects, **options)
            held = set(bundle.by_ref("issues"))
            assert bundle.get("relations")
            for relation in bundle.get("relations"):
                assert {relation["from_ref"], relation["to_ref"]} & held, relation

    async def test_bundles_of_one_project_each_validate_with_external_refs_only(self, cloud, run_export):
        # OPS-8 is the parent of ACME-3, and SCRUM-1's parent is in ACME; OPS holds no outside parent.
        expected = {"ACME": ["issues.jsonl", "relations.jsonl"], "SCRUM": ["issues.jsonl", "relations.jsonl"]}
        for project in ("ACME", "SCRUM", "OPS"):
            _, bundle = await run_export([project])
            files = _only_external_refs(validate_bundle(bundle.root))
            assert files == expected.get(project, ["relations.jsonl"]), project

    async def test_a_link_to_an_issue_jira_no_longer_has_is_counted(self, cloud, run_export):
        # ACME-2 links to ACME-4, which was deleted between the search and the read.
        issue = _issue_json(cloud, "10002")
        issue["fields"]["issuelinks"].append(
            {
                "id": "10305",
                "type": {"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                "outwardIssue": {"id": "10004", "key": "ACME-4"},
            }
        )
        cloud.override("GET", "/rest/api/3/issue/10002", {"status": 200, "json": issue})
        _, bundle = await run_export()
        assert "10305" not in bundle.by_ref("relations")
        assert bundle.by_ref("issues")["10002"]["extra"]["jira_links"] == [
            {
                "id": "10305",
                "type": "Blocks",
                "direction": "outward",
                "phrase": "blocks",
                "issue_key": "ACME-4",
                "issue_id": "10004",
            }
        ]
        assert bundle.not_carried()[LINKS_GONE] == 1

    async def test_remote_links_are_counted_from_the_change_history(self, cloud, run_export):
        _, bundle = await run_export()
        assert bundle.not_carried()[REMOTE_LINKS] == 1


class TestCloudParents:
    async def test_a_sub_task_and_an_epic_child_in_another_project(self, cloud, run_export):
        _, bundle = await run_export()
        issues = bundle.by_ref("issues")
        # SCRUM-2 is a sub-task of SCRUM-1, which is a child of the epic ACME-5.
        assert issues["10102"]["parent_ref"] == "10101"
        assert issues["10101"]["parent_ref"] == "10006"
        assert "parent_ref" not in issues["10001"]
        assert validate_bundle(bundle.root).ok

    async def test_a_parent_change_in_the_history(self, cloud, run_export):
        """Cloud writes one change under IssueParentAssociation and parent: one detail, by ref, with the keys."""
        _, bundle = await run_export()
        details = bundle.by_ref("journals")["history:20101"]["details"]
        parents = [d for d in details if d["prop_key"] == "parent_id"]
        assert parents == [
            {
                "property": "attr",
                "prop_key": "parent_id",
                "old_ref": "10950",
                "new_ref": "10006",
                "old_value": "OLD-4",
                "new_value": "ACME-5",
            }
        ]
        counted = bundle.not_carried()
        assert not any("IssueParentAssociation" in what or "parent" in what.lower() for what in counted)
        assert validate_bundle(bundle.root).ok

    async def test_a_parent_in_another_bundle_is_its_ref(self, cloud, run_export):
        _, bundle = await run_export()
        acme3 = bundle.by_ref("issues")["10003"]
        # OPS-8 is not in the bundle: Specivo sets it once both are imported.
        assert acme3["parent_ref"] == "10998"
        assert "jira_parent" not in acme3["extra"]
        assert not any("Parent" in what for what in bundle.not_carried())

    async def test_one_project_alone(self, cloud, run_export):
        _, bundle = await run_export(["SCRUM"])
        issues = bundle.by_ref("issues")
        assert issues["10102"]["parent_ref"] == "10101"
        assert issues["10101"]["parent_ref"] == "10006"
        assert "jira_parent" not in issues["10101"]["extra"]

    async def test_a_parent_jira_no_longer_has_is_counted(self, cloud, run_export):
        issue = _issue_json(cloud, "10002")
        issue["fields"]["parent"] = {"id": "10004", "key": "ACME-4"}
        cloud.override("GET", "/rest/api/3/issue/10002", {"status": 200, "json": issue})
        _, bundle = await run_export()
        acme2 = bundle.by_ref("issues")["10002"]
        assert "parent_ref" not in acme2
        assert acme2["extra"]["jira_parent"] == {"key": "ACME-4", "id": "10004"}
        assert bundle.not_carried()[PARENTS_GONE] == 1

    async def test_a_project_holding_parents_is_listed_first(self, cloud, run_export):
        _, bundle = await run_export(["SCRUM", "ACME"])
        # SCRUM-1's parent is in ACME, and Specivo loads projects in the order listed.
        assert [p["identifier"] for p in bundle.get("projects")] == ["acme", "scrum"]
        _, bundle = await run_export(["SCRUM", "ACME", "OPS"])
        # ACME-3's parent is in OPS.
        assert [p["identifier"] for p in bundle.get("projects")] == ["ops", "acme", "scrum"]

    async def test_a_parent_in_another_bundle_does_not_change_the_order(self, cloud, run_export):
        _, bundle = await run_export(["SCRUM", "ACME"])
        # OPS-8 (ACME-3's parent) is in another bundle: only ACME before SCRUM matters here.
        assert [p["identifier"] for p in bundle.get("projects")] == ["acme", "scrum"]
        _, bundle = await run_export(["ACME"])
        assert [p["identifier"] for p in bundle.get("projects")] == ["acme"]


class TestCloudWorklogs:
    async def test_a_truncated_list_is_read_in_full(self, cloud, run_export):
        _, bundle = await run_export()
        assert len(cloud.calls("GET", "/rest/api/3/issue/10001/worklog")) == 2
        # ACME-5 holds its whole list: not read again.
        assert cloud.calls("GET", "/rest/api/3/issue/10006/worklog") == []
        assert [t["source_ref"] for t in bundle.get("time_entries")] == ["10600", "10601", "10602"]

    async def test_time_entries(self, cloud, run_export):
        _, bundle = await run_export()
        entries = bundle.by_ref("time_entries")
        assert entries["10600"] == {
            "source_ref": "10600",
            "project_ref": "10000",
            "hours": "1.50",
            # 18:30 UTC is the next morning in the author's time zone, Asia/Bangkok.
            "spent_on": "2024-02-02",
            "issue_ref": "10001",
            "user_ref": ALICE,
            "activity_ref": "worklog",
            "comments": "Reproduced on Safari 17 with @Bob Example watching.\nNotes: https://docs.example.com/safari",
            "created_at": "2024-02-01T18:30:00Z",
            "updated_at": "2024-02-01T18:30:00Z",
        }
        # No time zone for the author: the day in the offset Jira gave.
        assert entries["10601"]["spent_on"] == "2024-02-03"
        # 20 minutes, rounded to hundredths as Specivo keeps them; the seconds are kept.
        assert entries["10601"]["hours"] == "0.33"
        assert entries["10601"]["extra"] == {"jira_time_spent_seconds": 1200}
        assert "comments" not in entries["10601"]
        # Restricted to a group: the time is kept, the comment is not.
        assert "comments" not in entries["10602"]
        assert entries["10602"]["extra"] == {"jira_visibility": {"type": "group", "value": "jira-administrators"}}

    async def test_a_restricted_worklog_comment_is_counted(self, cloud, run_export):
        _, bundle = await run_export()
        assert bundle.not_carried()["Comments of worklogs restricted to a group or role"] == 1

    async def test_one_activity_and_time_tracking(self, cloud, run_export):
        _, bundle = await run_export()
        assert bundle.lookups["activities"] == [{"source_ref": "worklog", "name": "Development", "is_default": True}]
        modules = {p["identifier"]: p["modules"] for p in bundle.get("projects")}
        assert modules == {"acme": ["issue_tracking", "time_tracking"], "scrum": ["issue_tracking"]}

    async def test_a_worklog_of_seconds_is_counted(self, cloud, run_export):
        _, bundle = await run_export()
        what = "Worklogs under 18 seconds (no time once rounded to hundredths of an hour)"
        assert bundle.not_carried()[what] == 1
        assert not any(w.startswith("Field timespent") for w in bundle.not_carried())

    async def test_no_worklogs_no_activity(self, cloud, run_export):
        _, bundle = await run_export(["SCRUM"])
        assert bundle.get("time_entries") == []
        assert "activities" not in bundle.lookups


class TestCloudComponentsAndVersions:
    async def test_components_are_categories_read_page_by_page(self, cloud, run_export):
        _, bundle = await run_export()
        assert len(cloud.calls("GET", "/rest/api/2/project/ACME/component")) == 2
        assert bundle.get("categories") == [
            {
                "source_ref": "10000",
                "project_ref": "10000",
                "name": "Web",
                "assigned_to_ref": ALICE,
                "extra": {"description": "The storefront.", "assignee_type": "COMPONENT_LEAD"},
            },
            {
                "source_ref": "10001",
                "project_ref": "10000",
                "name": "API",
                "extra": {"assignee_type": "PROJECT_DEFAULT"},
            },
        ]

    async def test_versions_and_their_status(self, cloud, run_export):
        _, bundle = await run_export()
        versions = bundle.by_ref("versions")
        assert [v["name"] for v in bundle.get("versions")] == ["0.9", "1.0", "1.1", "2.0"]
        assert versions["10400"] == {
            "source_ref": "10400",
            "project_ref": "10000",
            "name": "1.0",
            "description": "The first public release.",
            "status": "closed",
            "effective_date": "2024-01-31",
            "extra": {"released": True, "archived": False},
        }
        # Released and archived: closed. Archived only: locked, as nothing new may target it.
        assert versions["10403"]["status"] == "closed"
        assert versions["10401"]["status"] == "locked"
        assert "status" not in versions["10402"]
        assert versions["10402"]["extra"]["start_date"] == "2024-03-01"

    async def test_one_category_and_one_target_version_per_issue(self, cloud, run_export):
        _, bundle = await run_export()
        issues = bundle.by_ref("issues")
        assert issues["10001"]["category_ref"] == "10000"
        # Of 2.0, 1.0 and 0.9 the one released last; of 1.1 and 2.0, neither released, the last.
        assert issues["10001"]["fixed_version_ref"] == "10400"
        assert issues["10003"]["fixed_version_ref"] == "10402"
        assert issues["10002"]["fixed_version_ref"] == "10402"
        assert issues["10002"]["extra"]["affects_versions"] == ["1.0"]
        counted = bundle.not_carried()
        assert counted["Issues with more than one component (the first is the category)"] == 1
        assert counted["Fix versions beyond the one carried (Specivo has one per issue)"] == 3
        assert counted["Affects versions set on issues"] == 1
        assert validate_bundle(bundle.root).ok


def _change(field: str, field_id: str | None, old: str | None, new: str | None) -> dict:
    """One change history item, as Jira writes it (Data Center leaves out ``fieldId``)."""
    item = {"field": field, "fieldtype": "jira", "from": old, "fromString": old, "to": new, "toString": new}
    if field_id is not None:
        item["fieldId"] = field_id
    return item


def _history(history_id: str, created: str, *items: dict) -> dict:
    return {"id": history_id, "author": {"accountId": ALICE}, "created": created, "items": list(items)}


def _with_histories(issue: dict, *histories: dict) -> dict:
    changelog = issue["changelog"]
    changelog["histories"] += list(histories)
    changelog["total"] = changelog["maxResults"] = len(changelog["histories"])
    return issue


# Work logged in Jira writes these into the history; they follow from the worklogs, carried as time entries.
WORKLOG_CHANGES = (
    _change("timespent", "timespent", None, "3600"),
    _change("timeestimate", "timeestimate", "9000", "5400"),
    _change("WorklogId", None, None, "10650"),
)


class TestCloudEstimatesAndPlanningHistory:
    """ACME-2 (10002) now: estimate 2.5 h, 1.5 h left; fix versions 1.0 and 2.0 (target 1.0); components API, Web."""

    def _issue(self, cloud: FakeJira) -> dict:
        issue = _issue_json(cloud, "10002")
        issue["fields"]["timeoriginalestimate"] = 9000
        issue["fields"]["timeestimate"] = 5400
        issue["fields"]["fixVersions"] = [
            {"id": "10400", "name": "1.0", "released": True, "releaseDate": "2024-01-31"},
            {"id": "10402", "name": "2.0", "released": False, "releaseDate": "2024-06-30"},
        ]
        issue["fields"]["components"] = [{"id": "10001", "name": "API"}, {"id": "10000", "name": "Web"}]
        return _with_histories(
            issue,
            _history(
                "20901",
                "2024-02-02T01:00:00.000+0000",
                _change("Fix Version", "fixVersions", None, "10402"),
                _change("Component", "components", None, "10000"),
                _change("timeoriginalestimate", "timeoriginalestimate", None, "3600"),
            ),
            _history(
                "20902",
                "2024-02-02T01:10:00.000+0000",
                _change("Fix Version", "fixVersions", None, "10400"),
                _change("timeoriginalestimate", "timeoriginalestimate", "3600", "9000"),
                _change("Component", "components", None, "10001"),
            ),
            # 1.1 is added and removed again: 1.0 stays the target, so neither is a detail.
            _history("20903", "2024-02-02T01:20:00.000+0000", _change("Fix Version", "fixVersions", None, "10401")),
            _history("20904", "2024-02-02T01:30:00.000+0000", _change("Fix Version", "fixVersions", "10401", None)),
            _history("20905", "2024-02-02T01:40:00.000+0000", *WORKLOG_CHANGES),
        )

    async def _export(self, cloud, run_export, issue: dict | None = None):
        cloud.override("GET", "/rest/api/3/issue/10002", {"status": 200, "json": issue or self._issue(cloud)})
        _, bundle = await run_export()
        return bundle

    @staticmethod
    def _details(bundle, ref: str) -> list[tuple[str, str | None, str | None]]:
        journal = bundle.by_ref("journals")[ref]
        return [_values(d) for d in journal["details"]]

    async def test_the_original_estimate_is_the_estimated_hours(self, cloud, run_export):
        bundle = await self._export(cloud, run_export)
        issue = bundle.by_ref("issues")["10002"]
        assert issue["estimated_hours"] == "2.50"
        assert "jira_original_estimate_seconds" not in issue["extra"]
        assert "estimated_hours" not in bundle.by_ref("issues")["10001"]

    async def test_an_estimate_is_rounded_to_hundredths_and_the_seconds_kept(self, cloud, run_export):
        issue = self._issue(cloud)
        issue["fields"]["timeoriginalestimate"] = 1210
        bundle = await self._export(cloud, run_export, issue)
        issue = bundle.by_ref("issues")["10002"]
        assert issue["estimated_hours"] == "0.34"
        assert issue["extra"]["jira_original_estimate_seconds"] == 1210

    async def test_a_zero_estimate_is_kept(self, cloud, run_export):
        issue = self._issue(cloud)
        issue["fields"]["timeoriginalestimate"] = 0
        bundle = await self._export(cloud, run_export, issue)
        assert bundle.by_ref("issues")["10002"]["estimated_hours"] == "0.00"

    async def test_the_remaining_estimate_is_carried(self, cloud, run_export):
        bundle = await self._export(cloud, run_export)
        issue = bundle.by_ref("issues")["10002"]
        assert issue["remaining_estimate"] == "1.50"
        assert "jira_remaining_estimate_seconds" not in issue["extra"]
        assert "remaining_estimate" not in bundle.by_ref("issues")["10001"]
        assert not any("timeestimate" in what for what in bundle.not_carried())

    async def test_a_remaining_estimate_is_rounded_to_hundredths_and_the_seconds_kept(self, cloud, run_export):
        issue = self._issue(cloud)
        issue["fields"]["timeestimate"] = 1210
        bundle = await self._export(cloud, run_export, issue)
        issue = bundle.by_ref("issues")["10002"]
        assert issue["remaining_estimate"] == "0.34"
        assert issue["extra"]["jira_remaining_estimate_seconds"] == 1210

    async def test_no_time_left_is_kept(self, cloud, run_export):
        issue = self._issue(cloud)
        issue["fields"]["timeestimate"] = 0
        bundle = await self._export(cloud, run_export, issue)
        assert bundle.by_ref("issues")["10002"]["remaining_estimate"] == "0.00"

    async def test_a_remaining_estimate_without_an_original_one(self, cloud, run_export):
        issue = self._issue(cloud)
        issue["fields"]["timeoriginalestimate"] = None
        bundle = await self._export(cloud, run_export, issue)
        issue = bundle.by_ref("issues")["10002"]
        assert "estimated_hours" not in issue
        assert issue["remaining_estimate"] == "1.50"

    async def test_estimate_target_version_and_category_history(self, cloud, run_export):
        bundle = await self._export(cloud, run_export)
        # In the order Jira lists the changes; the values are the bundle's refs, the hours as Specivo writes them.
        assert self._details(bundle, "history:20901") == [
            ("fixed_version_id", None, "10402"),
            ("category_id", None, "10000"),
            ("estimated_hours", None, "1.00"),
        ]
        names = [
            (d.get("old_value"), d.get("new_value")) for d in bundle.by_ref("journals")["history:20902"]["details"]
        ]
        assert names[0] == ("2.0", "1.0")
        # 1.0 is released, so it becomes the target; API comes before Web, so it becomes the category.
        assert self._details(bundle, "history:20902") == [
            ("fixed_version_id", "10402", "10400"),
            ("estimated_hours", "1.00", "2.50"),
            ("category_id", "10000", "10001"),
        ]
        assert {d["property"] for d in bundle.by_ref("journals")["history:20902"]["details"]} == {"attr"}
        # The last change matches the issue as it is now.
        issue = bundle.by_ref("issues")["10002"]
        assert (issue["fixed_version_ref"], issue["category_ref"]) == ("10400", "10001")
        assert validate_bundle(bundle.root).ok

    async def test_changes_that_leave_the_derived_value_alone_write_nothing(self, cloud, run_export):
        bundle = await self._export(cloud, run_export)
        journals = bundle.by_ref("journals")
        assert "history:20903" not in journals
        assert "history:20904" not in journals

    async def test_worklog_history_is_derived_and_not_counted(self, cloud, run_export):
        bundle = await self._export(cloud, run_export)
        assert "history:20905" not in bundle.by_ref("journals")
        counted = bundle.not_carried()
        for name in ("timespent", "timeestimate", "WorklogId", "timeoriginalestimate", "Fix Version", "Component"):
            assert f"Change history of {name}" not in counted, name
        assert not any("timeoriginalestimate" in what for what in counted)

    async def test_worklog_history_with_a_carried_change_keeps_only_that(self, cloud, run_export):
        issue = _with_histories(
            self._issue(cloud),
            _history(
                "20906",
                "2024-02-02T01:50:00.000+0000",
                _change("WorklogTimeSpent", None, "3600", "1800"),
                _change("timespent", "timespent", "3600", "1800"),
                _change("status", "status", "3", "10002"),
            ),
        )
        bundle = await self._export(cloud, run_export, issue)
        assert self._details(bundle, "history:20906") == [("status_id", "3", "10002")]
        assert "Change history of WorklogTimeSpent" not in bundle.not_carried()

    async def test_an_estimate_change_that_rounds_to_the_same_hours_writes_nothing(self, cloud, run_export):
        issue = _with_histories(
            self._issue(cloud),
            _history(
                "20907",
                "2024-02-02T01:55:00.000+0000",
                _change("timeoriginalestimate", "timeoriginalestimate", "9000", "9001"),
            ),
        )
        issue["fields"]["timeoriginalestimate"] = 9001
        bundle = await self._export(cloud, run_export, issue)
        assert "history:20907" not in bundle.by_ref("journals")

    async def test_removing_the_target_falls_back_to_the_rule(self, cloud, run_export):
        # Today: 2.0 only, no components. Before: 1.0 and 2.0 (target 1.0), and component Web.
        issue = _issue_json(cloud, "10002")
        issue["fields"]["fixVersions"] = [{"id": "10402", "name": "2.0", "released": False}]
        issue["fields"]["components"] = []
        issue = _with_histories(
            issue,
            _history(
                "20908",
                "2024-02-02T02:00:00.000+0000",
                _change("Fix Version", "fixVersions", "10400", None),
                _change("Component", "components", "10000", None),
            ),
        )
        bundle = await self._export(cloud, run_export, issue)
        assert self._details(bundle, "history:20908") == [
            ("fixed_version_id", "10400", "10402"),
            ("category_id", "10000", None),
        ]

    async def test_a_component_outside_the_project_is_no_category(self, cloud, run_export):
        issue = _issue_json(cloud, "10002")
        issue["fields"]["components"] = [{"id": "10000", "name": "Web"}]
        issue = _with_histories(
            issue,
            # A component since deleted from the project was removed.
            _history(
                "20909",
                "2024-02-02T02:00:00.000+0000",
                _change("Component", "components", "10099", None),
            ),
        )
        bundle = await self._export(cloud, run_export, issue)
        # Before: [Web, 10099], the issue's own components first: Web was the category then, as now.
        assert "history:20909" not in bundle.by_ref("journals")


class TestCloudWatchers:
    async def test_only_issues_with_watchers_are_asked(self, cloud, run_export):
        await run_export()
        asked = sorted(r.url.path for r in cloud.requests if r.url.path.endswith("/watchers"))
        # SCRUM-2 has no watchers, so it is not asked about.
        assert asked == [f"/rest/api/3/issue/{i}/watchers" for i in ("10001", "10002", "10003", "10006", "10101")]

    async def test_watchers_as_jira_lists_them(self, cloud, run_export):
        _, bundle = await run_export()
        assert [(w["container_ref"], w["user_ref"]) for w in bundle.get("watchers")] == [
            ("10001", ALICE),
            ("10001", BOB),
            ("10003", CAROL),
            ("10101", ALICE),
        ]
        assert {w["container_kind"] for w in bundle.get("watchers")} == {"issue"}

    async def test_what_is_not_carried_is_counted(self, cloud, run_export):
        _, bundle = await run_export()
        counted = bundle.not_carried()
        assert counted["Watchers Jira refused to list (HTTP 403)"] == 1
        assert counted["Watchers Jira did not list"] == 1
        assert counted["Watchers that are app accounts"] == 1
        assert "Watchers" not in counted

    async def test_a_continued_run_does_not_ask_again(self, cloud, run_export, tmp_path):
        out = tmp_path / "bundle"
        cloud.override("GET", "/rest/api/3/issue/10006/watchers", {"status": 500, "json": {}})
        with pytest.raises(Exception, match="HTTP 500"):
            await run_export(out=out)
        assert len(cloud.calls("GET", "/rest/api/3/issue/10001/watchers")) == 1
        _, bundle = await run_export(out=out)
        assert len(cloud.calls("GET", "/rest/api/3/issue/10001/watchers")) == 1
        assert len(cloud.calls("GET", "/rest/api/3/issue/10006/watchers")) == 2
        assert len(bundle.get("watchers")) == 4


class TestCloudAttachments:
    async def test_records(self, cloud, run_export):
        _, bundle = await run_export()
        attachments = bundle.by_ref("attachments")
        assert set(attachments) == {"10200", "10201", "10210", "10211", "10220"}
        shot = attachments["10201"]
        png = _fixture_bytes("cloud.json", _media_path("10201"))
        assert shot == {
            "source_ref": "10201",
            "container_kind": "issue",
            "container_ref": "10001",
            "filename": "screenshot.png",
            "sha256": hashlib.sha256(png).hexdigest(),
            "content_type": "image/png",
            "filesize": len(png),
            "author_ref": BOB,
            "created_at": "2024-02-01T11:05:00+07:00",
        }
        assert (bundle.root / "files" / shot["sha256"]).read_bytes() == png
        assert bundle.manifest["counts"]["attachments"] == 5

    async def test_content_two_attachments_share_is_stored_once(self, cloud, run_export):
        _, bundle = await run_export()
        attachments = bundle.by_ref("attachments")
        assert attachments["10201"]["sha256"] == attachments["10210"]["sha256"]
        stored = sorted(p.name for p in (bundle.root / "files").iterdir())
        assert stored == sorted({a["sha256"] for a in attachments.values()})
        assert len(stored) == 4
        _only_external_refs(validate_bundle(bundle.root))

    async def test_files_are_named_by_hash_never_by_their_jira_name(self, cloud, run_export):
        summary, bundle = await run_export()
        names = {p.name for p in bundle.root.rglob("*")}
        assert not names & {"screenshot.png", "image.png", "console.log", "ภาพหน้าจอ.png"}

    async def test_a_name_used_twice_on_one_issue_is_numbered(self, cloud, run_export):
        _, bundle = await run_export()
        attachments = bundle.by_ref("attachments")
        assert attachments["10210"]["filename"] == "image.png"
        assert "extra" not in attachments["10210"]
        assert attachments["10211"]["filename"] == "image (2).png"
        assert attachments["10211"]["extra"] == {"jira_filename": "image.png"}
        description = bundle.by_ref("issues")["10002"]["description"]
        assert description == (
            "Before and after:\n\n![image.png]({{attachment:10210}})\n\n![image (2).png]({{attachment:10211}})"
        )

    async def test_an_image_in_the_description(self, cloud, run_export):
        _, bundle = await run_export()
        text = bundle.by_ref("issues")["10001"]["description"]
        assert "The screenshot:\n\n![screenshot.png]({{attachment:10201}})\n\n" in text

    async def test_files_in_a_comment(self, cloud, run_export):
        _, bundle = await run_export()
        notes = bundle.by_ref("journals")["comment:10102"]["notes"]
        assert notes.endswith(
            "[console.log]({{attachment:10200}}) "
            "*(attachment screen recording.mp4: not carried over, larger than the size limit)*"
        )

    async def test_a_thai_file_name_and_an_image_of_another_issue(self, cloud, run_export):
        _, bundle = await run_export()
        assert bundle.by_ref("attachments")["10220"]["filename"] == "ภาพหน้าจอ.png"
        text = bundle.by_ref("issues")["10101"]["description"]
        assert text.endswith("![ภาพหน้าจอ.png]({{attachment:10220}})\n\n*(an attachment is shown here in Jira)*")

    async def test_what_is_not_carried_over_is_counted_by_reason(self, cloud, run_export):
        _, bundle = await run_export()
        counted = bundle.not_carried()
        assert counted["Attachments larger than --max-attachment-size"] == 1
        assert counted["Attachments Jira refused to send (HTTP 403)"] == 1
        assert counted["Attachments Jira no longer has (HTTP 404)"] == 1
        # The recording, and the image whose file is another issue's.
        assert counted[MEDIA_NOT_IN_BUNDLE] == 2
        assert "Attachments" not in counted

    async def test_downloads_go_through_the_media_host_without_credentials(self, cloud, run_export):
        await run_export()
        media = [r for r in cloud.requests if r.url.host == MEDIA_HOST]
        assert {r.url.path for r in media} == {_media_path(i) for i in ("10200", "10201", "10210", "10211", "10220")}
        for request in media:
            assert "Authorization" not in request.headers
        site = [r for r in cloud.requests if r.url.host == "example.atlassian.net"]
        assert all(r.headers["Authorization"].startswith("Basic ") for r in site)
        # The large file: its redirect was read for the media id, its bytes were not.
        assert len(cloud.calls("GET", "/rest/api/3/attachment/content/10202")) == 1
        assert not cloud.calls("GET", _media_path("10202"), host=MEDIA_HOST)

    async def test_the_media_map_is_kept_in_the_state(self, cloud, run_export, tmp_path):
        out = tmp_path / "bundle"
        await run_export(out=out, keep_state=True)
        kept = json.loads((state_path_for(out) / "attachments.json").read_text())
        assert kept["10201"]["media_id"] == "5e1f0000-0000-4000-8000-000000010201"
        assert kept["10202"] == {
            "status": "too_large",
            "size": 300 * 1024 * 1024,
            "limit": 100 * 1024 * 1024,
            "media_id": "5e1f0000-0000-4000-8000-000000010202",
        }
        assert kept["10203"] == {"status": "forbidden"}
        assert kept["10204"] == {"status": "gone"}
        files = state_path_for(out) / "files"
        assert stat.S_IMODE(next(files.iterdir()).stat().st_mode) == 0o600

    async def test_a_lower_limit_leaves_out_more(self, cloud, run_export):
        _, bundle = await run_export(max_attachment_size=60)
        # Only the log is under 60 bytes; whatever Jira says is larger is not even asked for.
        assert set(bundle.by_ref("attachments")) == {"10200"}
        assert bundle.not_carried()["Attachments larger than --max-attachment-size"] == 7
        assert "Attachments Jira refused to send (HTTP 403)" not in bundle.not_carried()
        assert (
            "*(attachment screenshot.png: not carried over, larger than the size limit)*"
            in (bundle.by_ref("issues")["10001"]["description"])
        )

    async def test_a_body_larger_than_jira_said_is_cut_off(self, cloud, run_export):
        cloud.override("GET", _media_path("10200"), {"status": 200, "text": "x" * 5000}, host=MEDIA_HOST)
        _, bundle = await run_export(max_attachment_size=1000)
        assert "10200" not in bundle.by_ref("attachments")
        # The log, the recording and the contract (which Jira says is 4 KB).
        assert bundle.not_carried()["Attachments larger than --max-attachment-size"] == 3
        assert (
            "*(attachment console.log: not carried over, larger than the size limit)*"
            in (bundle.by_ref("journals")["comment:10102"]["notes"])
        )


class TestAttachmentResume:
    async def test_a_failed_download_continues_where_it_stopped(self, cloud, run_export, tmp_path):
        out = tmp_path / "bundle"
        cloud.override("GET", _media_path("10211"), {"status": 500, "json": {}}, host=MEDIA_HOST)
        with pytest.raises(Exception, match="HTTP 500"):
            await run_export(out=out)
        state = state_path_for(out)
        kept = json.loads((state / "attachments.json").read_text())
        assert {"10200", "10201", "10210"} <= set(kept) and "10211" not in kept
        assert not list((state / "files").glob(".part-*"))

        summary, bundle = await run_export(out=out)
        assert summary.resumed
        # Files already stored were not downloaded again.
        assert len(cloud.calls("GET", _media_path("10201"), host=MEDIA_HOST)) == 1
        assert len(cloud.calls("GET", _media_path("10211"), host=MEDIA_HOST)) == 2
        assert "10211" in bundle.by_ref("attachments")

    async def test_a_stored_file_that_changed_is_downloaded_again(self, cloud, run_export, tmp_path):
        out = tmp_path / "bundle"
        cloud.override("GET", _media_path("10211"), {"status": 500, "json": {}}, host=MEDIA_HOST)
        with pytest.raises(Exception, match="HTTP 500"):
            await run_export(out=out)
        state = state_path_for(out)
        sha = json.loads((state / "attachments.json").read_text())["10200"]["sha256"]
        (state / "files" / sha).write_bytes(b"tampered")
        (state / "files" / ".part-left-over").write_bytes(b"half")

        _, bundle = await run_export(out=out)
        assert len(cloud.calls("GET", _media_path("10200"), host=MEDIA_HOST)) == 2
        assert bundle.by_ref("attachments")["10200"]["sha256"] == sha
        assert validate_bundle(bundle.root).ok

    async def test_a_higher_limit_on_a_continued_run_downloads_what_was_skipped(self, cloud, run_export, tmp_path):
        out = tmp_path / "bundle"
        await run_export(out=out, max_attachment_size=60, keep_state=True)
        kept = json.loads((state_path_for(out) / "attachments.json").read_text())
        assert kept["10201"]["status"] == "too_large"
        shutil.rmtree(out)
        summary, bundle = await run_export(out=out)
        assert summary.resumed
        assert "10201" in bundle.by_ref("attachments")
        # What was stored already is not downloaded again.
        assert len(cloud.calls("GET", _media_path("10200"), host=MEDIA_HOST)) == 1


class TestFileNames:
    def test_control_characters_and_length(self):
        from specivo_jira_migration.mapping import _filename

        assert _filename({"id": "1", "filename": "a\tb\nc.png"}) == "a b c.png"
        assert _filename({"id": "7", "filename": "  "}) == "attachment-7"
        long = _filename({"id": "1", "filename": "x" * 300 + ".png"})
        assert len(long) == 255 and long.endswith("x.png")

    def test_numbering(self):
        from specivo_jira_migration.mapping import _unique_filename

        taken: set[str] = set()
        assert [_unique_filename(n, taken) for n in ["a.png", "a.png", "a (2).png", "a.png", "README", "README"]] == [
            "a.png",
            "a (2).png",
            "a (2) (2).png",
            "a (3).png",
            "README",
            "README (2)",
        ]


class TestRateLimitsInAnExport:
    async def test_waits_are_taken_and_the_issues_read(self, cloud, run_export, sleeps):
        _, bundle = await run_export()
        assert len(cloud.calls("GET", "/rest/api/3/issue/10002")) == 2
        assert len(cloud.calls("GET", "/rest/api/3/issue/10003")) == 3
        # Retry-After: 2, plus jitter; then two backoffs without it.
        assert len(sleeps.waits) == 3
        assert 2.0 <= sleeps.waits[0] <= 3.0
        assert {"10002", "10003"} <= set(bundle.by_ref("issues"))


class TestSearchPaging:
    async def test_endless_islast_ends_on_an_empty_page_and_a_repeated_token(self, cloud, run_export):
        await run_export()
        bodies = [json.loads(r.content) for r in cloud.calls("POST", "/rest/api/3/search/jql")]
        acme = [b.get("nextPageToken") for b in bodies if '"ACME"' in b["jql"]]
        scrum = [b.get("nextPageToken") for b in bodies if '"SCRUM"' in b["jql"]]
        assert acme == [None, "acme-page-2", "acme-page-3", "acme-page-4"]
        assert scrum == [None, "scrum-page-2"]
        assert all(b["fields"] == ["key"] for b in bodies)

    async def test_limit(self, cloud, run_export):
        _, bundle = await run_export(limit=1)
        assert [i["extra"]["jira_key"] for i in bundle.get("issues")] == ["ACME-1", "SCRUM-1"]
        # A key of an issue the limit left out is still a link Specivo can make later.
        assert "{{issue:10002|ACME-2}}" in bundle.by_ref("issues")["10001"]["description"]


class TestRepeatable:
    async def test_the_same_state_writes_the_same_bundle(self, cloud, run_export, tmp_path):
        out = tmp_path / "first"
        await run_export(out=out, keep_state=True)
        first = _tree(out)
        assert any(name.startswith("files/") for name in first)
        # Write again from the kept state: nothing is read from Jira again.
        requests = len(cloud.requests)
        os.rename(out, tmp_path / "moved")
        await run_export(out=out, keep_state=True)
        assert len(cloud.requests) == requests
        assert _tree(out) == first

    async def test_two_fresh_runs_write_the_same_records(self, cloud, run_export):
        _, one = await run_export()
        _, two = await run_export()
        assert one.records == two.records
        assert one.lookups == two.lookups


class TestResume:
    async def test_a_failed_run_continues_where_it_stopped(self, cloud, run_export, tmp_path):
        out = tmp_path / "bundle"
        failing = {"status": 500, "json": {"errorMessages": ["Internal server error"], "errors": {}}}
        cloud.override("GET", "/rest/api/3/issue/10006", failing)
        with pytest.raises(Exception, match="HTTP 500"):
            await run_export(out=out)
        state = state_path_for(out)
        assert (state / "issues" / "10001.json").exists()
        assert not (out / "manifest.json").exists()

        before = len(cloud.calls("GET", "/rest/api/3/issue/10001"))
        summary, bundle = await run_export(out=out)
        assert summary.resumed
        # Issues already read are not read again; the search is not repeated.
        assert len(cloud.calls("GET", "/rest/api/3/issue/10001")) == before
        assert (
            len([r for r in cloud.calls("POST", "/rest/api/3/search/jql") if '"ACME"' in json.loads(r.content)["jql"]])
            == 4
        )
        assert "10006" in bundle.by_ref("issues")
        assert not state.exists()

    async def test_the_state_is_private(self, cloud, run_export, tmp_path):
        out = tmp_path / "bundle"
        await run_export(out=out, keep_state=True)
        state = state_path_for(out)
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
        assert stat.S_IMODE((state / "state.json").stat().st_mode) == 0o600

    async def test_a_state_of_an_older_format_is_refused(self, cloud, run_export, tmp_path):
        out = tmp_path / "bundle"
        cloud.override("GET", "/rest/api/3/issue/10006", {"status": 500, "json": {}})
        with pytest.raises(Exception, match="HTTP 500"):
            await run_export(out=out)
        state_file = state_path_for(out) / "state.json"
        data = json.loads(state_file.read_text())
        data["version"] = 1
        state_file.write_text(json.dumps(data))
        with pytest.raises(ExportError, match=r"another version of this tool .*delete it"):
            await run_export(out=out)
        _, bundle = await run_export(out=out, restart=True)
        assert len(bundle.get("issues")) == 6

    async def test_a_state_of_other_options_is_refused(self, cloud, run_export, tmp_path):
        out = tmp_path / "bundle"
        cloud.override("GET", "/rest/api/3/issue/10006", {"status": 500, "json": {}})
        with pytest.raises(Exception, match="HTTP 500"):
            await run_export(out=out)
        with pytest.raises(ExportError, match="other options .*limit"):
            await run_export(out=out, limit=2)
        _, bundle = await run_export(out=out, limit=2, restart=True)
        assert len(bundle.get("issues")) == 4


class TestTarget:
    async def test_an_existing_bundle_is_never_overwritten(self, cloud, run_export, tmp_path):
        out = tmp_path / "bundle"
        await run_export(out=out)
        with pytest.raises(ExportError, match="already holds a bundle"):
            await run_export(out=out)

    async def test_a_directory_with_other_files_is_refused(self, cloud, run_export, tmp_path):
        out = tmp_path / "bundle"
        out.mkdir()
        (out / "notes.txt").write_text("mine")
        with pytest.raises(ExportError, match="not empty"):
            await run_export(out=out)
        assert (out / "notes.txt").read_text() == "mine"

    async def test_an_unknown_project(self, cloud, run_export):
        with pytest.raises(ExportError, match="no project NOPE"):
            await run_export(["NOPE"])

    async def test_a_zip(self, cloud, run_export, tmp_path):
        summary, _ = await run_export(out=tmp_path / "bundle.zip")
        assert summary.path.is_file()
        assert validate_bundle(summary.path).ok

    async def test_a_data_center_token_on_cloud_is_refused(self, cloud, run_export):
        with pytest.raises(ExportError, match="this is Jira Cloud"):
            await run_export(auth=DC_AUTH)


class TestDataCenter:
    async def _run(self, run_export):
        return await run_export(["SUP"], url=DC_URL, auth=DC_AUTH)

    async def test_the_bundle_is_valid(self, datacenter, run_export):
        summary, bundle = await self._run(run_export)
        assert validate_bundle(bundle.root).ok
        assert bundle.manifest["source"]["instance"] == "jira.example.com"
        assert bundle.manifest["source"]["version"] == "9.12.4"
        assert summary.counts["issues"] == 3

    async def test_search_pages_with_start_at(self, datacenter, run_export):
        await self._run(run_export)
        assert [r.url.params["startAt"] for r in datacenter.calls("GET", "/rest/api/2/search")] == ["0", "2"]
        assert all(r.headers["Authorization"] == "Bearer dc-token-for-tests" for r in datacenter.requests)

    async def test_users_are_keyed_by_user_key(self, datacenter, run_export):
        _, bundle = await self._run(run_export)
        users = bundle.by_ref("users")
        assert users["JIRAUSER10100"]["login"] == "alice"
        assert users["JIRAUSER10100"]["email"] == "alice@example.com"
        assert users["bob"]["login"] == "bob"
        assert users["JIRAUSER10102"]["status"] == "locked"

    async def test_user_names_in_history_and_mentions_become_user_keys(self, datacenter, run_export):
        _, bundle = await self._run(run_export)
        journals = bundle.by_ref("journals")
        assignee = journals["history:40001"]["details"][0]
        assert (assignee["old_ref"], assignee["new_ref"]) == ("JIRAUSER10100", "bob")
        assert journals["comment:30001"]["notes"] == "Restarting the spooler helps for a while, {{user:JIRAUSER10100}}."
        issue = bundle.by_ref("issues")["20001"]
        assert issue["description"] == (
            "### Symptoms\n\n- Jobs stay queued\n- No error shown\n\n"
            "{{user:bob}} is looking at it; related to {{issue:20002}}."
        )

    async def test_estimate_target_version_and_category_history(self, datacenter, run_export):
        issue = _issue_json(datacenter, "20001", api="2")
        issue["fields"]["timeoriginalestimate"] = 7200
        issue["fields"]["timeestimate"] = 5400
        issue["changelog"]["histories"].append(
            {
                "id": "40903",
                "author": {"name": "alice", "key": "JIRAUSER10100"},
                "created": "2023-11-04T10:00:00.000+0700",
                "items": [
                    _change("Fix Version", None, None, "30300"),
                    _change("Component", None, None, "30200"),
                    _change("timeoriginalestimate", None, None, "7200"),
                    _change("timespent", None, None, "1800"),
                    _change("WorklogId", None, None, "30700"),
                ],
            }
        )
        issue["changelog"]["total"] = issue["changelog"]["maxResults"] = 3
        datacenter.override("GET", "/rest/api/2/issue/20001", {"status": 200, "json": issue})
        _, bundle = await self._run(run_export)
        assert bundle.by_ref("issues")["20001"]["estimated_hours"] == "2.00"
        assert bundle.by_ref("issues")["20001"]["remaining_estimate"] == "1.50"
        details = bundle.by_ref("journals")["history:40903"]["details"]
        assert [_values(d) for d in details] == [
            ("fixed_version_id", None, "30300"),
            ("category_id", None, "30200"),
            ("estimated_hours", None, "2.00"),
        ]
        counted = bundle.not_carried()
        for name in ("Fix Version", "Component", "timeoriginalestimate", "timespent", "WorklogId"):
            assert f"Change history of {name}" not in counted, name

    async def test_a_truncated_history_is_counted(self, datacenter, run_export):
        _, bundle = await self._run(run_export)
        assert bundle.not_carried()["Change history entries Jira did not return"] == 1
        assert datacenter.calls("GET", "/rest/api/2/issue/20002/changelog") == []

    async def test_attachments(self, datacenter, run_export):
        _, bundle = await self._run(run_export)
        attachments = bundle.by_ref("attachments")
        assert {ref: a["filename"] for ref, a in attachments.items()} == {
            "30500": "quote.png",
            "30501": "paper list.txt",
        }
        assert attachments["30500"]["author_ref"] == "JIRAUSER10100"
        assert bundle.by_ref("issues")["20003"]["description"] == (
            "Quote attached: ![quote.png]({{attachment:30500}})\n\nThe list: [paper list.txt]({{attachment:30501}})"
        )
        assert validate_bundle(bundle.root).ok

    async def test_attachments_are_read_from_the_site_only(self, datacenter, run_export):
        await self._run(run_export)
        # The content URL on the site is used; one on another host is not, the REST endpoint is.
        quote = datacenter.calls("GET", "/secure/attachment/30500/quote.png")
        assert quote[0].headers["Authorization"] == "Bearer dc-token-for-tests"
        assert datacenter.calls("GET", "/rest/api/2/attachment/content/30501")
        assert not [r for r in datacenter.requests if r.url.host == "files.example.net"]

    async def test_epic_links_become_parents(self, datacenter, run_export):
        _, bundle = await run_export(["SUP"], url=DC_URL, auth=DC_AUTH)
        issues = bundle.by_ref("issues")
        assert issues["20003"]["parent_ref"] == "20001"
        # An epic outside the bundle: the field gives its key, and Jira its id.
        assert datacenter.calls("GET", "/rest/api/2/issue/PLAT-3")
        assert issues["20002"]["parent_ref"] == "20903"
        assert "jira_parent" not in issues["20002"]["extra"]
        assert not any("Epic Link" in what or "Parent" in what for what in bundle.not_carried())
        assert _only_external_refs(validate_bundle(bundle.root)) == ["issues.jsonl"]

    async def test_epic_link_changes_in_the_history(self, datacenter, run_export):
        _, bundle = await run_export(["SUP"], url=DC_URL, auth=DC_AUTH)
        journals = bundle.by_ref("journals")

        def parent(ref):
            [detail] = [d for d in journals[ref]["details"] if d["prop_key"] == "parent_id"]
            return detail.get("old_ref"), detail.get("new_ref"), detail.get("old_value"), detail.get("new_value")

        # To an epic outside the bundle: its id all the same, and its key for the text.
        assert parent("history:40004") == ("20001", "20903", "SUP-1", "PLAT-3")
        # A key alone: the id of the issue the export knows by it.
        assert parent("history:40005") == (None, "20001", None, "SUP-1")
        # The epic's side of a change (Epic Child) is the child's history: neither a detail nor counted.
        assert not [d for d in journals["history:40002"]["details"] if d["prop_key"] == "parent_id"]
        assert not any("Epic" in what for what in bundle.not_carried())

    async def test_an_epic_link_jira_does_not_know_is_counted(self, datacenter, run_export):
        datacenter.override("GET", "/rest/api/2/issue/PLAT-3", {"status": 404, "json": {"errorMessages": []}})
        _, bundle = await run_export(["SUP"], url=DC_URL, auth=DC_AUTH)
        issue = bundle.by_ref("issues")["20002"]
        assert "parent_ref" not in issue
        assert issue["extra"]["jira_parent"] == {"key": "PLAT-3"}
        assert bundle.not_carried()[PARENTS_UNKNOWN] == 1

    async def test_without_key_look_ups_an_epic_link_outside_is_counted(self, datacenter, run_export):
        _, bundle = await run_export(["SUP"], url=DC_URL, auth=DC_AUTH, resolve_keys=False)
        assert datacenter.calls("GET", "/rest/api/2/issue/PLAT-3") == []
        issues = bundle.by_ref("issues")
        assert issues["20002"]["extra"]["jira_parent"] == {"key": "PLAT-3"}
        # An Epic Link to an issue of the bundle needs no look-up.
        assert issues["20003"]["parent_ref"] == "20001"
        assert bundle.not_carried()[PARENTS_UNKNOWN] == 1

    async def test_a_worklog(self, datacenter, run_export):
        _, bundle = await run_export(["SUP"], url=DC_URL, auth=DC_AUTH)
        [entry] = bundle.get("time_entries")
        assert (entry["hours"], entry["spent_on"], entry["user_ref"]) == ("0.75", "2024-03-05", "JIRAUSER10100")
        # Wiki markup is kept as written: the field is plain text in Specivo.
        assert entry["comments"] == "Read the *proxy* logs."

    async def test_components_and_versions(self, datacenter, run_export):
        _, bundle = await run_export(["SUP"], url=DC_URL, auth=DC_AUTH)
        [category] = bundle.get("categories")
        assert (category["name"], category["assigned_to_ref"]) == ("Proxy", "JIRAUSER10100")
        [version] = bundle.get("versions")
        assert (version["name"], version["status"], version["effective_date"]) == ("3.2", "closed", "2024-02-28")
        issue = bundle.by_ref("issues")["20001"]
        assert (issue["category_ref"], issue["fixed_version_ref"]) == ("30200", "30300")

    async def test_watchers(self, datacenter, run_export):
        _, bundle = await run_export(["SUP"], url=DC_URL, auth=DC_AUTH)
        assert [(w["container_ref"], w["user_ref"]) for w in bundle.get("watchers")] == [
            ("20001", "JIRAUSER10100"),
            ("20002", "JIRAUSER10102"),
            ("20003", "bob"),
        ]

    async def test_a_link(self, datacenter, run_export):
        _, bundle = await run_export(["SUP"], url=DC_URL, auth=DC_AUTH)
        assert bundle.get("relations") == [
            {"source_ref": "30100", "from_ref": "20001", "to_ref": "20002", "relation_type": "blocks"}
        ]

    async def test_an_attachment_in_the_history(self, datacenter, run_export):
        _, bundle = await self._run(run_export)
        detail = bundle.by_ref("journals")["history:40005"]["details"][0]
        assert detail == {"property": "attachment", "prop_key": "30500", "new_value": "quote.png"}

    async def test_cloud_credentials_on_data_center_are_refused(self, datacenter, run_export):
        with pytest.raises(ExportError, match="this is Jira Data Center"):
            await run_export(["SUP"], url=DC_URL)
