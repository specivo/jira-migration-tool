"""Jira custom fields as Specivo custom fields.

Specivo keeps an issue's custom values in its metadata, checked against a JSON
Schema per project (and issue type); a bundle's ``custom_fields.jsonl`` says
what each field is (``field_format``, ``json_schema``, whether it holds several
values) and where it applies. This module decides that for one Jira field,
from the ``schema`` Jira gives in its field list:

======================================  ==============  ===============================
Jira (``schema.custom`` or type)         field_format    value
======================================  ==============  ===============================
textfield, readonlyfield                 ``string``      the text
textarea                                 ``text``        Markdown (ADF or wiki markup)
float (story points too)                 ``float``       a number
datepicker / datetime                    ``date`` /      ``2026-11-19`` / a timestamp
                                         ``datetime``
select, radiobuttons                     ``list``        the option's value
multiselect, multicheckboxes             ``list``, many  the options' values
cascadingselect                          ``list``        ``Parent / Child``
userpicker / multiuserpicker, people     ``user``        the user's ``source_ref``
version / multiversion                   ``version``     the version's ``source_ref``
url                                      ``link``        the URL
labels                                   ``string``,     the labels
                                         many
grouppicker / multigrouppicker           ``string``      the group names
project                                  ``string``      the project's key
======================================  ==============  ===============================

Fields Jira keeps for its own screens or that another part of the bundle
carries are left out (:data:`SKIPPED_FIELDS`): Rank, the Sprint and Epic Link
fields (carried as the issue's sprint and parent), epic name, colour and
status, Team, Development, Parent Link and the like. So is a field of a type
this table does not know. Their values are counted in the manifest.

A field's key (where its values are stored, and what the issue page labels it
by) is its name in snake case; names with no letters or digits that ASCII
can spell (a Thai name, say) use the field id. Two fields whose names give the
same key both add their number, so the key does not depend on which fields an
export happens to carry.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

type JSON = dict[str, Any]
type ValueKind = Literal["scalar", "user_ref", "version_ref"]

_PREFIX = "com.atlassian.jira.plugin.system.customfieldtypes:"
_AGILE = "com.pyxis.greenhopper.jira:"

# The Sprint field of Jira Software, carried as the issue's sprint.
SPRINT_FIELD = _AGILE + "gh-sprint"

# Custom fields left out, by ``schema.custom``, with what the manifest says about them.
SKIPPED_FIELDS: dict[str, str] = {
    _AGILE + "gh-lexo-rank": "Jira's ordering of the backlog",
    SPRINT_FIELD: "carried as the issue's sprint",
    _AGILE + "gh-epic-link": "carried as the issue's parent",
    _AGILE + "gh-epic-label": "an epic's name, kept by Jira for its boards",
    _AGILE + "gh-epic-color": "an epic's colour on Jira's boards",
    _AGILE + "gh-epic-status": "an epic's status on Jira's boards",
    "com.atlassian.jpo:jpo-custom-field-parent": "Advanced Roadmaps' parent link",
    "com.atlassian.jira.plugins.jira-development-integration-plugin:devsummary": "read-only development data",
    "com.atlassian.jira.plugins.jira-development-integration-plugin:devsummarycf": "read-only development data",
    "com.atlassian.jira.plugins.jira-development-integration-plugin:designcf": "read-only design data",
    "com.atlassian.jira.plugins.jira-development-integration-plugin:vulnerabilitycf": "read-only security data",
    "com.atlassian.teams:rm-teams-custom-field-team": "Atlassian teams",
    _PREFIX + "atlassian-team": "Atlassian teams",
    "com.atlassian.jira.ext.charting:timeinstatus": "computed by Jira",
    "com.atlassian.jira.ext.charting:firstresponsedate": "computed by Jira",
    "com.atlassian.jira.toolkit:lastupdaterorcommenter": "computed by Jira",
    "com.atlassian.jira.toolkit:participants": "computed by Jira",
    "com.atlassian.jira.toolkit:message": "a message shown on Jira's screens",
    "com.atlassian.jira.toolkit:viewmessage": "a message shown on Jira's screens",
}

# By ``schema.custom``: (field_format, several values, value kind).
_BY_CUSTOM: dict[str, tuple[str, bool, ValueKind]] = {
    _PREFIX + "textfield": ("string", False, "scalar"),
    _PREFIX + "readonlyfield": ("string", False, "scalar"),
    _PREFIX + "textarea": ("text", False, "scalar"),
    _PREFIX + "float": ("float", False, "scalar"),
    _AGILE + "jsw-story-points": ("float", False, "scalar"),
    _PREFIX + "datepicker": ("date", False, "scalar"),
    _PREFIX + "datetime": ("datetime", False, "scalar"),
    _PREFIX + "select": ("list", False, "scalar"),
    _PREFIX + "radiobuttons": ("list", False, "scalar"),
    _PREFIX + "multiselect": ("list", True, "scalar"),
    _PREFIX + "multicheckboxes": ("list", True, "scalar"),
    _PREFIX + "cascadingselect": ("list", False, "scalar"),
    _PREFIX + "userpicker": ("user", False, "user_ref"),
    _PREFIX + "multiuserpicker": ("user", True, "user_ref"),
    _PREFIX + "people": ("user", True, "user_ref"),
    _PREFIX + "version": ("version", False, "version_ref"),
    _PREFIX + "multiversion": ("version", True, "version_ref"),
    _PREFIX + "url": ("link", False, "scalar"),
    _PREFIX + "labels": ("string", True, "scalar"),
    _PREFIX + "grouppicker": ("string", False, "scalar"),
    _PREFIX + "multigrouppicker": ("string", True, "scalar"),
    _PREFIX + "project": ("string", False, "scalar"),
}

# By ``schema.type`` (and ``schema.items`` for an array), for a type ``_BY_CUSTOM`` does not name.
_BY_TYPE: dict[tuple[str, str | None], tuple[str, bool, ValueKind]] = {
    ("string", None): ("string", False, "scalar"),
    ("number", None): ("float", False, "scalar"),
    ("date", None): ("date", False, "scalar"),
    ("datetime", None): ("datetime", False, "scalar"),
    ("option", None): ("list", False, "scalar"),
    ("option-with-child", None): ("list", False, "scalar"),
    ("array", "option"): ("list", True, "scalar"),
    ("user", None): ("user", False, "user_ref"),
    ("array", "user"): ("user", True, "user_ref"),
    ("version", None): ("version", False, "version_ref"),
    ("array", "version"): ("version", True, "version_ref"),
    ("array", "string"): ("string", True, "scalar"),
    ("group", None): ("string", False, "scalar"),
    ("array", "group"): ("string", True, "scalar"),
    ("project", None): ("string", False, "scalar"),
}

_JSON_SCHEMAS: dict[str, JSON] = {
    "string": {"type": "string"},
    "text": {"type": "string"},
    "float": {"type": "number"},
    "date": {"type": "string", "format": "date"},
    "datetime": {"type": "string", "format": "date-time"},
    "link": {"type": "string", "format": "uri"},
    "list": {"type": "string"},
    "user": {"type": "integer"},
    "version": {"type": "integer"},
}

_KEY_UNSAFE_RE = re.compile(r"[^a-z0-9]+")
_FIELD_NUMBER_RE = re.compile(r"(\d+)$")
# Cascading select: a parent option and one of its children.
CASCADE_SEPARATOR = " / "


@dataclass(frozen=True)
class FieldKind:
    """What a Jira field is in the bundle."""

    field_format: str
    multiple: bool
    value_kind: ValueKind
    # Rich text: ADF on Cloud, wiki markup on Data Center, written as Markdown.
    rich: bool = False
    cascading: bool = False
    # Jira's ``schema.custom``.
    custom: str = ""

    def json_schema(self, options: Iterable[str] = ()) -> JSON:
        """The JSON Schema one value matches; an option field's lists its options."""
        item = dict(_JSON_SCHEMAS[self.field_format])
        listed = list(dict.fromkeys(options))
        if self.field_format == "list" and listed:
            item["enum"] = listed
        return {"type": "array", "items": item} if self.multiple else item


def custom_type(field: Mapping[str, Any]) -> str:
    schema = field.get("schema") or {}
    return str(schema.get("custom") or "")


def classify(field: Mapping[str, Any]) -> FieldKind | str:
    """What *field* (an entry of Jira's field list) is in the bundle, or why it is left out."""
    schema = field.get("schema") or {}
    custom = custom_type(field)
    if custom in SKIPPED_FIELDS:
        return SKIPPED_FIELDS[custom]
    kind_type = str(schema.get("type") or "")
    known = _BY_CUSTOM.get(custom)
    if known is None and custom and not custom.startswith(_PREFIX):
        # An app's own type: its values may mean anything.
        return f"a field of type {custom}"
    if known is None:
        items = str(schema.get("items")) if kind_type == "array" and schema.get("items") else None
        known = _BY_TYPE.get((kind_type, items))
    if known is None:
        return f"a field of type {custom or kind_type or 'unknown'}"
    field_format, multiple, value_kind = known
    return FieldKind(
        field_format=field_format,
        multiple=multiple,
        value_kind=value_kind,
        rich=field_format == "text",
        cascading=custom == _PREFIX + "cascadingselect" or kind_type == "option-with-child",
        custom=custom,
    )


def field_keys(fields: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """The metadata key of every custom field in Jira's field list, by field id.

    A name's snake case, or the field id when that leaves nothing; when two
    fields' names give the same key, each adds its number (``severity_10032``).
    """
    wanted: dict[str, str] = {}
    for field in fields:
        if not field.get("custom") or not field.get("id"):
            continue
        field_id = str(field["id"])
        wanted[field_id] = _key_text(str(field.get("name") or "")) or _key_text(field_id) or "field"
    taken: dict[str, int] = {}
    for key in wanted.values():
        taken[key] = taken.get(key, 0) + 1
    keys: dict[str, str] = {}
    for field_id, key in wanted.items():
        if taken[key] > 1:
            number = _FIELD_NUMBER_RE.search(field_id)
            key = f"{key}_{number.group(1) if number else _key_text(field_id)}"
        keys[field_id] = key
    return keys


def _key_text(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    key = _KEY_UNSAFE_RE.sub("_", folded).strip("_")[:60].strip("_")
    if key and not (key[0].isalpha() or key[0] == "_"):
        key = f"field_{key}"
    return key


def option_text(value: object, *, cascading: bool = False) -> str | None:
    """An option value as text: its ``value`` and, for a cascading select, ``Parent / Child``."""
    if isinstance(value, str):
        return value or None
    if not isinstance(value, dict):
        return None
    text = str(value.get("value") or value.get("name") or "").strip()
    if not text:
        return None
    child = value.get("child")
    if cascading and isinstance(child, dict) and str(child.get("value") or "").strip():
        return f"{text}{CASCADE_SEPARATOR}{str(child['value']).strip()}"
    return text


def context_options(options: Iterable[Mapping[str, Any]], *, cascading: bool) -> list[str]:
    """A context's options as values, in Jira's order; a cascading select's as parents and ``Parent / Child``."""
    listed = [option for option in options if isinstance(option, Mapping)]
    parents = {str(o["id"]): str(o.get("value") or "") for o in listed if o.get("id") and not o.get("optionId")}
    values: list[str] = []
    for option in listed:
        text = str(option.get("value") or "").strip()
        if not text:
            continue
        parent = option.get("optionId")
        if parent and cascading:
            parent_text = parents.get(str(parent), "").strip()
            values.append(f"{parent_text}{CASCADE_SEPARATOR}{text}" if parent_text else text)
        elif not parent:
            values.append(text)
    return values


def history_list(value: object) -> list[str]:
    """The values a change history item gives for a field of several values: ``[a, b]`` or ``a, b``."""
    text = str(value or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [part.strip() for part in text.split(",") if part.strip()]


def empty(value: object) -> bool:
    """Whether a field value holds nothing (Jira writes an unset field as null, ``[]`` or ``""``)."""
    if value is None or value == "" or value == [] or value == {}:
        return True
    if isinstance(value, dict):
        return all(empty(item) for item in value.values())
    return False


def by_name(field_list: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Custom field ids by name, in lower case, for names only one custom field has.

    Data Center's change history names a custom field by its name only.
    """
    seen: dict[str, list[str]] = {}
    for field in field_list:
        if field.get("custom") and field.get("id") and field.get("name"):
            seen.setdefault(str(field["name"]).casefold(), []).append(str(field["id"]))
    return {name: ids[0] for name, ids in seen.items() if len(ids) == 1}


def history_field_id(change: Mapping[str, Any], names: Mapping[str, str]) -> str | None:
    """The custom field a change history item is about: its ``fieldId``, or else its name."""
    field_id = str(change.get("fieldId") or "")
    if field_id.startswith("customfield_"):
        return field_id
    if str(change.get("fieldtype") or "") != "custom":
        return None
    return names.get(str(change.get("field") or "").casefold())


def fields_in_use(items: Iterable[Mapping[str, Any]], field_list: Iterable[Mapping[str, Any]]) -> set[str]:
    """The custom fields that hold a value on one of *items* (issues as the state keeps them), or that
    their change history names."""
    names = by_name(list(field_list))
    found: set[str] = set()
    for item in items:
        values = (item.get("issue") or {}).get("fields") or {}
        found |= {name for name, value in values.items() if name.startswith("customfield_") and not empty(value)}
        for history in item.get("histories") or []:
            for change in history.get("items") or []:
                field_id = history_field_id(change, names)
                if field_id is not None:
                    found.add(field_id)
    return found
