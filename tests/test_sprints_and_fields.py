"""Sprints, custom fields and labels, end to end against the hand-written fixture sites."""

from __future__ import annotations

from specivo_migration_sdk import validate_bundle

from specivo_jira_migration.mapping import (
    BOARDS_UNREADABLE,
    CF_USERS_LEFT_OUT,
    CONTEXTS_UNREADABLE,
    LONG_LABELS,
    ORPHAN_SPRINTS,
    SHARED_SPRINTS,
    SPRINTS_BEFORE,
)
from tests.conftest import DC_AUTH, DC_URL

ALICE = "712020:00000000-0000-4000-8000-00000000a11c"
BOB = "712020:00000000-0000-4000-8000-000000000b0b"
CAROL = "712020:00000000-0000-4000-8000-00000000ca01"
ERIN = "712020:00000000-0000-4000-8000-00000000e417"
ACME, SCRUM = "10000", "10001"


def _values(issue: dict) -> dict:
    return {value["key"]: value["value"] for value in issue.get("custom_values") or []}


def _details(bundle, journal: str, prop_key: str) -> list[dict]:
    return [d for d in bundle.by_ref("journals")[journal]["details"] if d["prop_key"] == prop_key]


class TestCloudSprints:
    async def test_a_projects_sprints_come_from_its_boards(self, cloud, run_export):
        _, bundle = await run_export()
        sprints = {s["source_ref"]: s for s in bundle.get("sprints")}
        assert sprints["1"] == {
            "source_ref": "1",
            "project_ref": ACME,
            "name": "ACME Sprint 1",
            "goal": "Fix sign-in",
            "status": "completed",
            # The days Jira shows the account that exports (Asia/Bangkok).
            "start_date": "2024-01-29",
            "end_date": "2024-02-12",
            "completed_at": "2024-02-12T04:00:00Z",
            "created_at": "2024-01-25T02:00:00Z",
            "extra": {"jira_sprint_id": "1", "jira_board_id": "1"},
        }
        assert sprints["2"]["status"] == "active"
        # A future sprint no issue is in yet: planned, the default.
        assert sprints["4"]["name"] == "ACME Sprint 3"
        assert "status" not in sprints["4"]
        assert sprints["3"]["project_ref"] == SCRUM

    async def test_an_issue_is_in_its_open_sprint_and_names_the_others(self, cloud, run_export):
        _, bundle = await run_export()
        issues = bundle.by_ref("issues")
        assert issues["10001"]["sprint_ref"] == "2"
        assert issues["10001"]["extra"]["sprints"] == ["ACME Sprint 1", "ACME Sprint 2"]
        assert issues["10101"]["sprint_ref"] == "3"
        assert "sprint_ref" not in issues["10003"]
        assert bundle.not_carried()[SPRINTS_BEFORE] == 1

    async def test_a_sprint_of_another_projects_board_is_one_of_the_issues_project(self, cloud, run_export):
        _, bundle = await run_export()
        sprints = {s["source_ref"]: s for s in bundle.get("sprints")}
        # SCRUM-2 sits in ACME's sprint: SCRUM gets a sprint of its own.
        assert bundle.by_ref("issues")["10102"]["sprint_ref"] == f"2@{SCRUM}"
        copy = sprints[f"2@{SCRUM}"]
        assert (copy["project_ref"], copy["name"], copy["status"]) == (SCRUM, "ACME Sprint 2", "active")
        assert copy["extra"]["shared_with_other_projects"] is True
        assert bundle.not_carried()[SHARED_SPRINTS] == 1
        # The project's own sprints come first: Specivo keeps the first active one active.
        scrum = [s["source_ref"] for s in bundle.get("sprints") if s["project_ref"] == SCRUM]
        assert scrum == ["3", f"2@{SCRUM}"]

    async def test_a_sprint_whose_board_is_gone(self, cloud, run_export):
        _, bundle = await run_export()
        assert bundle.by_ref("issues")["10006"]["sprint_ref"] == f"7@{ACME}"
        assert {s["source_ref"]: s for s in bundle.get("sprints")}[f"7@{ACME}"]["name"] == "Old sprint"
        assert bundle.not_carried()[ORPHAN_SPRINTS] == 1
        assert cloud.calls("GET", "/rest/agile/1.0/board/9")

    async def test_a_project_alone_gets_the_same_refs(self, cloud, run_export):
        _, bundle = await run_export(["SCRUM"])
        assert {s["source_ref"] for s in bundle.get("sprints")} == {"3", f"2@{SCRUM}"}
        assert bundle.by_ref("issues")["10102"]["sprint_ref"] == f"2@{SCRUM}"
        # ACME's board is asked for where it is located.
        assert cloud.calls("GET", "/rest/agile/1.0/board/1")
        assert validate_bundle(bundle.root).ok

    async def test_sprint_history(self, cloud, run_export):
        _, bundle = await run_export()
        (detail,) = _details(bundle, "history:20003", "sprint_id")
        assert detail == {
            "property": "attr",
            "prop_key": "sprint_id",
            "old_ref": "1",
            "new_ref": "2",
            "old_value": "ACME Sprint 1",
            "new_value": "ACME Sprint 2",
        }

    async def test_kanban_boards_are_not_asked_for_sprints(self, cloud, run_export):
        await run_export(["ACME", "SCRUM", "OPS"])
        assert cloud.calls("GET", "/rest/agile/1.0/board/1/sprint")
        assert not cloud.calls("GET", "/rest/agile/1.0/board/3/sprint")

    async def test_without_jira_software_the_issues_name_the_sprints(self, cloud, run_export):
        refused = {"status": 403, "json": {"errorMessages": ["No access"], "errors": {}}}
        cloud.override("GET", "/rest/agile/1.0/board", refused, refused)
        cloud.override("GET", "/rest/agile/1.0/board/1", refused)
        _, bundle = await run_export()
        sprints = {s["source_ref"]: s for s in bundle.get("sprints")}
        # No board says where ACME's sprints belong, so each project has its own.
        assert set(sprints) == {f"2@{ACME}", f"7@{ACME}", f"2@{SCRUM}", "3"}
        assert sprints[f"2@{ACME}"]["goal"] == "Ship the checkout"
        assert bundle.not_carried()[BOARDS_UNREADABLE] == 2
        assert validate_bundle(bundle.root).ok

    async def test_every_sprint_validates(self, cloud, run_export):
        _, bundle = await run_export(["ACME", "SCRUM", "OPS"])
        report = validate_bundle(bundle.root)
        assert report.ok and not report.problems, report.format_text()


class TestCloudCustomFields:
    async def test_definitions_by_type(self, cloud, run_export):
        _, bundle = await run_export()
        fields = {f["key"]: f for f in bundle.get("custom_fields")}
        assert {key: (f["field_format"], f.get("multiple", False)) for key, f in fields.items()} == {
            "story_point_estimate": ("float", False),
            "customer_reference": ("string", False),
            "acceptance_criteria": ("text", False),
            "severity_10032": ("list", False),
            "platforms": ("list", True),
            "reviewers": ("user", True),
            "qa_owner": ("user", False),
            "found_in_version": ("version", False),
            "target_releases": ("version", True),
            "design_link": ("link", False),
            "target_start": ("date", False),
            "deployed_at": ("datetime", False),
            "area": ("list", False),
            "customer_tags": ("string", True),
            "impact": ("list", False),
            # A name with nothing ASCII can spell keeps the field id.
            "customfield_10044": ("string", False),
            "mood": ("list", False),
        }
        assert fields["customfield_10044"]["name"] == "ระดับความเสี่ยง"
        assert fields["reviewers"]["json_schema"] == {"type": "array", "items": {"type": "integer"}}
        assert fields["deployed_at"]["json_schema"] == {"type": "string", "format": "date-time"}

    async def test_option_fields_list_their_options(self, cloud, run_export):
        _, bundle = await run_export()
        fields = {f["key"]: f for f in bundle.get("custom_fields")}
        # A disabled option is listed too: issues may still hold it.
        assert fields["severity_10032"]["json_schema"] == {
            "type": "string",
            "enum": ["Low", "Medium", "High", "Critical"],
        }
        assert fields["platforms"]["json_schema"]["items"]["enum"] == ["Web", "iOS", "Android"]
        assert fields["area"]["json_schema"]["enum"] == [
            "Checkout",
            "Checkout / Payment",
            "Checkout / Shipping",
            "Account",
            "Account / Sign-in",
        ]

    async def test_scoping_follows_the_contexts(self, cloud, run_export):
        _, bundle = await run_export()
        fields = {f["key"]: f for f in bundle.get("custom_fields")}
        # A global context: every project.
        assert fields["platforms"]["is_for_all"] is True
        # A context for ACME's bugs.
        severity = fields["severity_10032"]
        assert (severity.get("is_for_all", False), severity["project_refs"], severity["tracker_refs"]) == (
            False,
            [ACME],
            ["10004"],
        )
        # Contexts Jira would not list: the projects whose issues use the field.
        assert fields["customer_reference"]["project_refs"] == [ACME]
        assert bundle.not_carried()[CONTEXTS_UNREADABLE] == 1
        # A team-managed project's own field.
        assert fields["mood"]["project_refs"] == [SCRUM]
        assert fields["mood"]["extra"]["team_managed"] is True

    async def test_values_on_issues(self, cloud, run_export):
        _, bundle = await run_export()
        values = _values(bundle.by_ref("issues")["10001"])
        assert values == {
            "story_point_estimate": 5,
            "customer_reference": "PO-4471",
            # Rich text as Markdown, a mention as a name and a key as text: values are stored as they are.
            "acceptance_criteria": "Works in **Safari 17**, checked by @Dan Example as in ACME-2.",
            "severity_10032": "High",
            "platforms": ["Web", "iOS"],
            # The app account is left out.
            "reviewers": [ALICE, BOB],
            "qa_owner": BOB,
            "found_in_version": "10400",
            "target_releases": ["10400", "10402"],
            "design_link": "https://design.example.com/login",
            "target_start": "2024-02-12",
            "deployed_at": "2024-02-20T14:30:00+07:00",
            "area": "Checkout / Payment",
            "customer_tags": ["vip", "enterprise"],
            "impact": "Many users",
        }
        kinds = {v["key"]: v.get("value_kind", "scalar") for v in bundle.by_ref("issues")["10001"]["custom_values"]}
        assert (kinds["reviewers"], kinds["found_in_version"], kinds["area"]) == ("user_ref", "version_ref", "scalar")
        assert bundle.not_carried()[CF_USERS_LEFT_OUT] == 1
        assert _values(bundle.by_ref("issues")["10101"]) == {
            "story_point_estimate": 3.0,
            "customfield_10044": "ต่ำ",
            "mood": "Happy",
        }

    async def test_a_user_only_a_field_names_is_carried(self, cloud, run_export):
        _, bundle = await run_export(["ACME"])
        # Erin is one of ACME-2's reviewers, and nothing else.
        assert bundle.by_ref("users")[ERIN]["display_name"] == "Erin Example"
        assert _values(bundle.by_ref("issues")["10002"])["reviewers"] == [CAROL, ERIN]

    async def test_history(self, cloud, run_export):
        _, bundle = await run_export()
        details = [d for d in bundle.by_ref("journals")["history:20003"]["details"] if d["property"] == "cf"]
        assert details == [
            {"property": "cf", "prop_key": "customfield_10032", "old_value": "Low", "new_value": "High"},
            {
                "property": "cf",
                "prop_key": "customfield_10035",
                "old_ref": CAROL,
                "new_ref": BOB,
                "old_value": "Carol Example",
                "new_value": "Bob Example",
            },
            # A field of several values: a detail per value added or removed.
            {"property": "cf", "prop_key": "customfield_10034", "new_ref": BOB, "new_value": "Bob Example"},
            {"property": "cf", "prop_key": "customfield_10037", "new_ref": "10402", "new_value": "2.0"},
        ]
        (text,) = _details(bundle, "history:20002", "customfield_10031")
        assert text == {"property": "cf", "prop_key": "customfield_10031", "new_value": "Works in Safari 17"}

    async def test_contexts_are_read_once_per_field(self, cloud, run_export):
        await run_export()
        assert len(cloud.calls("GET", "/rest/api/3/field/customfield_10032/context")) == 1
        # Team-managed fields have no contexts to read.
        assert not cloud.calls("GET", "/rest/api/3/field/customfield_10050/context")
        # Nor do fields that are left out.
        assert not cloud.calls("GET", "/rest/api/3/field/customfield_10019/context")


class TestCloudTags:
    async def test_labels_are_tags(self, cloud, run_export):
        _, bundle = await run_export()
        issues = bundle.by_ref("issues")
        assert issues["10001"]["tags"] == ["login", "safari"]
        assert issues["10002"]["tags"] == ["login"]
        assert "tags" not in issues["10003"]

    async def test_a_label_too_long_for_a_tag_is_kept_and_counted(self, cloud, run_export):
        _, bundle = await run_export()
        assert bundle.by_ref("issues")["10001"]["extra"]["labels"] == ["x" * 70]
        assert bundle.not_carried()[LONG_LABELS] == 1


class TestDataCenterSprintsAndFields:
    async def _run(self, run_export):
        return await run_export(["SUP"], url=DC_URL, auth=DC_AUTH)

    async def test_sprints_from_the_board_and_the_sprint_strings(self, datacenter, run_export):
        _, bundle = await self._run(run_export)
        sprints = {s["source_ref"]: s for s in bundle.get("sprints")}
        assert sprints["11"]["name"] == "SUP Sprint 1, the first"
        assert sprints["11"]["status"] == "completed"
        assert sprints["11"]["completed_at"] == "2024-02-12T11:00:00+07:00"
        assert sprints["12"]["status"] == "active"
        issue = bundle.by_ref("issues")["20001"]
        assert issue["sprint_ref"] == "12"
        assert issue["tags"] == ["support"]
        assert validate_bundle(bundle.root).ok

    async def test_fields_values_and_history(self, datacenter, run_export):
        _, bundle = await self._run(run_export)
        fields = {f["key"]: f for f in bundle.get("custom_fields")}
        # No contexts on Data Center: the projects whose issues use the field.
        assert fields["severity"]["project_refs"] == ["10200"]
        # The options history names are options too.
        assert fields["severity"]["json_schema"]["enum"] == ["High", "Low"]
        assert _values(bundle.by_ref("issues")["20001"]) == {
            "severity": "High",
            # The user key, as for every Data Center user.
            "reviewer": "bob",
            "release_notes": "Fixed the **login** page for @Alice Example.",
        }
        details = bundle.by_ref("journals")["history:40001"]["details"]
        assert [d for d in details if d["prop_key"] == "sprint_id"] == [
            {
                "property": "attr",
                "prop_key": "sprint_id",
                "old_ref": "11",
                "new_ref": "12",
                "old_value": "SUP Sprint 1, the first",
                "new_value": "SUP Sprint 2",
            }
        ]
        reviewer = next(d for d in details if d["prop_key"] == "customfield_10201")
        # History names a user by user name; the bundle by user key.
        assert (reviewer["old_ref"], reviewer["new_ref"]) == ("JIRAUSER10100", "bob")
        assert not datacenter.calls("GET", "/rest/api/3/field/customfield_10200/context")
