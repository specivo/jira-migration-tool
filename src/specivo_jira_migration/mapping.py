"""Turn what was read from Jira into bundle records.

Everything here works on the state directory's contents alone (no network),
in a fixed order, so the same state always gives the same records.

Source refs:

- issue: the Jira issue **id** (``10042``), not its key. The id never changes,
  while a key changes when the issue moves to another project or the
  project's key is renamed, so the id is what lets a later export of the same
  site continue the first one. The key is kept in ``extra.jira_key``.
- project: the project id; tracker, status, priority: their ids.
- user: the Cloud ``accountId``, or the Data Center user ``key``.
- journal: ``comment:<id>`` for a comment, ``history:<id>`` for a change-history entry.
- attachment: the Jira attachment id.

Attachments: each file the state holds becomes a record on its issue, under
the name Jira shows, its bytes named by SHA-256. Specivo resolves an
attachment placeholder to the file name and the issue's newest attachment of
that name, so when one issue holds several files of the same name the later
ones are numbered (``image (2).png``) and the name Jira gave kept in
``extra.jira_filename``. A file that was not downloaded is counted by reason.
Attachments added and removed in the change history are journal details
(``property`` ``attachment``, ``prop_key`` the attachment id, the file name as
the new or old value), whether or not the file is still there.

Issue links: each Jira link is one relation, its ``source_ref`` the link id
(Jira lists a link on both of its issues; it is written once). The link type
decides the relation type and its direction, see :data:`LINK_TYPES`; a type
Specivo has no equivalent for becomes ``relates``, with Jira's names in
``extra``. A link to an issue outside the bundle (another project, exported
to another bundle, or past ``--limit``) is a relation all the same, its far
end the other issue's id: every bundle that holds one of its two issues
carries it, under the same ``source_ref``, and Specivo makes it once, when
both are imported. Only a link to an issue Jira no longer has (deleted
between the search and the read) is counted and kept in the issue's
``extra.jira_links``. A relation always has at least one end in the bundle:
links are read from the bundle's own issues.

Parents: an issue's ``parent`` (a sub-task's parent, an epic's child on Cloud
and in team-managed projects) or, without one, its Epic Link field (Data
Center, and older Cloud sites) becomes ``parent_ref``, the parent's id,
whether or not the parent is in the bundle. The Epic Link field gives a key
only; one of an issue outside the bundle is looked up with the keys in text
(see :mod:`.fetch`). A parent whose id is not known that way, or that Jira no
longer has, is counted and kept in ``extra.jira_parent``. Specivo loads
projects in the order the bundle lists them, so a project holding parents of
another project's issues in the same bundle is listed before it where it can
be.

Worklogs: each becomes a time entry (``source_ref`` the worklog id) on its
issue and project, under one activity, "Development" (Jira has no activities).
The day it was spent is the date of ``started`` in the author's time zone, as
Jira shows it to them, or else in the offset Jira gave; the hours are
``timeSpentSeconds`` rounded to hundredths, as Specivo keeps them, with the
seconds in ``extra`` when that rounded them. A comment is plain text in
Specivo, so ADF is flattened to text, a line per paragraph. The comment of a
worklog restricted to a group or role is left out and counted, as Specivo shows
a time entry to everyone who may see the project's time; the restriction is
kept in ``extra.jira_visibility``.

Components and versions: each project's components become its categories and
its versions its versions (released: closed; archived: locked, as nothing new
may target it; otherwise open), with their dates and description. Specivo has
one category and one target version per issue: an issue's first component is
its category, and of its fix versions the one released last (or, when none is
released, the last listed) is its target. The rest are counted and kept in
``extra``, and so are affects versions, which Specivo has no field for.

Change history of these lists: Jira's fix versions and components hold
several values, Specivo's target version and category one. Each history
entry that changes the list is a journal detail on ``fixed_version_id`` /
``category_id`` when the value the rule above picks changes. The list before each entry is
rebuilt by walking the history back from the issue's list now; values the
issue still has keep its order, the others follow in the project's order.
An entry that leaves the picked value alone writes no detail.

Change history of the parent (``parent``, ``IssueParentAssociation``, Epic
Link) is a ``parent_id`` detail: the parents' ids as refs, in the bundle or not,
and their keys as text. Cloud writes one change under two fields; it is one
detail. The epic's side of it (Epic Child) is left out, as the child's history
carries it.

History values that name a record (status, priority, issue type, assignee,
target version, category) are refs, ``old_ref`` / ``new_ref``: the record's id
as everywhere in the bundle (a Data Center assignee's user name becomes the
user key), with the name Jira shows for it as ``old_value`` / ``new_value``,
which Specivo shows when the record did not come across (a version since
deleted, say).

Estimates: the original estimate (``timeoriginalestimate``, seconds) is the
issue's ``estimated_hours`` and the remaining estimate (``timeestimate``) its
``remaining_estimate``, both rounded to hundredths of an hour, with the
seconds in ``extra`` when that rounded them. Changes of the original estimate
are ``estimated_hours`` details in the same form. What Jira writes into the
history when work is logged (time spent, remaining estimate, worklog ids, see
:data:`DERIVED_HISTORY_FIELDS`) follows from the worklogs, which are carried
as time entries, so it is neither a detail nor counted: Specivo keeps no
history of the remaining estimate, only its value now.

Watchers: the watchers Jira lists for an issue, as they are; the reporter and
the assignee only when Jira lists them. Watchers Jira refused to list (403),
did not list, or that are app accounts are counted.

Labels are the issue's tags (Specivo's tags belong to a project, and a label is
a tag of each project whose issues carry it); one longer than a tag can be is
counted and kept in ``extra.labels``.

Sprints (see :mod:`.sprints`): each project gets the sprints of the boards
located in it and each sprint of another board its issues are in, under a ref
of its own; an issue is in the sprint :func:`.sprints.pick` takes, and a Sprint
field change is a ``sprint_id`` detail when the picked sprint changes.

Custom fields (see :mod:`.fields`): the fields the issues use, of a type the
bundle can hold, with their values; scoped by their contexts where Jira lists
them, and otherwise to the projects whose issues use them. A change is a
``cf`` detail: by ref for a user or version field (a detail per value added or
removed for a field of several), as Jira's text otherwise.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from specivo_migration_sdk import (
    Activity,
    Attachment,
    Category,
    CustomField,
    CustomValue,
    Issue,
    Journal,
    JournalDetail,
    Priority,
    Project,
    Relation,
    Sprint,
    Status,
    TimeEntry,
    Tracker,
    User,
    Version,
    Watcher,
    date_value,
    source_ref,
    timestamp,
)
from specivo_migration_sdk.models import RelationType, StatusCategory, VersionStatus

from specivo_jira_migration import adf, convert, sprints
from specivo_jira_migration import fields as jira_fields
from specivo_jira_migration.client import Deployment
from specivo_jira_migration.convert import AttachmentTarget, TextContext
from specivo_jira_migration.fetch import (
    FORBIDDEN,
    GONE,
    LISTED,
    STORED,
    TOO_LARGE,
    UNAVAILABLE,
    epic_link,
    epic_link_fields,
    is_user,
    issue_attachments,
    sprint_fields,
    user_key,
    user_objects,
    watch_count,
)

type JSON = dict[str, Any]

# Jira's status categories, by key, in Specivo's grouping.
STATUS_CATEGORIES: dict[str, StatusCategory] = {
    "new": "backlog",
    "indeterminate": "active",
    "done": "closed",
    "undefined": "backlog",
}

# The ref of the priority given to issues Jira has none for (a team-managed
# project can switch the priority field off).
NO_PRIORITY_REF = "none"

# Change history fields carried as journal details, by Jira field id, and the
# issue attribute each one is in Specivo's history.
HISTORY_FIELDS = {
    "status": "status_id",
    "assignee": "assigned_to_id",
    "priority": "priority_id",
    "summary": "subject",
    "issuetype": "tracker_id",
}
# Attributes whose values are text (fromString/toString) rather than ids (from/to).
TEXT_HISTORY_FIELDS = frozenset({"summary"})
# History of Jira's lists whose one picked value is an issue attribute, by field id (Cloud) and the
# name Data Center writes, in lower case.
LIST_HISTORY_FIELDS = {
    "fixversions": "fixed_version_id",
    "fix version": "fixed_version_id",
    "components": "category_id",
    "component": "category_id",
}
# History of the issue's parent, by field id and the name Jira writes, in lower case: a sub-task's
# parent, an epic's child on Cloud (both ``parent`` and ``IssueParentAssociation`` for one change),
# and the Epic Link field of Data Center (on Cloud, by its field id: see ``epic_link_fields``).
PARENT_HISTORY_FIELDS = frozenset({"parent", "issueparentassociation", "parent issue", "epic link"})
# The same change as the epic sees it: carried by the child's history, so left out without being counted.
INVERSE_HISTORY_FIELDS = frozenset({"epic child"})
# The original estimate, in seconds, carried as estimated_hours.
ESTIMATE_HISTORY_FIELDS = frozenset({"timeoriginalestimate", "original estimate"})
# History Jira writes when work is logged or sub-tasks change, in lower case: it follows from the
# worklogs (carried as time entries) and the estimates, so it is left out without being counted.
DERIVED_HISTORY_FIELDS = frozenset(
    {
        "timespent",
        "time spent",
        "timeestimate",
        "remaining estimate",
        "worklogid",
        "worklogtimespent",
        "aggregatetimespent",
        "aggregatetimeestimate",
        "aggregatetimeoriginalestimate",
    }
)

# Issue fields the bundle carries.
CARRIED_FIELDS = frozenset(
    {
        "summary",
        "description",
        "issuetype",
        "status",
        "priority",
        "reporter",
        "assignee",
        "created",
        "updated",
        "resolutiondate",
        "resolution",
        "security",
        "issuerestriction",
        "labels",
        "duedate",
        "project",
        "comment",
        "attachment",
        "issuelinks",
        "parent",
        "components",
        "fixVersions",
        "watches",
        "timeoriginalestimate",
        "timeestimate",
    }
)
# Fields Jira derives from others or keeps for its own screens: nothing is lost
# by leaving them out, so they are not reported either.
DERIVED_FIELDS = frozenset(
    {
        "creator",
        "lastViewed",
        "statuscategorychangedate",
        "progress",
        "aggregateprogress",
        "workratio",
        "aggregatetimespent",
        # The sum of the worklogs, which are carried.
        "timespent",
        "aggregatetimeestimate",
        "aggregatetimeoriginalestimate",
        "timetracking",
        "thumbnail",
        "statusCategory",
        # A sub-task names its parent; the parent's list of them adds nothing.
        "subtasks",
    }
)
# Fields reported by what they hold rather than as "a field": (label, how to count).
LISTED_FIELDS = {
    "versions": "Affects versions set on issues",
}

_LOGIN_UNSAFE_RE = re.compile(r"[^a-z0-9._-]+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
MAX_FILENAME = 255

# Attachments that were not downloaded: (what the text's note says, what the manifest says), by status.
SKIPPED_ATTACHMENTS = {
    TOO_LARGE: ("not carried over, larger than the size limit", "Attachments larger than --max-attachment-size"),
    FORBIDDEN: ("not carried over, Jira refused to send it", "Attachments Jira refused to send (HTTP 403)"),
    GONE: ("not carried over, Jira no longer has it", "Attachments Jira no longer has (HTTP 404)"),
}
NOT_DOWNLOADED = ("not carried over", "Attachments that were not downloaded")

# Jira's link types, by name (lower case), as Specivo relation types: (type, swap). Jira
# reads a link "<issue> <outward> <outwardIssue>"; Specivo stores one canonical
# direction, so with *swap* the relation runs from the outward issue back.
# "A clones B" makes A the copy, and Specivo's "B copied_to A" says the same.
LINK_TYPES: dict[str, tuple[RelationType, bool]] = {
    "blocks": ("blocks", False),
    "duplicate": ("duplicates", False),
    "cloners": ("copied_to", True),
    "relates": ("relates", False),
}
# The same, by the outward name, for a site that renamed a built-in type.
LINK_OUTWARD_NAMES: dict[str, tuple[RelationType, bool]] = {
    "blocks": ("blocks", False),
    "duplicates": ("duplicates", False),
    "clones": ("copied_to", True),
    "relates to": ("relates", False),
}
PARENTS_GONE = "Parents Jira no longer has (kept in the issue's extra.jira_parent)"
PARENTS_UNKNOWN = "Epic Links to an issue whose id is not known (kept in the issue's extra.jira_parent)"
# The one activity time entries are logged under: Jira's worklogs have none.
WORKLOG_ACTIVITY = Activity(source_ref="worklog", name="Development", is_default=True)
HUNDREDTH = Decimal("0.01")
LINKS_GONE = "Issue links to issues Jira no longer has (kept in the issue's extra.jira_links)"
REMOTE_LINKS = "Remote links (web and Confluence links), as the change history shows them"
SPRINTS_BEFORE = "Sprints an issue was in before its current one (named in the issue's extra.sprints)"
SHARED_SPRINTS = "Sprints holding issues of several projects (carried as one sprint per project)"
ORPHAN_SPRINTS = "Sprints of a board Jira no longer has (carried as a sprint of each project whose issues are in it)"
BOARDS_UNREADABLE = "Projects whose boards Jira would not list (their sprints are the ones their issues name)"
LONG_LABELS = "Labels longer than a Specivo tag's 64 characters (kept in the issue's extra.labels)"
CF_USERS_LEFT_OUT = "Custom field values naming app accounts"
CF_VERSIONS_LEFT_OUT = "Custom field values naming versions that are not the project's"
CF_UNREADABLE = "Custom field values that could not be read"
CONTEXTS_UNREADABLE = "Custom fields whose contexts Jira would not list (scoped to the projects whose issues use them)"
# The longest tag name Specivo keeps.
MAX_TAG = 64


@dataclass
class Records:
    """Everything one bundle holds, in the order it is written."""

    trackers: list[Tracker] = field(default_factory=list)
    statuses: list[Status] = field(default_factory=list)
    priorities: list[Priority] = field(default_factory=list)
    activities: list[Activity] = field(default_factory=list)
    users: list[User] = field(default_factory=list)
    projects: list[Project] = field(default_factory=list)
    versions: list[Version] = field(default_factory=list)
    categories: list[Category] = field(default_factory=list)
    sprints: list[Sprint] = field(default_factory=list)
    custom_fields: list[CustomField] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    journals: list[Journal] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    watchers: list[Watcher] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    time_entries: list[TimeEntry] = field(default_factory=list)
    not_carried_over: list[tuple[str, int]] = field(default_factory=list)
    hidden_emails: int = 0
    conversion_failures: int = 0

    def all(self) -> Iterator[Any]:
        yield from self.statuses
        yield from self.trackers
        yield from self.priorities
        yield from self.activities
        yield from self.users
        yield from self.projects
        yield from self.versions
        yield from self.categories
        yield from self.sprints
        yield from self.custom_fields
        yield from self.issues
        yield from self.journals
        yield from self.relations
        yield from self.watchers
        yield from self.attachments
        yield from self.time_entries


@dataclass
class MapOptions:
    site_url: str
    deployment: Deployment
    project_keys: list[str]
    status_category_map: Mapping[str, str] = field(default_factory=dict)


def build_records(
    meta: JSON,
    issues_by_project: Mapping[str, list[JSON]],
    refs: JSON,
    options: MapOptions,
    downloads: Mapping[str, JSON] | None = None,
    watchers: Mapping[str, JSON] | None = None,
) -> Records:
    """Map the state's contents to bundle records.

    *downloads* is what became of each attachment, by id; *watchers* what Jira
    answered about each issue's watchers, by issue id.
    """
    return _Mapper(meta, issues_by_project, refs, options, downloads or {}, watchers or {}).build()


class _Mapper:
    def __init__(
        self,
        meta: JSON,
        issues_by_project: Mapping[str, list[JSON]],
        refs: JSON,
        options: MapOptions,
        downloads: Mapping[str, JSON],
        watchers: Mapping[str, JSON],
    ):
        self.meta = meta
        self.watcher_lists = watchers
        self.options = options
        self.deployment = options.deployment
        self.refs = refs
        self.downloads = downloads
        self.items: list[tuple[str, JSON]] = []
        for key in options.project_keys:
            present = [item for item in issues_by_project.get(key, []) if not item.get("missing")]
            present.sort(key=_issue_order)
            self.items += [(key, item) for item in present]
        # Issues the search listed that Jira no longer had when they were read.
        self.gone = {
            str(item.get("id") or (item.get("issue") or {}).get("id"))
            for key in options.project_keys
            for item in issues_by_project.get(key, [])
            if item.get("missing")
        }
        self.records = Records()
        self.not_carried: Counter[str] = Counter()
        self.relations: dict[str, Relation] = {}
        self.links_gone: set[str] = set()
        self.field_names = {str(entry.get("id")): str(entry.get("name")) for entry in meta.get("fields") or []}
        self.epic_link_fields = epic_link_fields(meta)
        self.sprint_field_ids = frozenset(sprint_fields(meta))
        self.cf_names = jira_fields.by_name(meta.get("fields") or [])
        self.zone = str(meta.get("time_zone") or "") or None
        self.key_to_id = {str(item["issue"].get("key")): str(item["issue"]["id"]) for _, item in self.items}

    # ------------------------------------------------------------------

    def build(self) -> Records:
        self.parents = {str(item["issue"]["id"]): self._parent(item["issue"]) for _, item in self.items}
        self._users()
        self._text_context()
        self._lookups()
        self._projects()
        self._project_lists()
        self._sprint_setup()
        self._field_setup()
        for project_key, item in self.items:
            self._issue(project_key, item)
        self._sprint_records()
        self._field_records()
        self.records.relations = [self.relations[ref] for ref in sorted(self.relations, key=_id_order)]
        if self.records.time_entries:
            self.records.activities.append(WORKLOG_ACTIVITY)
        self.not_carried.update(self.text.notes)
        self.not_carried.update(self.plain.notes)
        self.records.not_carried_over = sorted(self.not_carried.items())
        self.records.conversion_failures = self.text.failures
        return self.records

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def _users(self) -> None:
        found: dict[str, JSON] = {}
        for _, item in self.items:
            for user in user_objects(item) + self._listed_watchers(str(item["issue"]["id"])):
                key = user_key(user, self.deployment)
                if key and key not in found:
                    found[key] = user
        for _name, user in sorted((self.refs.get("users") or {}).items()):
            key = user_key(user, self.deployment)
            if user and key and key not in found:
                found[key] = user

        # People only: app accounts (automation rules, integrations) are left out,
        # and what they wrote falls back to the import account.
        self.display_names: dict[str, str] = {}
        self.carried_users: set[str] = set()
        self.name_to_key: dict[str, str] = {}
        apps = 0
        logins: set[str] = set()
        for key in sorted(found):
            user = found[key]
            display = str(user.get("displayName") or user.get("name") or key)
            self.display_names[key] = display
            if user.get("name"):
                self.name_to_key[str(user["name"])] = key
                self.display_names[str(user["name"])] = display
            if user.get("accountType") == "app":
                apps += 1
                continue
            email = str(user.get("emailAddress") or "").strip() or None
            if email is None:
                self.records.hidden_emails += 1
            login = _unique(self._login(user, email, key), logins)
            self.carried_users.add(key)
            extra: JSON = {"jira_account_type": user["accountType"]} if user.get("accountType") else {}
            if self.deployment is Deployment.DATA_CENTER and user.get("name") and user.get("name") != login:
                extra["jira_username"] = user["name"]
            self.records.users.append(
                User(
                    source_ref=key,
                    login=login,
                    display_name=display,
                    email=email,
                    status="active" if user.get("active", True) else "locked",
                    language=_language(user.get("locale")),
                    extra=extra or None,
                )
            )
        if apps:
            self.not_carried["App accounts (their comments and changes are attributed to the import account)"] = apps

    def _login(self, user: JSON, email: str | None, key: str) -> str:
        if self.deployment is Deployment.DATA_CENTER and user.get("name"):
            candidate = _login_text(str(user["name"]))
        elif email:
            candidate = _login_text(email.split("@", 1)[0])
        else:
            candidate = _login_text(str(user.get("displayName") or ""))
        return candidate or "jira-" + _login_text(key)[-12:]

    def user_ref(self, user: JSON | None) -> str | None:
        key = user_key(user, self.deployment)
        return key if key in self.carried_users else None

    # ------------------------------------------------------------------
    # Text
    # ------------------------------------------------------------------

    def _text_context(self) -> None:
        user_refs = {key: key for key in self.carried_users}
        if self.deployment is Deployment.DATA_CENTER:
            # Data Center mentions name the user name; the bundle refers to the user key.
            user_refs.update({name: key for name, key in self.name_to_key.items() if key in self.carried_users})
        issue_refs: dict[str, str] = {}
        for key, found in sorted((self.refs.get("keys") or {}).items()):
            if found:
                issue_refs[key] = str(found["id"])
        in_bundle = set()
        for _, item in self.items:
            issue = item["issue"]
            issue_refs[str(issue["key"])] = str(issue["id"])
            in_bundle.add(str(issue["id"]))
        self.text = TextContext(
            site_url=self.options.site_url,
            user_refs=user_refs,
            display_names=self.display_names,
            issue_refs=issue_refs,
            in_bundle=frozenset(in_bundle),
        )
        # Custom field values are stored as they are, with no placeholders: mentions read @Name, keys stay text.
        self.plain = TextContext(site_url=self.options.site_url, display_names=self.display_names)

    def markdown(self, body: object) -> str | None:
        """A description or comment body as Markdown: ADF on Cloud, wiki markup on Data Center."""
        if self.deployment is Deployment.CLOUD:
            return adf.to_markdown(body, self.text)
        return convert.to_markdown(body if isinstance(body, str) else None, self.text)

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def _lookups(self) -> None:
        statuses: dict[str, JSON] = {}
        types: dict[str, JSON] = {}
        priorities: dict[str, JSON] = {}
        history_refs: dict[str, set[str]] = {"status": set(), "issuetype": set(), "priority": set()}
        needs_no_priority = False
        initial: dict[str, Counter[str]] = {}
        for _, item in self.items:
            fields = item["issue"].get("fields", {})
            for target, value in ((statuses, fields.get("status")), (types, fields.get("issuetype"))):
                if isinstance(value, dict) and value.get("id"):
                    target.setdefault(str(value["id"]), value)
            priority = fields.get("priority")
            if isinstance(priority, dict) and priority.get("id"):
                priorities.setdefault(str(priority["id"]), priority)
            else:
                needs_no_priority = True
            for history in item["histories"]:
                for change in history.get("items") or []:
                    name = _history_field(change)
                    if name in history_refs:
                        history_refs[name] |= {str(v) for v in (change.get("from"), change.get("to")) if v}
            type_id = str((fields.get("issuetype") or {}).get("id") or "")
            first = _initial_status(item)
            if type_id and first:
                initial.setdefault(type_id, Counter())[first] += 1

        # Values only the history names come from the site-wide lists, when they are there.
        for target, listed, name in (
            (statuses, self.meta.get("statuses") or [], "status"),
            (types, self.meta.get("issue_types") or [], "issuetype"),
            (priorities, self.meta.get("priorities") or [], "priority"),
        ):
            by_id = {str(entry.get("id")): entry for entry in listed if isinstance(entry, dict)}
            for ref in sorted(history_refs[name] - set(target)):
                if ref in by_id:
                    target[ref] = by_id[ref]

        status_overrides = {name.casefold(): category for name, category in self.options.status_category_map.items()}
        for position, ref in enumerate(sorted(statuses, key=_id_order)):
            status = statuses[ref]
            name = str(status.get("name") or ref)
            category_key = str((status.get("statusCategory") or {}).get("key") or "undefined")
            category = status_overrides.get(name.casefold()) or STATUS_CATEGORIES.get(category_key, "backlog")
            self.records.statuses.append(
                Status(
                    source_ref=ref,
                    name=name,
                    category=category,  # type: ignore[arg-type]
                    position=position,
                    extra={"jira_status_category": category_key},
                )
            )
        known_statuses = set(statuses)

        for position, ref in enumerate(sorted(types, key=_id_order)):
            issue_type = types[ref]
            default = None
            counts = initial.get(ref)
            if counts:
                # The status most of this type's issues started in, ties to the lowest id.
                ranked = sorted(counts.items(), key=lambda pair: (-pair[1], _id_order(pair[0])))
                default = next((status for status, _ in ranked if status in known_statuses), None)
            extra: JSON = {"subtask": bool(issue_type.get("subtask"))}
            if issue_type.get("hierarchyLevel") is not None:
                extra["hierarchy_level"] = issue_type["hierarchyLevel"]
            scope = issue_type.get("scope") or {}
            if scope.get("type") == "PROJECT":
                # A team-managed project's own type: another project's "Task" is another type.
                extra["project_id"] = str((scope.get("project") or {}).get("id") or "")
            self.records.trackers.append(
                Tracker(
                    source_ref=ref,
                    name=str(issue_type.get("name") or ref),
                    description=str(issue_type.get("description") or "").strip() or None,
                    default_status_ref=default,
                    position=position,
                    extra=extra,
                )
            )

        order = {str(entry.get("id")): index for index, entry in enumerate(self.meta.get("priorities") or [])}
        ranked_priorities = sorted(priorities, key=lambda ref: (order.get(ref, len(order)), _id_order(ref)))
        for position, ref in enumerate(ranked_priorities):
            priority = priorities[ref]
            listed = next((p for p in self.meta.get("priorities") or [] if str(p.get("id")) == ref), priority)
            self.records.priorities.append(
                Priority(
                    source_ref=ref,
                    name=str(priority.get("name") or ref),
                    position=position,
                    is_default=bool(listed.get("isDefault")),
                )
            )
        if needs_no_priority:
            self.records.priorities.append(
                Priority(source_ref=NO_PRIORITY_REF, name="No priority", position=len(ranked_priorities))
            )

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def _projects(self) -> None:
        projects = self.meta.get("projects") or {}
        self.project_refs: dict[str, str] = {}
        for key in self._project_order():
            project = projects[key]
            ref = str(project["id"])
            self.project_refs[key] = ref
            extra: JSON = {"jira_key": key}
            if project.get("projectTypeKey"):
                extra["project_type"] = project["projectTypeKey"]
            if project.get("style"):
                extra["style"] = project["style"]
            elif project.get("simplified") is not None:
                extra["style"] = "next-gen" if project["simplified"] else "classic"
            logged = any(item.get("worklogs") for project_key, item in self.items if project_key == key)
            self.records.projects.append(
                Project(
                    source_ref=ref,
                    identifier=key.lower(),
                    name=str(project.get("name") or key),
                    description=str(project.get("description") or "").strip() or None,
                    modules=["issue_tracking", "time_tracking"] if logged else ["issue_tracking"],
                    extra=extra,
                )
            )

    def _project_lists(self) -> None:
        """Each project's components as categories and its versions as versions, in Jira's order."""
        lists = self.meta.get("project_lists") or {}
        # Per project: category refs, and version ref -> (released, release date, position).
        self.categories: dict[str, set[str]] = {}
        self.category_positions: dict[str, dict[str, int]] = {}
        self.versions: dict[str, dict[str, tuple[bool, str, int]]] = {}
        for key in self.project_refs:
            project_ref = self.project_refs[key]
            entry = lists.get(key) or {}
            self.categories[key] = set()
            self.category_positions[key] = {}
            for component in entry.get("components") or []:
                if not component.get("id") or not str(component.get("name") or "").strip():
                    continue
                ref = str(component["id"])
                self.categories[key].add(ref)
                self.category_positions[key].setdefault(ref, len(self.category_positions[key]))
                extra: JSON = {}
                if str(component.get("description") or "").strip():
                    extra["description"] = str(component["description"]).strip()
                if component.get("assigneeType"):
                    extra["assignee_type"] = component["assigneeType"]
                lead = component.get("lead") if component.get("assigneeType") == "COMPONENT_LEAD" else None
                self.records.categories.append(
                    Category(
                        source_ref=ref,
                        project_ref=project_ref,
                        name=str(component["name"]).strip(),
                        assigned_to_ref=self.user_ref(lead),
                        extra=extra or None,
                    )
                )
            self.versions[key] = {}
            for position, version in enumerate(entry.get("versions") or []):
                if not version.get("id") or not str(version.get("name") or "").strip():
                    continue
                ref = str(version["id"])
                released, archived = bool(version.get("released")), bool(version.get("archived"))
                status: VersionStatus = "closed" if released else "locked" if archived else "open"
                release_date = _date(version.get("releaseDate"))
                self.versions[key][ref] = (released, release_date or "", position)
                version_extra: JSON = {"released": released, "archived": archived}
                if _date(version.get("startDate")):
                    version_extra["start_date"] = _date(version.get("startDate"))
                self.records.versions.append(
                    Version(
                        source_ref=ref,
                        project_ref=project_ref,
                        name=str(version["name"]).strip(),
                        description=str(version.get("description") or "").strip() or None,
                        status=status,
                        effective_date=release_date,
                        extra=version_extra,
                    )
                )

    def _category(self, project_key: str, fields: JSON, extra: JSON) -> str | None:
        """The issue's first component, as its category; the others are counted and named in *extra*."""
        components = [c for c in fields.get("components") or [] if isinstance(c, dict) and c.get("id")]
        if not components:
            return None
        if len(components) > 1:
            self.not_carried["Issues with more than one component (the first is the category)"] += 1
            extra["components"] = [str(c.get("name") or c["id"]) for c in components]
        chosen = self._first_category(project_key, [str(c["id"]) for c in components])
        if chosen is None:
            self.not_carried["Components that are not among the project's components"] += 1
        return chosen

    def _first_category(self, project_key: str, refs: list[str]) -> str | None:
        """The category of an issue whose components are *refs*: the first, when it is the project's."""
        if refs and refs[0] in self.categories.get(project_key, set()):
            return refs[0]
        return None

    def _fixed_version(self, project_key: str, fields: JSON, extra: JSON) -> str | None:
        """The fix version released last, or else the last listed; the others are counted and named in *extra*."""
        listed = [v for v in fields.get("fixVersions") or [] if isinstance(v, dict) and v.get("id")]
        known = self.versions.get(project_key, {})
        candidates = [str(v["id"]) for v in listed if str(v["id"]) in known]
        if len(candidates) < len(listed):
            self.not_carried["Fix versions that are not among the project's versions"] += len(listed) - len(candidates)
        if len(listed) > 1:
            extra["fix_versions"] = [str(v.get("name") or v["id"]) for v in listed]
        if len(candidates) > 1:
            self.not_carried["Fix versions beyond the one carried (Specivo has one per issue)"] += len(candidates) - 1
        return self._target_version(project_key, candidates)

    def _target_version(self, project_key: str, refs: list[str]) -> str | None:
        """The target version of an issue whose fix versions are *refs*: the one released last, or else the
        last listed, of those that are the project's."""
        known = self.versions.get(project_key, {})
        candidates = [ref for ref in refs if ref in known]
        if not candidates:
            return None
        released = [ref for ref in candidates if known[ref][0]]
        if released:
            return max(released, key=lambda ref: (known[ref][1], known[ref][2]))
        return candidates[-1]

    def _list_history(self, project_key: str, item: JSON) -> dict[tuple[str, str], tuple[str | None, str | None]]:
        """The target version and category before and after each history entry that changes them, by
        ``(history id, attribute)``.

        Jira's history names the values added to and removed from a list; walking it back from the issue's
        list now gives the list before each entry, and the same rule as for the issue picks its one value.
        """
        fields: JSON = item["issue"].get("fields", {})
        rules = (
            (
                "fixed_version_id",
                _list_refs(fields.get("fixVersions")),
                self._target_version,
                {ref: position for ref, (_, _, position) in self.versions.get(project_key, {}).items()},
            ),
            (
                "category_id",
                _list_refs(fields.get("components")),
                self._first_category,
                self.category_positions.get(project_key, {}),
            ),
        )
        newest_first = sorted(
            item["histories"],
            key=lambda history: (_moment(_time(history.get("created"))), _id_order(str(history.get("id")))),
            reverse=True,
        )
        found: dict[tuple[str, str], tuple[str | None, str | None]] = {}
        for attribute, now, pick, positions in rules:
            ranks = {ref: rank for rank, ref in enumerate(now)}
            after = now
            for history in newest_first:
                changes = [
                    change
                    for change in history.get("items") or []
                    if LIST_HISTORY_FIELDS.get(_history_field(change).casefold()) == attribute
                ]
                if not changes:
                    continue
                values = set(after)
                for change in reversed(changes):
                    if change.get("to"):
                        values.discard(str(change["to"]))
                    if change.get("from"):
                        values.add(str(change["from"]))
                before = _ordered(values, ranks, positions)
                old, new = pick(project_key, before), pick(project_key, after)
                if old != new:
                    found[(str(history["id"]), attribute)] = (old, new)
                after = before
        return found

    # ------------------------------------------------------------------
    # Issues and journals
    # ------------------------------------------------------------------

    def _issue(self, project_key: str, item: JSON) -> None:
        issue = item["issue"]
        fields: JSON = issue.get("fields", {})
        ref = str(issue["id"])
        extra: JSON = {"jira_key": issue["key"]}
        project_ref = self.project_refs[project_key]
        tags = self._tags(fields, extra)
        sprint_ref = self._issue_sprint(project_ref, item, extra)
        custom_values = self._custom_values(project_key, fields)
        resolution = fields.get("resolution")
        if isinstance(resolution, dict) and resolution.get("name"):
            extra["resolution"] = resolution["name"]
        restriction = _restriction(fields)
        if restriction:
            extra["restriction"] = restriction
        priority = fields.get("priority")
        gone = self._links(ref, fields)
        if gone:
            extra["jira_links"] = gone
        category_ref = self._category(project_key, fields, extra)
        fixed_version_ref = self._fixed_version(project_key, fields, extra)
        affects = [v for v in fields.get("versions") or [] if isinstance(v, dict)]
        if affects:
            extra["affects_versions"] = [str(v.get("name") or v.get("id")) for v in affects]
        estimated_hours = _estimate(fields, "timeoriginalestimate", extra, "jira_original_estimate_seconds")
        remaining_estimate = _estimate(fields, "timeestimate", extra, "jira_remaining_estimate_seconds")
        parent_ref, parent_kept, why = self.parents[ref]
        if parent_kept is not None and why is not None:
            extra["jira_parent"] = parent_kept
            self.not_carried[why] += 1
        self._attachments(ref, item)
        self.records.issues.append(
            Issue(
                source_ref=ref,
                project_ref=self.project_refs[project_key],
                tracker_ref=str(fields["issuetype"]["id"]),
                status_ref=str(fields["status"]["id"]),
                priority_ref=str(priority["id"]) if isinstance(priority, dict) else NO_PRIORITY_REF,
                subject=str(fields.get("summary") or ""),
                author_ref=self.user_ref(fields.get("reporter")) or self.user_ref(fields.get("creator")),
                assigned_to_ref=self.user_ref(fields.get("assignee")),
                description=self.markdown(fields.get("description")),
                parent_ref=parent_ref,
                category_ref=category_ref,
                fixed_version_ref=fixed_version_ref,
                sprint_ref=sprint_ref,
                estimated_hours=estimated_hours,
                remaining_estimate=remaining_estimate,
                due_date=date_value(fields["duedate"]) if fields.get("duedate") else None,
                created_at=_time(fields.get("created")),
                updated_at=_time(fields.get("updated")),
                closed_at=_time(fields.get("resolutiondate")),
                is_private=bool(restriction),
                custom_values=custom_values,
                tags=tags,
                extra=extra,
            )
        )
        self._count_left_out(fields)

        journals: list[tuple[datetime, str, Journal]] = []
        for comment in item["comments"]:
            notes = self.markdown(comment.get("body"))
            if notes is None:
                self.not_carried["Comments with no text"] += 1
                continue
            comment_extra: JSON = {}
            visibility = comment.get("visibility")
            if isinstance(visibility, dict):
                comment_extra["visibility"] = {"type": visibility.get("type"), "value": visibility.get("value")}
            if comment.get("updated") and comment.get("updated") != comment.get("created"):
                comment_extra["updated_at"] = _time(comment["updated"])
            created = _time(comment.get("created"))
            journals.append(
                (
                    _moment(created),
                    "comment",
                    Journal(
                        source_ref=source_ref("comment", str(comment["id"])),
                        issue_ref=ref,
                        user_ref=self.user_ref(comment.get("author")),
                        notes=notes,
                        is_private=isinstance(visibility, dict),
                        created_at=created,
                        extra=comment_extra or None,
                    ),
                )
            )
        derived = self._list_history(project_key, item)
        names = _list_names(item)
        for history in item["histories"]:
            details = []
            written: set[str] = set()
            for change in history.get("items") or []:
                name = _history_field(change)
                folded = name.casefold()
                if folded == "attachment":
                    detail = _attachment_detail(change)
                    if detail is None:
                        self.not_carried["Change history of attachments that names no attachment"] += 1
                    else:
                        details.append(detail)
                    continue
                if folded in DERIVED_HISTORY_FIELDS or folded in INVERSE_HISTORY_FIELDS:
                    continue
                if folded in PARENT_HISTORY_FIELDS or name in self.epic_link_fields:
                    # One detail per entry, though Cloud writes one change under two fields.
                    detail = self._parent_detail(change)
                    if detail is not None and "parent_id" not in written:
                        written.add("parent_id")
                        details.append(detail)
                    continue
                field_id = jira_fields.history_field_id(change, self.cf_names)
                if field_id is not None and field_id in self.sprint_field_ids:
                    sprint_detail = self._sprint_detail(change, project_ref)
                    if sprint_detail is not None and "sprint_id" not in written:
                        written.add("sprint_id")
                        details.append(sprint_detail)
                    continue
                if field_id is not None and field_id in self.cf_kinds:
                    self.cf_projects.setdefault(field_id, set()).add(project_ref)
                    details += self._cf_details(field_id, change)
                    continue
                attribute = LIST_HISTORY_FIELDS.get(folded)
                if attribute is not None:
                    # One detail per entry, where the list's first change is, when the picked value changed.
                    picked = derived.get((str(history["id"]), attribute))
                    if picked is not None and attribute not in written:
                        written.add(attribute)
                        old_ref, new_ref = picked
                        details.append(
                            JournalDetail(
                                property="attr",
                                prop_key=attribute,
                                old_ref=old_ref,
                                new_ref=new_ref,
                                old_value=names.get((attribute, old_ref)) if old_ref else None,
                                new_value=names.get((attribute, new_ref)) if new_ref else None,
                            )
                        )
                    continue
                if folded in ESTIMATE_HISTORY_FIELDS:
                    old_hours = _hours(change.get("from") or change.get("fromString"))
                    new_hours = _hours(change.get("to") or change.get("toString"))
                    if old_hours != new_hours:
                        details.append(
                            JournalDetail(
                                property="attr",
                                prop_key="estimated_hours",
                                old_value=None if old_hours is None else str(old_hours),
                                new_value=None if new_hours is None else str(new_hours),
                            )
                        )
                    continue
                prop_key = HISTORY_FIELDS.get(name)
                if prop_key is None:
                    self.not_carried[f"Change history of {change.get('field') or name}"] += 1
                    continue
                old_text, new_text = _text(change.get("fromString")), _text(change.get("toString"))
                if name in TEXT_HISTORY_FIELDS:
                    details.append(
                        JournalDetail(property="attr", prop_key=prop_key, old_value=old_text, new_value=new_text)
                    )
                    continue
                # The others name a record: by ref, with Jira's name for it in case it did not come across.
                old_ref, new_ref = _text(change.get("from")), _text(change.get("to"))
                if name == "assignee":
                    old_ref, new_ref = self._assignee_value(old_ref), self._assignee_value(new_ref)
                details.append(
                    JournalDetail(
                        property="attr",
                        prop_key=prop_key,
                        old_ref=old_ref,
                        new_ref=new_ref,
                        old_value=old_text if old_ref else None,
                        new_value=new_text if new_ref else None,
                    )
                )
            if not details:
                continue
            created = _time(history.get("created"))
            journals.append(
                (
                    _moment(created),
                    "history",
                    Journal(
                        source_ref=source_ref("history", str(history["id"])),
                        issue_ref=ref,
                        user_ref=self.user_ref(history.get("author")),
                        created_at=created,
                        details=details,
                    ),
                )
            )
        remote = sum(
            (1 if change.get("to") else 0) - (1 if change.get("from") else 0)
            for history in item["histories"]
            for change in history.get("items") or []
            if str(change.get("field") or "") == "RemoteIssueLink"
        )
        if remote > 0:
            self.not_carried[REMOTE_LINKS] += remote
        if item.get("missing_histories"):
            self.not_carried["Change history entries Jira did not return"] += int(item["missing_histories"])
        journals.sort(key=lambda entry: (entry[0], entry[1], _id_order(entry[2].source_ref.split(":", 1)[1])))
        self.records.journals += [journal for _, _, journal in journals]
        self._time_entries(project_key, ref, item)
        self._watchers(ref, item)
        self.text.media, self.text.files = {}, {}

    # ------------------------------------------------------------------
    # Labels, sprints and custom fields
    # ------------------------------------------------------------------

    def _tags(self, fields: JSON, extra: JSON) -> list[str]:
        """The issue's labels, as tags; one longer than a tag can be is kept in *extra* and counted."""
        labels = [str(label) for label in fields.get("labels") or [] if str(label).strip()]
        tags = [label for label in labels if len(label) <= MAX_TAG]
        kept = [label for label in labels if len(label) > MAX_TAG]
        if kept:
            extra["labels"] = kept
            self.not_carried[LONG_LABELS] += len(kept)
        return tags

    def _issue_sprint_list(self, item: JSON) -> list[sprints.SprintInfo]:
        """The sprints an issue's Sprint field names, each once, in Jira's order."""
        values = item["issue"].get("fields", {})
        found: dict[str, sprints.SprintInfo] = {}
        for field_id in sorted(self.sprint_field_ids):
            for sprint in sprints.issue_sprints(values.get(field_id)):
                found.setdefault(sprint.id, sprint)
        return list(found.values())

    def _sprint_setup(self) -> None:
        """What is known of every sprint, and the project each board is located in."""
        agile: JSON = self.meta.get("agile") or {}
        self.board_projects: dict[str, str] = {}
        for board_id, board in (agile.get("boards") or {}).items():
            location = board.get("location") if isinstance(board, dict) else None
            if isinstance(location, dict) and location.get("projectId"):
                self.board_projects[str(board_id)] = str(location["projectId"])
        self.sprint_infos: dict[str, sprints.SprintInfo] = {}
        bundle_projects = set(self.project_refs.values())
        self.home_sprints: dict[str, set[str]] = {}
        for raw in (agile.get("sprints") or {}).values():
            info = sprints.parse_sprint(raw)
            if info is None:
                continue
            self.sprint_infos[info.id] = info
            home = self.board_projects.get(info.board_id or "")
            if home in bundle_projects:
                self.home_sprints.setdefault(home, set()).add(info.id)
        for _, item in self.items:
            for named in self._issue_sprint_list(item):
                known = self.sprint_infos.get(named.id)
                if known is None:
                    self.sprint_infos[named.id] = named
                else:
                    known.merge(named)
        self.used_sprints: dict[str, set[str]] = {}
        unreadable = [
            key
            for key, entry in (agile.get("projects") or {}).items()
            if key in self.project_refs and isinstance(entry, dict) and entry.get("status") == UNAVAILABLE
        ]
        if unreadable:
            self.not_carried[BOARDS_UNREADABLE] += len(unreadable)

    def _sprint_ref(self, sprint_id: str, project_ref: str) -> str:
        """A sprint's ref in a project: Jira's id in the project its board is located in, else ``id@project``."""
        info = self.sprint_infos.get(sprint_id)
        home = self.board_projects.get(info.board_id or "") if info is not None else None
        return sprint_id if home == project_ref else f"{sprint_id}@{project_ref}"

    def _sprint_name(self, sprint_id: str | None, names: Mapping[str, str]) -> str | None:
        if sprint_id is None:
            return None
        info = self.sprint_infos.get(sprint_id)
        return info.name if info is not None else names.get(sprint_id, sprint_id)

    def _issue_sprint(self, project_ref: str, item: JSON, extra: JSON) -> str | None:
        """The sprint the issue is in now; the others it was in are named in *extra* and counted."""
        listed = self._issue_sprint_list(item)
        if len(listed) > 1:
            extra["sprints"] = [sprint.name for sprint in listed]
            self.not_carried[SPRINTS_BEFORE] += len(listed) - 1
        picked = sprints.pick([sprint.id for sprint in listed], self.sprint_infos)
        if picked is None:
            return None
        self.used_sprints.setdefault(project_ref, set()).add(picked)
        return self._sprint_ref(picked, project_ref)

    def _sprint_detail(self, change: JSON, project_ref: str) -> JournalDetail | None:
        """A change of the Sprint field, as a change of the issue's one sprint when the rule picks another."""
        names: dict[str, str] = {}
        for side in ("from", "to"):
            ids = jira_fields.history_list(change.get(side))
            texts = jira_fields.history_list(change.get(f"{side}String"))
            if len(ids) == len(texts):
                names.update(zip(ids, texts, strict=True))
        old = sprints.pick(jira_fields.history_list(change.get("from")), self.sprint_infos)
        new = sprints.pick(jira_fields.history_list(change.get("to")), self.sprint_infos)
        if old == new:
            return None
        return JournalDetail(
            property="attr",
            prop_key="sprint_id",
            old_ref=self._sprint_ref(old, project_ref) if old else None,
            new_ref=self._sprint_ref(new, project_ref) if new else None,
            old_value=self._sprint_name(old, names),
            new_value=self._sprint_name(new, names),
        )

    def _sprint_records(self) -> None:
        """Each project's sprints: those of the boards located in it, and those its issues are in."""
        shared = orphaned = 0
        for project_ref in self.project_refs.values():
            wanted = self.home_sprints.get(project_ref, set()) | self.used_sprints.get(project_ref, set())
            # The project's own sprints first: Specivo keeps the first active sprint of a project active.
            own_first = sorted(wanted, key=lambda ref: (self._sprint_ref(ref, project_ref) != ref, _id_order(ref)))
            for sprint_id in own_first:
                info = self.sprint_infos[sprint_id]
                ref = self._sprint_ref(sprint_id, project_ref)
                extra: JSON = {"jira_sprint_id": sprint_id}
                if info.board_id:
                    extra["jira_board_id"] = info.board_id
                if ref != sprint_id and self.board_projects.get(info.board_id or ""):
                    shared += 1
                    extra["shared_with_other_projects"] = True
                elif ref != sprint_id:
                    orphaned += 1
                self.records.sprints.append(
                    Sprint(
                        source_ref=ref,
                        project_ref=project_ref,
                        name=info.name[:255],
                        goal=info.goal,
                        status=sprints.STATES[info.state],  # type: ignore[arg-type]
                        start_date=sprints.sprint_date(info.start, self.zone),
                        end_date=sprints.sprint_date(info.end, self.zone),
                        completed_at=_time(info.completed) if info.state == "closed" else None,
                        created_at=_time(info.created),
                        extra=extra,
                    )
                )
        if shared:
            self.not_carried[SHARED_SPRINTS] += shared
        if orphaned:
            self.not_carried[ORPHAN_SPRINTS] += orphaned

    def _field_setup(self) -> None:
        """The custom fields the bundle carries: those the issues use, of a type the bundle can hold."""
        listed = [entry for entry in self.meta.get("fields") or [] if entry.get("custom") and entry.get("id")]
        self.field_entries = {str(entry["id"]): entry for entry in listed}
        self.field_keys = jira_fields.field_keys(listed)
        self.cf_kinds: dict[str, jira_fields.FieldKind] = {}
        for field_id in sorted(jira_fields.fields_in_use([item for _, item in self.items], listed)):
            entry = self.field_entries.get(field_id)
            kind = jira_fields.classify(entry) if entry is not None else None
            if isinstance(kind, jira_fields.FieldKind):
                self.cf_kinds[field_id] = kind
        # Per field: the projects whose issues hold a value or a change of it, and the option values seen.
        self.cf_projects: dict[str, set[str]] = {}
        self.cf_observed: dict[str, list[str]] = {}

    def _custom_values(self, project_key: str, fields: JSON) -> list[CustomValue]:
        values: list[CustomValue] = []
        for field_id, kind in self.cf_kinds.items():
            raw = fields.get(field_id)
            if jira_fields.empty(raw):
                continue
            self.cf_projects.setdefault(field_id, set()).add(self.project_refs[project_key])
            value = self._cf_value(project_key, field_id, kind, raw)
            if value is None:
                continue
            values.append(
                CustomValue(field_ref=field_id, key=self.field_keys[field_id], value=value, value_kind=kind.value_kind)
            )
        return values

    def _cf_value(self, project_key: str, field_id: str, kind: jira_fields.FieldKind, raw: object) -> Any:
        """One field's value on an issue, as the bundle holds it; None when nothing of it can be carried."""
        entries = raw if isinstance(raw, list) else [raw]
        found: list[Any] = []
        if kind.value_kind == "user_ref":
            for entry in entries:
                ref = self.user_ref(entry) if isinstance(entry, dict) and is_user(entry) else None
                if ref is None:
                    self.not_carried[CF_USERS_LEFT_OUT] += 1
                elif ref not in found:
                    found.append(ref)
        elif kind.value_kind == "version_ref":
            known = self.versions.get(project_key, {})
            for entry in entries:
                ref = str(entry.get("id") or "") if isinstance(entry, dict) else ""
                if ref not in known:
                    self.not_carried[CF_VERSIONS_LEFT_OUT] += 1
                elif ref not in found:
                    found.append(ref)
        else:
            for entry in entries:
                value = self._cf_scalar(kind, entry)
                if value is None:
                    self.not_carried[CF_UNREADABLE] += 1
                    continue
                found.append(value)
            if kind.field_format == "list":
                self.cf_observed.setdefault(field_id, []).extend(found)
        if kind.multiple:
            return found or None
        return found[0] if found else None

    def _cf_scalar(self, kind: jira_fields.FieldKind, value: object) -> Any:
        if kind.field_format == "float":
            if isinstance(value, int | float) and not isinstance(value, bool):
                return value
            try:
                return float(str(value))
            except ValueError:
                return None
        if kind.field_format == "date":
            return _date(value)
        if kind.field_format == "datetime":
            try:
                return _time(value)
            except ValueError:
                return None
        if kind.field_format == "list":
            return jira_fields.option_text(value, cascading=kind.cascading)
        if kind.field_format == "text":
            if self.deployment is Deployment.CLOUD and isinstance(value, dict):
                return adf.to_markdown(value, self.plain)
            if self.deployment is Deployment.DATA_CENTER and isinstance(value, str):
                return convert.to_markdown(value, self.plain)
        if isinstance(value, dict):
            # A group (its name) or a project (its key).
            text = value.get("key") if kind.custom.endswith(":project") else value.get("name") or value.get("value")
            return str(text).strip() or None if text else None
        if isinstance(value, str | int | float) and not isinstance(value, bool):
            return str(value).strip() or None
        return None

    def _cf_history_refs(self, kind: jira_fields.FieldKind, value: object) -> list[str]:
        refs = jira_fields.history_list(value)
        if kind.value_kind == "user_ref" and self.deployment is Deployment.DATA_CENTER:
            # Data Center's history names a user by user name; the bundle refers to the user key.
            refs = [self.name_to_key.get(ref, ref) for ref in refs]
        return refs

    def _cf_details(self, field_id: str, change: JSON) -> list[JournalDetail]:
        """A custom field change: its text, or for a user or version field its refs, a detail per value of a
        field of several."""
        kind = self.cf_kinds[field_id]
        if kind.value_kind == "scalar":
            old_text, new_text = _text(change.get("fromString")), _text(change.get("toString"))
            if old_text == new_text:
                return []
            if kind.field_format == "list" and not kind.multiple:
                # Options the issue no longer has are options all the same.
                self.cf_observed.setdefault(field_id, []).extend(t for t in (old_text, new_text) if t)
            return [JournalDetail(property="cf", prop_key=field_id, old_value=old_text, new_value=new_text)]
        old = self._cf_history_refs(kind, change.get("from"))
        new = self._cf_history_refs(kind, change.get("to"))
        names: dict[str, str] = {}
        for refs, side in ((old, "from"), (new, "to")):
            texts = jira_fields.history_list(change.get(f"{side}String")) if kind.multiple else []
            if not kind.multiple:
                texts = [text] if (text := _text(change.get(f"{side}String"))) else []
            if len(refs) == len(texts):
                names.update(zip(refs, texts, strict=True))
        if not kind.multiple:
            old_ref, new_ref = (old[0] if old else None), (new[0] if new else None)
            if old_ref == new_ref:
                return []
            return [
                JournalDetail(
                    property="cf",
                    prop_key=field_id,
                    old_ref=old_ref,
                    new_ref=new_ref,
                    old_value=names.get(old_ref) if old_ref else None,
                    new_value=names.get(new_ref) if new_ref else None,
                )
            ]
        removed = [
            JournalDetail(property="cf", prop_key=field_id, old_ref=ref, old_value=names.get(ref))
            for ref in old
            if ref not in new
        ]
        added = [
            JournalDetail(property="cf", prop_key=field_id, new_ref=ref, new_value=names.get(ref))
            for ref in new
            if ref not in old
        ]
        return removed + added

    def _field_records(self) -> None:
        """The definitions of the custom fields the bundle's issues use, scoped as their contexts say."""
        bundle_projects = set(self.project_refs.values())
        trackers = {tracker.source_ref for tracker in self.records.trackers}
        contexts_read: JSON = self.meta.get("field_contexts") or {}
        for field_id in sorted(self.cf_kinds, key=_field_order):
            if field_id not in self.cf_projects:
                continue
            entry, kind = self.field_entries[field_id], self.cf_kinds[field_id]
            extra: JSON = {"jira_field_id": field_id}
            if kind.custom:
                extra["jira_type"] = kind.custom
            projects: set[str] = set()
            types: set[str] = set()
            options: list[str] = []
            for_all = any_type = False
            scope = entry.get("scope") or {}
            listed = contexts_read.get(field_id)
            if scope.get("type") == "PROJECT":
                # A team-managed project's own field.
                projects.add(str((scope.get("project") or {}).get("id") or ""))
                any_type = True
                extra["team_managed"] = True
            elif isinstance(listed, dict) and listed.get("status") == LISTED:
                for context in listed.get("contexts") or []:
                    for_all = for_all or bool(context.get("global"))
                    any_type = any_type or bool(context.get("any_issue_type"))
                    projects |= set(context.get("projects") or [])
                    types |= set(context.get("issue_types") or [])
                    options += jira_fields.context_options(context.get("options") or [], cascading=kind.cascading)
            else:
                any_type = True
                if isinstance(listed, dict) and listed.get("status") == UNAVAILABLE:
                    self.not_carried[CONTEXTS_UNREADABLE] += 1
            # Projects whose issues hold a value always get the field, whatever its contexts say now.
            project_refs = (
                [] if for_all else sorted((projects & bundle_projects) | self.cf_projects[field_id], key=_id_order)
            )
            self.records.custom_fields.append(
                CustomField(
                    source_ref=field_id,
                    name=str(entry.get("name") or field_id).strip() or field_id,
                    key=self.field_keys[field_id],
                    field_format=kind.field_format,
                    json_schema=kind.json_schema(options + self.cf_observed.get(field_id, [])),
                    multiple=kind.multiple,
                    is_for_all=for_all,
                    tracker_refs=[] if any_type else sorted(types & trackers, key=_id_order),
                    project_refs=project_refs,
                    extra=extra,
                )
            )

    def _attachments(self, issue_ref: str, item: JSON) -> None:
        """Write the issue's attachment records, and tell the text converter what the issue's text may show."""
        media: dict[str, AttachmentTarget] = {}
        files: dict[str, AttachmentTarget] = {}
        taken: set[str] = set()
        ordered = sorted(
            issue_attachments(item), key=lambda att: (_moment(_time(att.get("created"))), _id_order(str(att["id"])))
        )
        for att in ordered:
            att_id = str(att["id"])
            entry = self.downloads.get(att_id) or {}
            shown = _filename(att)
            content_type = str(att.get("mimeType") or "").strip() or None
            if entry.get("status") == STORED:
                name = _unique_filename(shown, taken)
                target = AttachmentTarget(name, ref=att_id, content_type=content_type)
                self.records.attachments.append(
                    Attachment(
                        source_ref=att_id,
                        container_kind="issue",
                        container_ref=issue_ref,
                        filename=name,
                        sha256=str(entry["sha256"]),
                        content_type=content_type,
                        filesize=int(entry["size"]),
                        author_ref=self.user_ref(att.get("author")),
                        created_at=_time(att.get("created")),
                        extra={"jira_filename": att.get("filename")} if name != att.get("filename") else None,
                    )
                )
            else:
                reason, what = SKIPPED_ATTACHMENTS.get(str(entry.get("status")), NOT_DOWNLOADED)
                self.not_carried[what] += 1
                target = AttachmentTarget(shown, content_type=content_type, reason=reason)
            if entry.get("media_id"):
                media[str(entry["media_id"])] = target
            # Oldest first, so the newest of several with one name is the one a name refers to, as in Jira.
            files[shown] = target
        self.text.media, self.text.files = media, files

    def _parent(self, issue: JSON) -> tuple[str | None, JSON | None, str | None]:
        """The issue's parent as ``(parent_ref, None, None)``, or ``(None, what is known of it, why)`` when it
        cannot be written, or neither when it has none.

        The ref is the parent's id, in the bundle or not: Specivo links a parent of another bundle when both
        are imported.
        """
        parent = issue.get("fields", {}).get("parent")
        if isinstance(parent, dict) and parent.get("id"):
            found: JSON = {"key": parent.get("key"), "id": str(parent["id"])}
        else:
            key = epic_link(issue, self.epic_link_fields)
            if key is None:
                return None, None, None
            known = self.key_to_id.get(key) or ((self.refs.get("keys") or {}).get(key) or {}).get("id")
            if not known:
                # Not in the bundle, and Jira did not say (or was not asked) which issue the key is.
                return None, {"key": key}, PARENTS_UNKNOWN
            found = {"key": key, "id": str(known)}
        if found["id"] == str(issue["id"]):
            return None, None, None
        if found["id"] in self.gone:
            return None, found, PARENTS_GONE
        return str(found["id"]), None, None

    def _parent_detail(self, change: JSON) -> JournalDetail | None:
        """A parent change, by ref, with the parent's key as Jira shows it for its text.

        Jira gives the parent's id in ``from`` / ``to`` and its key in ``fromString`` / ``toString``;
        a value that gives only a key is looked up among the keys whose ids are known, as an Epic
        Link is. A parent whose id is not known is written by its key alone, which Specivo shows as
        text (or leaves out for a reader it cannot show the issue to).
        """
        old_ref = self._parent_value(change.get("from"), change.get("fromString"))
        new_ref = self._parent_value(change.get("to"), change.get("toString"))
        old_text, new_text = _text(change.get("fromString")), _text(change.get("toString"))
        if (old_ref, old_text) == (new_ref, new_text):
            return None
        return JournalDetail(
            property="attr",
            prop_key="parent_id",
            old_ref=old_ref,
            new_ref=new_ref,
            old_value=old_text,
            new_value=new_text,
        )

    def _parent_value(self, value: object, text: object) -> str | None:
        found = _text(value)
        if found is not None and found.isdigit():
            return found
        key = _text(text)
        if key is None:
            return None
        known = self.key_to_id.get(key) or ((self.refs.get("keys") or {}).get(key) or {}).get("id")
        return str(known) if known else None

    def _project_order(self) -> list[str]:
        """The projects, those holding parents of another's issues first (in the order given otherwise).

        Only parents in the bundle count: one of another bundle is linked whichever is loaded first.
        """
        keys = list(self.options.project_keys)
        project_of = {str(item["issue"]["id"]): key for key, item in self.items}
        needs: dict[str, set[str]] = {key: set() for key in keys}
        for child, (parent, _, _) in self.parents.items():
            if parent in project_of and project_of[parent] != project_of[child]:
                needs[project_of[child]].add(project_of[parent])
        order: list[str] = []
        while len(order) < len(keys):
            ready = [key for key in keys if key not in order and needs[key] <= set(order)]
            # Projects whose issues are each other's parents: the first one given goes first.
            order.append(ready[0] if ready else next(key for key in keys if key not in order))
        return order

    def _links(self, issue_ref: str, fields: JSON) -> list[JSON]:
        """Turn the issue's links into relations; return those to issues Jira no longer has, for ``extra``.

        The other issue may be outside the bundle: the relation names it by id, and every bundle holding one
        of the two issues writes it, under the link id.
        """
        gone: list[JSON] = []
        for link in fields.get("issuelinks") or []:
            if not isinstance(link, dict) or not link.get("id"):
                continue
            link_id = str(link["id"])
            kind = link.get("type") or {}
            direction = "outward" if isinstance(link.get("outwardIssue"), dict) else "inward"
            other = link.get(f"{direction}Issue") or {}
            other_ref = str(other.get("id") or "")
            if not other_ref or other_ref == issue_ref:
                continue
            if other_ref in self.gone:
                gone.append(
                    {
                        "id": link_id,
                        "type": kind.get("name"),
                        "direction": direction,
                        "phrase": kind.get(direction),
                        "issue_key": other.get("key"),
                        "issue_id": other_ref,
                    }
                )
                if link_id not in self.links_gone:
                    self.links_gone.add(link_id)
                    self.not_carried[LINKS_GONE] += 1
                continue
            if link_id in self.relations:
                continue
            # "<outward side> <outward name> <inward side>", as Jira reads it.
            source, target = (issue_ref, other_ref) if direction == "outward" else (other_ref, issue_ref)
            name = str(kind.get("name") or "")
            known = LINK_TYPES.get(name.casefold()) or LINK_OUTWARD_NAMES.get(str(kind.get("outward") or "").casefold())
            relation_type, swap = known or ("relates", False)
            if swap:
                source, target = target, source
            relation_extra: JSON | None = None
            if known is None:
                relation_extra = {"jira_link_type": name, "outward": kind.get("outward"), "inward": kind.get("inward")}
                self.not_carried[f"Issue links of type {name or kind.get('id')} (carried as relates)"] += 1
            self.relations[link_id] = Relation(
                source_ref=link_id, from_ref=source, to_ref=target, relation_type=relation_type, extra=relation_extra
            )
        return gone

    def _time_entries(self, project_key: str, issue_ref: str, item: JSON) -> None:
        entries: list[tuple[datetime, int, TimeEntry]] = []
        for worklog in item.get("worklogs") or []:
            seconds = worklog.get("timeSpentSeconds")
            started = _time(worklog.get("started"))
            if not isinstance(seconds, int) or seconds <= 0 or started is None or not worklog.get("id"):
                self.not_carried["Worklogs without a time spent or a start"] += 1
                continue
            hours = (Decimal(seconds) / 3600).quantize(HUNDREDTH, rounding=ROUND_HALF_UP)
            if hours == 0:
                self.not_carried["Worklogs under 18 seconds (no time once rounded to hundredths of an hour)"] += 1
                continue
            author = worklog.get("author") if isinstance(worklog.get("author"), dict) else {}
            if self.deployment is Deployment.CLOUD:
                comment = adf.to_plain_text(worklog.get("comment"), self.display_names)
            else:
                comment = str(worklog.get("comment") or "").strip() or None
            extra: JSON = {"jira_time_spent_seconds": seconds} if hours * 3600 != seconds else {}
            visibility = worklog.get("visibility")
            if isinstance(visibility, dict):
                # Specivo shows a time entry to everyone who may see the project's time, so the
                # comment of a worklog Jira shows to a group or role only is left out.
                extra["jira_visibility"] = {"type": visibility.get("type"), "value": visibility.get("value")}
                if comment:
                    self.not_carried["Comments of worklogs restricted to a group or role"] += 1
                comment = None
            entries.append(
                (
                    _moment(started),
                    _id_order(str(worklog["id"])),
                    TimeEntry(
                        source_ref=str(worklog["id"]),
                        project_ref=self.project_refs[project_key],
                        hours=hours,
                        spent_on=_local_date(started, author.get("timeZone")),
                        issue_ref=issue_ref,
                        user_ref=self.user_ref(author),
                        activity_ref=WORKLOG_ACTIVITY.source_ref,
                        comments=comment,
                        created_at=_time(worklog.get("created")),
                        updated_at=_time(worklog.get("updated")),
                        extra=extra or None,
                    ),
                )
            )
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        self.records.time_entries += [entry for _, _, entry in entries]

    def _listed_watchers(self, issue_ref: str) -> list[JSON]:
        entry = self.watcher_lists.get(issue_ref) or {}
        if entry.get("status") != LISTED:
            return []
        return [user for user in entry.get("watchers") or [] if isinstance(user, dict)]

    def _watchers(self, issue_ref: str, item: JSON) -> None:
        count = watch_count(item)
        if count <= 0:
            return
        entry = self.watcher_lists.get(issue_ref) or {}
        if entry.get("status") == FORBIDDEN:
            self.not_carried["Watchers Jira refused to list (HTTP 403)"] += count
            return
        listed = self._listed_watchers(issue_ref)
        seen: set[str] = set()
        for user in listed:
            ref = self.user_ref(user)
            if ref is None:
                self.not_carried["Watchers that are app accounts"] += 1
            elif ref not in seen:
                seen.add(ref)
                self.records.watchers.append(Watcher(container_kind="issue", container_ref=issue_ref, user_ref=ref))
        if len(listed) < count:
            self.not_carried["Watchers Jira did not list"] += count - len(listed)

    def _assignee_value(self, value: object) -> str | None:
        if value is None or value == "":
            return None
        text = str(value)
        if self.deployment is Deployment.DATA_CENTER:
            # Data Center's history names the user name; the bundle refers to users by key.
            return self.name_to_key.get(text, text)
        return text

    def _count_left_out(self, fields: JSON) -> None:
        names = self.field_names
        for name, value in fields.items():
            if name in CARRIED_FIELDS or name in DERIVED_FIELDS or name in self.epic_link_fields or _empty(value):
                continue
            if name in self.cf_kinds or name in self.sprint_field_ids:
                continue
            if name in LISTED_FIELDS:
                self.not_carried[LISTED_FIELDS[name]] += len(value) if isinstance(value, list) else 1
            elif name == "votes":
                if isinstance(value, dict) and value.get("votes"):
                    self.not_carried["Votes"] += int(value["votes"])
            else:
                label = names.get(name, name)
                what = f"Field {label} ({name})" if label != name else f"Field {name}"
                self.not_carried[f"{what}: values on issues"] += 1


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _attachment_detail(change: JSON) -> JournalDetail | None:
    """An attachment added (``to``) or removed (``from``) in the change history, as a journal detail."""
    added, removed = change.get("to"), change.get("from")
    ref = str(added or removed or "").strip()
    if not ref:
        return None
    return JournalDetail(
        property="attachment",
        prop_key=ref[:255],
        old_value=str(change.get("fromString") or "") or None if removed else None,
        new_value=str(change.get("toString") or "") or None if added else None,
    )


def _filename(att: JSON) -> str:
    """The name Jira shows for an attachment, fit for the bundle: no control characters, at most 255 characters."""
    name = " ".join(_CONTROL_RE.sub(" ", str(att.get("filename") or "")).split())
    return _shorten(name or f"attachment-{att['id']}", MAX_FILENAME)


def _unique_filename(name: str, taken: set[str]) -> str:
    """*name*, or ``stem (2).ext`` and so on when an earlier attachment of the issue has it."""
    candidate, number = name, 1
    stem, dot, extension = name.rpartition(".")
    if not stem:
        stem, dot, extension = name, "", ""
    while candidate in taken:
        number += 1
        suffix = f" ({number}){dot}{extension}"
        candidate = _shorten(stem, MAX_FILENAME - len(suffix)) + suffix
    taken.add(candidate)
    return candidate


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    stem, dot, extension = text.rpartition(".")
    if stem and len(extension) <= 10:
        return stem[: limit - len(extension) - 1] + dot + extension
    return text[:limit]


def _issue_order(item: JSON) -> tuple[int, int]:
    key = str(item["issue"].get("key") or "")
    number = key.rsplit("-", 1)[-1]
    return (int(number) if number.isdigit() else 0, _id_order(str(item["issue"].get("id"))))


def _id_order(ref: str) -> int:
    return int(ref) if ref.isdigit() else 0


def _field_order(field_id: str) -> tuple[int, str]:
    number = field_id.rsplit("_", 1)[-1]
    return (int(number) if number.isdigit() else 0, field_id)


def _list_names(item: JSON) -> dict[tuple[str, str], str]:
    """Jira's names for the fix versions and components an issue has or had, by ``(attribute, id)``.

    From the issue's lists now and the history's strings, so a version since deleted keeps its name.
    """
    names: dict[tuple[str, str], str] = {}
    fields: JSON = item["issue"].get("fields", {})
    for attribute, field_name in (("fixed_version_id", "fixVersions"), ("category_id", "components")):
        for entry in fields.get(field_name) or []:
            if isinstance(entry, dict) and entry.get("id") and entry.get("name"):
                names[(attribute, str(entry["id"]))] = str(entry["name"])
    for history in item["histories"]:
        for change in history.get("items") or []:
            listed = LIST_HISTORY_FIELDS.get(_history_field(change).casefold())
            if listed is None:
                continue
            for side in ("from", "to"):
                ref, text = change.get(side), change.get(f"{side}String")
                if ref and text:
                    names.setdefault((listed, str(ref)), str(text))
    return names


def _text(value: object) -> str | None:
    """A history value as text, an empty one as none."""
    if value is None or value == "":
        return None
    return str(value)


def _list_refs(value: object) -> list[str]:
    """The ids of a list field's values (fix versions, components), in Jira's order."""
    if not isinstance(value, list):
        return []
    return [str(entry["id"]) for entry in value if isinstance(entry, dict) and entry.get("id")]


def _ordered(refs: set[str], ranks: Mapping[str, int], positions: Mapping[str, int]) -> list[str]:
    """*refs* in the issue's order (*ranks*), those it no longer has after them in the project's (*positions*)."""
    return sorted(
        refs,
        key=lambda ref: (
            ref not in ranks,
            ranks.get(ref, 0),
            ref not in positions,
            positions.get(ref, 0),
            _id_order(ref),
            ref,
        ),
    )


def _estimate(fields: JSON, name: str, extra: JSON, seconds_key: str) -> Decimal | None:
    """The estimate in *fields* as hours; its seconds go into *extra* when rounding changed them."""
    seconds = fields.get(name)
    hours = _hours(seconds)
    if hours is not None and hours * 3600 != seconds:
        extra[seconds_key] = seconds
    return hours


def _hours(seconds: object) -> Decimal | None:
    """Seconds (a number, or digits as the history writes them) as hours, rounded to hundredths."""
    if isinstance(seconds, bool):
        return None
    if isinstance(seconds, str) and seconds.strip().isdigit():
        seconds = int(seconds.strip())
    if not isinstance(seconds, int) or seconds < 0:
        return None
    return (Decimal(seconds) / 3600).quantize(HUNDREDTH, rounding=ROUND_HALF_UP)


def _history_field(change: JSON) -> str:
    return str(change.get("fieldId") or change.get("field") or "")


def _initial_status(item: JSON) -> str | None:
    """The status an issue was created in: the first status change's old value, or its status now."""
    changes = []
    for history in item["histories"]:
        for change in history.get("items") or []:
            if _history_field(change) == "status" and change.get("from"):
                changes.append((_moment(_time(history.get("created"))), _id_order(str(history.get("id"))), change))
    if changes:
        changes.sort(key=lambda entry: (entry[0], entry[1]))
        return str(changes[0][2]["from"])
    status = item["issue"].get("fields", {}).get("status") or {}
    return str(status["id"]) if status.get("id") else None


def _moment(value: str | None) -> datetime:
    """A timestamp as an aware datetime for sorting; a missing one sorts first."""
    return datetime.fromisoformat(value) if value else datetime.min.replace(tzinfo=UTC)


def _date(value: object) -> str | None:
    """A Jira date (``2026-11-19``) as a bundle date; None when absent or not a date."""
    try:
        return date_value(str(value)) if value else None
    except ValueError:
        return None


def _local_date(started: str, zone: object) -> date:
    """The day a worklog was spent: *started* in the author's time zone when Jira names one, else as written."""
    moment = datetime.fromisoformat(started)
    if isinstance(zone, str) and zone:
        try:
            moment = moment.astimezone(ZoneInfo(zone))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return moment.date()


def _time(value: object) -> str | None:
    if not value:
        return None
    return timestamp(str(value))


def _empty(value: object) -> bool:
    if value is None or value == "" or value == [] or value == {}:
        return True
    if isinstance(value, dict):
        return all(_empty(item) for item in value.values())
    return False


def _login_text(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower().strip()
    folded = re.sub(r"\s+", ".", folded)
    return _LOGIN_UNSAFE_RE.sub("", folded).strip("._-")[:60]


def _unique(login: str, taken: set[str]) -> str:
    candidate, number = login, 1
    while candidate in taken:
        number += 1
        candidate = f"{login}-{number}"
    taken.add(candidate)
    return candidate


def _language(locale: object) -> str | None:
    if not isinstance(locale, str) or not locale:
        return None
    return locale.replace("_", "-").split("-", 1)[0].lower() or None


def _restriction(fields: JSON) -> JSON:
    """Who Jira limits an issue to: its security level and its restrictions, empty when none.

    Specivo has no equivalent of either, so a restricted issue is imported as private (seen only
    by those who may see private issues) rather than visible to every project member.
    """
    restriction: JSON = {}
    security = fields.get("security")
    if isinstance(security, dict) and (security.get("id") or security.get("name")):
        restriction["security_level"] = {"id": security.get("id"), "name": security.get("name")}
    restrictions = (fields.get("issuerestriction") or {}).get("issuerestrictions")
    if restrictions:
        restriction["issue_restrictions"] = restrictions
    return restriction
