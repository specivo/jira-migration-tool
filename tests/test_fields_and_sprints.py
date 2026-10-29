"""Custom field types and keys, and sprint values, one rule at a time."""

from __future__ import annotations

from datetime import date

import pytest

from specivo_jira_migration import fields, sprints

P = "com.atlassian.jira.plugin.system.customfieldtypes:"


def _field(custom: str, kind: str, items: str | None = None, **extra: object) -> dict:
    schema: dict = {"type": kind, "custom": custom}
    if items:
        schema["items"] = items
    return {"id": "customfield_10100", "name": "Field", "custom": True, "schema": schema, **extra}


class TestClassify:
    @pytest.mark.parametrize(
        ("custom", "kind", "items", "expected"),
        [
            (P + "textfield", "string", None, ("string", False, "scalar")),
            (P + "textarea", "string", None, ("text", False, "scalar")),
            (P + "float", "number", None, ("float", False, "scalar")),
            ("com.pyxis.greenhopper.jira:jsw-story-points", "number", None, ("float", False, "scalar")),
            (P + "datepicker", "date", None, ("date", False, "scalar")),
            (P + "datetime", "datetime", None, ("datetime", False, "scalar")),
            (P + "select", "option", None, ("list", False, "scalar")),
            (P + "radiobuttons", "option", None, ("list", False, "scalar")),
            (P + "multiselect", "array", "option", ("list", True, "scalar")),
            (P + "multicheckboxes", "array", "option", ("list", True, "scalar")),
            (P + "cascadingselect", "option-with-child", None, ("list", False, "scalar")),
            (P + "userpicker", "user", None, ("user", False, "user_ref")),
            (P + "multiuserpicker", "array", "user", ("user", True, "user_ref")),
            (P + "version", "version", None, ("version", False, "version_ref")),
            (P + "multiversion", "array", "version", ("version", True, "version_ref")),
            (P + "url", "string", None, ("link", False, "scalar")),
            (P + "labels", "array", "string", ("string", True, "scalar")),
            (P + "grouppicker", "group", None, ("string", False, "scalar")),
            (P + "multigrouppicker", "array", "group", ("string", True, "scalar")),
            # A system type the table does not name, read by its schema type.
            (P + "importid", "number", None, ("float", False, "scalar")),
        ],
    )
    def test_types(self, custom, kind, items, expected):
        found = fields.classify(_field(custom, kind, items))
        assert isinstance(found, fields.FieldKind)
        assert (found.field_format, found.multiple, found.value_kind) == expected

    def test_a_cascading_select_is_flagged(self):
        found = fields.classify(_field(P + "cascadingselect", "option-with-child"))
        assert isinstance(found, fields.FieldKind) and found.cascading

    @pytest.mark.parametrize(
        "custom",
        [
            "com.pyxis.greenhopper.jira:gh-lexo-rank",
            "com.pyxis.greenhopper.jira:gh-sprint",
            "com.pyxis.greenhopper.jira:gh-epic-link",
            "com.pyxis.greenhopper.jira:gh-epic-label",
            "com.pyxis.greenhopper.jira:gh-epic-color",
            "com.atlassian.jira.plugins.jira-development-integration-plugin:devsummarycf",
            P + "atlassian-team",
        ],
    )
    def test_jiras_own_fields_are_left_out_with_a_reason(self, custom):
        assert isinstance(fields.classify(_field(custom, "any")), str)

    def test_an_apps_type_is_left_out(self):
        assert fields.classify(_field("com.example.widgets:gauge", "number")) == (
            "a field of type com.example.widgets:gauge"
        )

    def test_a_type_nothing_knows(self):
        assert fields.classify(_field(P + "something", "any")) == f"a field of type {P}something"

    def test_json_schemas(self):
        many = fields.FieldKind("list", True, "scalar")
        assert many.json_schema(["Web", "iOS", "Web"]) == {
            "type": "array",
            "items": {"type": "string", "enum": ["Web", "iOS"]},
        }
        assert fields.FieldKind("list", False, "scalar").json_schema() == {"type": "string"}
        assert fields.FieldKind("date", False, "scalar").json_schema() == {"type": "string", "format": "date"}


class TestKeys:
    def test_snake_case_from_the_name(self):
        keys = fields.field_keys([{"id": "customfield_1", "name": "Story point estimate", "custom": True}])
        assert keys == {"customfield_1": "story_point_estimate"}

    def test_names_that_give_one_key_add_their_numbers(self):
        keys = fields.field_keys(
            [
                {"id": "customfield_10032", "name": "Severity", "custom": True},
                {"id": "customfield_10051", "name": "severity ", "custom": True},
                {"id": "customfield_10052", "name": "Impact", "custom": True},
            ]
        )
        assert keys == {
            "customfield_10032": "severity_10032",
            "customfield_10051": "severity_10051",
            "customfield_10052": "impact",
        }

    def test_a_name_ascii_cannot_spell_uses_the_id(self):
        keys = fields.field_keys([{"id": "customfield_10044", "name": "ระดับความเสี่ยง", "custom": True}])
        assert keys == {"customfield_10044": "customfield_10044"}

    def test_a_key_starts_with_a_letter(self):
        keys = fields.field_keys([{"id": "customfield_7", "name": "2nd reviewer", "custom": True}])
        assert keys == {"customfield_7": "field_2nd_reviewer"}

    def test_system_fields_have_no_key(self):
        assert fields.field_keys([{"id": "summary", "name": "Summary", "custom": False}]) == {}


class TestValues:
    def test_option_text(self):
        assert fields.option_text({"value": "High", "id": "1"}) == "High"
        cascade = {"value": "Checkout", "child": {"value": "Payment"}}
        assert fields.option_text(cascade, cascading=True) == "Checkout / Payment"
        assert fields.option_text({"value": ""}) is None

    def test_context_options_of_a_cascading_select(self):
        options = [
            {"id": "1", "value": "Checkout"},
            {"id": "2", "value": "Payment", "optionId": "1"},
            {"id": "3", "value": "Account"},
        ]
        assert fields.context_options(options, cascading=True) == ["Checkout", "Checkout / Payment", "Account"]

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("[a, b]", ["a", "b"]), ("10400, 10402", ["10400", "10402"]), ("1", ["1"]), (None, []), ("", [])],
    )
    def test_history_lists(self, value, expected):
        assert fields.history_list(value) == expected

    def test_a_custom_field_change_found_by_name_on_data_center(self):
        names = fields.by_name([{"id": "customfield_1", "name": "Severity", "custom": True}])
        assert fields.history_field_id({"field": "Severity", "fieldtype": "custom"}, names) == "customfield_1"
        assert fields.history_field_id({"field": "status", "fieldtype": "jira"}, names) is None
        assert fields.history_field_id({"fieldId": "customfield_9", "field": "X"}, names) == "customfield_9"

    def test_a_name_two_fields_share_is_not_guessed(self):
        names = fields.by_name(
            [
                {"id": "customfield_1", "name": "Severity", "custom": True},
                {"id": "customfield_2", "name": "Severity", "custom": True},
            ]
        )
        assert fields.history_field_id({"field": "Severity", "fieldtype": "custom"}, names) is None


LEGACY = (
    "com.atlassian.greenhopper.service.sprint.Sprint@6f00a8[id=11,rapidViewId=10,state=CLOSED,name=Sprint 1, the first,"
    "goal=,startDate=2024-01-29T10:00:00.000+07:00,endDate=2024-02-12T10:00:00.000+07:00,"
    "completeDate=2024-02-12T11:00:00.000+07:00,activatedDate=2024-01-29T10:00:00.000+07:00,sequence=11,"
    "synced=false,autoStartStop=false,incompleteIssuesDestinationId=<null>]"
)


class TestSprints:
    def test_a_data_center_sprint_string(self):
        sprint = sprints.parse_sprint(LEGACY)
        assert sprint is not None
        assert (sprint.id, sprint.name, sprint.state, sprint.goal, sprint.board_id) == (
            "11",
            "Sprint 1, the first",
            "closed",
            None,
            "10",
        )
        assert sprint.completed == "2024-02-12T11:00:00.000+07:00"

    def test_a_cloud_sprint_object(self):
        sprint = sprints.parse_sprint({"id": 2, "name": "Sprint 2", "state": "active", "boardId": 1, "goal": "Ship"})
        assert sprint is not None
        assert (sprint.id, sprint.state, sprint.goal, sprint.board_id) == ("2", "active", "Ship", "1")

    @pytest.mark.parametrize("value", ["not a sprint", {"name": "no id"}, None, 3])
    def test_what_is_not_a_sprint(self, value):
        assert sprints.parse_sprint(value) is None

    def _known(self) -> dict[str, sprints.SprintInfo]:
        return {
            "1": sprints.SprintInfo("1", "One", "closed", completed="2024-02-12T04:00:00Z"),
            "2": sprints.SprintInfo("2", "Two", "closed", completed="2024-02-26T04:00:00Z"),
            "3": sprints.SprintInfo("3", "Three", "active"),
            "4": sprints.SprintInfo("4", "Four", "future"),
        }

    @pytest.mark.parametrize(
        ("ids", "expected"),
        [
            (["1", "2"], "2"),
            (["2", "1"], "2"),
            (["1", "3"], "3"),
            (["1", "4"], "4"),
            (["4", "3"], "3"),
            (["1", "99"], "1"),
            ([], None),
        ],
    )
    def test_the_sprint_an_issue_is_in(self, ids, expected):
        assert sprints.pick(ids, self._known()) == expected

    def test_dates_in_the_zone_of_the_account(self):
        assert sprints.sprint_date("2024-01-28T20:00:00.000Z", "Asia/Bangkok") == date(2024, 1, 29)
        assert sprints.sprint_date("2024-01-28T20:00:00.000Z", None) == date(2024, 1, 28)
        assert sprints.sprint_date("2024-01-28T20:00:00.000Z", "Nowhere/Else") == date(2024, 1, 28)
        assert sprints.sprint_date(None, "Asia/Bangkok") is None
