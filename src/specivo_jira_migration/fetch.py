"""Read what the export needs from Jira into the state directory.

Phases, each resumable:

1. **Site**: ``serverInfo`` and the site-wide lists (fields, statuses,
   priorities, issue types), read once and kept.
2. **Per project**: the project with its components and versions (paged on
   Cloud), then its issue ids through search (the page cursor is saved after
   every page), then every issue with its full comment list, worklogs and
   change history, each written to its own file as soon as it is complete.
   An issue already on file is not read again.
3. **References**: users who are only mentioned in text, Data Center user
   names in the change history, and issue keys that are not in the bundle,
   in text and in the Epic Link field (which names an epic by key only),
   looked up once and kept.
4. **Attachments**: every file in an issue's ``attachment`` field is
   downloaded into the state directory (see :mod:`.state`), one at a time,
   and what became of each is saved as soon as it is known. A file already
   stored and still hashing to its name is not downloaded again.
5. **Watchers**: ``GET /rest/api/{version}/issue/{id}/watchers`` for each issue whose
   ``watches.watchCount`` is above zero, and only for those. The account
   needs "View voters and watchers"; a 403 is recorded and counted, not an
   error.
6. **Sprints**: each project's boards (``GET /rest/agile/1.0/board?projectKeyOrId=KEY``)
   and every scrum board's sprints (``/rest/agile/1.0/board/{id}/sprint``),
   then the boards those sprints and the issues' Sprint field name that were
   not listed, for the project each is located in (see :mod:`.sprints`). A
   site without Jira Software, or an account that may not read boards, is
   recorded and the sprints are taken from the issues' Sprint field alone.
7. **Custom field contexts** (Cloud): for each custom field the bundle will
   carry, its contexts (``/rest/api/3/field/{id}/context``), the projects and
   issue types each applies to, and the options of an option field. Reading
   them takes "Administer Jira"; a refusal is recorded, and the field is then
   scoped to the projects whose issues use it. Data Center has no such API;
   its fields are scoped that way too.

Search lists ids only. Each issue is then read with ``GET
/rest/api/{version}/issue/{id}`` and its comments and history from the same
version: **REST v3 on Cloud**, which returns descriptions, comment bodies,
the environment and textarea custom fields as Atlassian Document Format, the
editor's own JSON, converted by :mod:`.adf`; **REST v2 on Data Center**,
which returns wiki markup, converted by :mod:`.convert`. (Cloud's v2 turns
ADF into wiki markup first and loses part of it on the way.) Change history
values (``fromString``/``toString``) are strings on both: on Cloud a
description change holds wiki markup and a summary change plain text. The
site-wide lists are read through v2 on both; they do not differ.

Comments, worklogs and change history embedded in an issue are truncated
(Cloud keeps the newest 100 history entries and 20 worklogs), so when an
embedded list says it holds fewer items than its ``total``, the full list is
read from its own paged endpoint.
Data Center has no paged changelog endpoint; there the embedded history is
complete, and if it ever is not, the shortfall is counted in the manifest.

Attachments are read from ``GET /rest/api/3/attachment/content/{id}`` on
Cloud. Jira answers with a redirect to its media host, and the ``Location``
names the media file id (``/file/<uuid>/binary``) that ADF ``media`` nodes
refer to the file by: that is how an image in the text is tied to its
attachment, and it is kept with the attachment. (The attachment's metadata
does not hold the media id, and ``renderedFields`` HTML names it only for
some nodes.) A file over ``--max-attachment-size`` is not downloaded, but its
redirect is still read, so the text can say which file stood there. On Data
Center the attachment's own ``content`` URL is used when it is on the site,
and ``/rest/api/2/attachment/content/{id}`` otherwise; wiki markup names
attachments by file name, so no media ids are needed. A 403 or 404 on a
download is recorded and counted, not an error.

Company-managed and team-managed projects read the same way. Team-managed
projects have issue types (and fields) of their own, scoped to the project,
and use ``parent`` for epics and sub-tasks; both come through as ordinary
issue fields here.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from specivo_jira_migration import adf, convert, fields, sprints
from specivo_jira_migration.client import Deployment, DownloadTooLargeError, JiraClient, JiraError
from specivo_jira_migration.state import (
    ATTACHMENTS_FILE,
    ISSUE_ID,
    META_FILE,
    REFS_FILE,
    WATCHERS_FILE,
    StateDir,
    holds,
)

type Progress = Callable[[str], None]
type JSON = dict[str, Any]

# 100 MiB: the default limit on one attachment.
DEFAULT_MAX_ATTACHMENT_SIZE = 100 * 1024 * 1024

# What became of an attachment, as attachments.json records it.
STORED = "stored"
TOO_LARGE = "too_large"
FORBIDDEN = "forbidden"
GONE = "gone"
# What Jira answered about an issue's watchers, as watchers.json records it.
LISTED = "listed"
# What Jira answered about a project's boards or a field's contexts, as meta.json records it.
UNAVAILABLE = "unavailable"
# Statuses that mean "not here, or not for this account": no Jira Software, no permission, no such board.
AGILE_UNAVAILABLE = frozenset({400, 401, 403, 404})
# The Epic Link custom field of Jira Software (Data Center, and older Cloud sites).
EPIC_LINK_FIELD = "com.pyxis.greenhopper.jira:gh-epic-link"
# The media file id in the redirect Jira Cloud answers an attachment download with.
MEDIA_ID_RE = re.compile(r"/file/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:/|$)")


class FetchError(Exception):
    """Jira does not have what the export was asked for (an unknown project, say)."""


@dataclass
class FetchOptions:
    projects: list[str]
    limit: int | None = None
    resolve_keys: bool = True
    # None: no limit.
    max_attachment_size: int | None = DEFAULT_MAX_ATTACHMENT_SIZE


def media_id(location: str | None) -> str | None:
    """The media file id in a redirect's ``Location``, or None."""
    # Only the path: the query carries a token on Jira's media host.
    path = (location or "").split("?", 1)[0].split("#", 1)[0]
    match = MEDIA_ID_RE.search(path)
    return match.group(1) if match else None


def issue_attachments(item: JSON) -> list[JSON]:
    """An issue's attachments as Jira lists them, those with a numeric id only."""
    found = item["issue"].get("fields", {}).get("attachment") or []
    return [att for att in found if isinstance(att, dict) and ISSUE_ID.fullmatch(str(att.get("id") or ""))]


def jql_for(project_key: str) -> str:
    """Every issue of one project, oldest key first."""
    escaped = project_key.replace("\\", "\\\\").replace('"', '\\"')
    return f'project = "{escaped}" ORDER BY key ASC'


class Fetcher:
    def __init__(self, client: JiraClient, state: StateDir, options: FetchOptions, progress: Progress) -> None:
        self.client = client
        self.state = state
        self.options = options
        self.progress = progress

    async def run(self) -> None:
        await self._site()
        for key in self.options.projects:
            await self._project(key)
        await self._references()
        await self._attachments()
        await self._watchers()
        await self._agile()
        await self._field_contexts()

    # ------------------------------------------------------------------
    # Site
    # ------------------------------------------------------------------

    @property
    def deployment(self) -> Deployment:
        return Deployment(self.state.data["server"]["deployment"])

    @property
    def api(self) -> str:
        """Where issues, comments and history are read: REST v3 (ADF) on Cloud, v2 (wiki markup) on Data Center."""
        return "/rest/api/3" if self.deployment is Deployment.CLOUD else "/rest/api/2"

    async def _site(self) -> None:
        if "server" not in self.state.data:
            info = await self.client.server_info()
            self.state.data["server"] = {
                "base_url": info.base_url.rstrip("/"),
                "version": info.version,
                "deployment": info.deployment.value,
            }
            self.state.save()
        if self.deployment is Deployment.CLOUD and self.client.auth_kind != "cloud":
            raise FetchError("this is Jira Cloud: sign in with --email and an API token, not a personal access token")
        if self.deployment is Deployment.DATA_CENTER and self.client.auth_kind == "cloud":
            raise FetchError("this is Jira Data Center: sign in with a personal access token (--pat or JIRA_PAT)")
        kind = "Cloud" if self.deployment is Deployment.CLOUD else "Data Center"
        self.progress(f"Jira {kind} {self.state.data['server'].get('version') or ''}".rstrip())

        meta = self.state.read_json(META_FILE)
        if meta is None:
            meta = {
                "fields": await self.client.get("/rest/api/2/field") or [],
                "statuses": await self.client.get("/rest/api/2/status") or [],
                "priorities": await self.client.get("/rest/api/2/priority") or [],
                "issue_types": await self.client.get("/rest/api/2/issuetype") or [],
                "projects": {},
            }
            self.state.write_json(META_FILE, meta)
        if "time_zone" not in meta:
            # The zone of the account that exports: sprint dates are the days Jira shows it.
            myself = await self.client.get("/rest/api/2/myself", allow_404=True)
            meta["time_zone"] = str(myself.get("timeZone") or "") if isinstance(myself, dict) else ""
            self.state.write_json(META_FILE, meta)
        self.meta: JSON = meta

    # ------------------------------------------------------------------
    # Projects and issues
    # ------------------------------------------------------------------

    async def _project(self, key: str) -> None:
        projects: JSON = self.meta.setdefault("projects", {})
        if key not in projects:
            project = await self.client.get(f"/rest/api/2/project/{key}", allow_404=True)
            if not isinstance(project, dict):
                raise FetchError(f"Jira has no project {key}, or this account cannot see it")
            projects[key] = project
            self.state.write_json(META_FILE, self.meta)
        lists: JSON = self.meta.setdefault("project_lists", {})
        if key not in lists:
            lists[key] = {"components": await self._project_list(key, "component"),
                          "versions": await self._project_list(key, "version")}  # fmt: skip
            self.state.write_json(META_FILE, self.meta)
        entry = self.state.project(key)
        if not entry["search_done"]:
            await self._search(key, entry)
        ids: list[str] = entry["ids"]
        missing = [issue_id for issue_id in ids if not self.state.has_issue(issue_id)]
        if missing:
            self.progress(f"{key}: reading {len(missing)} of {len(ids)} issues")
        for done, issue_id in enumerate(missing, start=1):
            self.state.write_issue(issue_id, await self._issue(issue_id))
            if done % 50 == 0 and done < len(missing):
                self.progress(f"{key}: {done} of {len(missing)} issues read")
        self.progress(f"{key}: {len(ids)} issues read")

    async def _project_list(self, key: str, kind: str) -> list[JSON]:
        """A project's components or versions: paged on Cloud, one list on Data Center."""
        if self.deployment is Deployment.CLOUD:
            return await self.client.paged_values(f"/rest/api/2/project/{key}/{kind}", "values")
        found = await self.client.get(f"/rest/api/2/project/{key}/{kind}s") or []
        return [item for item in found if isinstance(item, dict)]

    async def _search(self, key: str, entry: JSON) -> None:
        limit = self.options.limit
        ids: list[str] = entry["ids"]
        known = set(ids)
        self.progress(f"{key}: listing issues" + (" (continuing)" if entry["cursor"] is not None else ""))

        def take(issues: list[JSON]) -> None:
            for issue in issues:
                issue_id = str(issue.get("id") or "")
                if not ISSUE_ID.fullmatch(issue_id):
                    self.progress(f"{key}: skipped a search result without a numeric issue id")
                    continue
                if issue_id not in known and (limit is None or len(ids) < limit):
                    known.add(issue_id)
                    ids.append(issue_id)

        jql = jql_for(key)
        if self.deployment is Deployment.CLOUD:
            try:
                async for issues, token in self.client.search_cloud(jql, next_page_token=entry["cursor"]):
                    take(issues)
                    entry["cursor"] = token
                    self.state.save()
                    if limit is not None and len(ids) >= limit:
                        break
            except JiraError as exc:
                if exc.status != 400 or entry["cursor"] is None:
                    raise
                # A saved page token has expired: list again from the start; ids already
                # taken are kept and not taken twice.
                entry["cursor"] = None
                self.state.save()
                await self._search(key, entry)
                return
        else:
            start = int(entry["cursor"] or 0)
            async for issues, next_start in self.client.search_data_center(jql, start_at=start):
                take(issues)
                entry["cursor"] = next_start
                self.state.save()
                if limit is not None and len(ids) >= limit:
                    break
        entry["search_done"] = True
        entry["cursor"] = None
        self.state.save()

    async def _issue(self, issue_id: str) -> JSON:
        issue = await self.client.get(
            f"{self.api}/issue/{issue_id}", {"fields": "*all", "expand": "changelog"}, allow_404=True
        )
        if not isinstance(issue, dict):
            # Deleted (or moved out of sight) since the search: nothing to carry.
            return {"missing": True, "id": issue_id}
        fields: JSON = issue.setdefault("fields", {})
        embedded_comments = fields.pop("comment", None) or {}
        comments = list(embedded_comments.get("comments") or [])
        if _truncated(embedded_comments, len(comments)):
            comments = await self.client.paged_values(f"{self.api}/issue/{issue_id}/comment", "comments")

        embedded_worklogs = fields.pop("worklog", None) or {}
        worklogs = list(embedded_worklogs.get("worklogs") or [])
        if _truncated(embedded_worklogs, len(worklogs)):
            worklogs = await self.client.paged_values(f"{self.api}/issue/{issue_id}/worklog", "worklogs")

        changelog = issue.pop("changelog", None) or {}
        histories = list(changelog.get("histories") or [])
        missing_histories = 0
        if _truncated(changelog, len(histories)):
            if self.deployment is Deployment.CLOUD:
                histories = await self.client.paged_values(f"{self.api}/issue/{issue_id}/changelog", "values")
            else:
                missing_histories = int(changelog.get("total") or 0) - len(histories)
        return {
            "issue": issue,
            "comments": comments,
            "worklogs": worklogs,
            "histories": histories,
            "missing_histories": missing_histories,
        }

    # ------------------------------------------------------------------
    # References
    # ------------------------------------------------------------------

    async def _references(self) -> None:
        refs: JSON = self.state.read_json(REFS_FILE) or {"users": {}, "keys": {}}
        issues = [
            self.state.read_issue(issue_id)
            for key in self.options.projects
            for issue_id in self.state.project(key)["ids"]
        ]
        issues = [issue for issue in issues if not issue.get("missing")]
        known_users, user_names = _users_seen(issues, self.deployment)
        bundle_keys = {str(item["issue"].get("key")) for item in issues}

        site_url = str(self.state.data["server"].get("base_url") or "")
        wanted_users: set[str] = set()
        wanted_keys: set[str] = set()
        for item in issues:
            for text in _texts(item):
                if isinstance(text, str):
                    wanted_users |= convert.mentioned_ids(text)
                    wanted_keys |= convert.candidate_keys(text, site_url)
                else:
                    wanted_users |= adf.mentioned_ids(text)
                    wanted_keys |= adf.candidate_keys(text, site_url)
            if self.deployment is Deployment.DATA_CENTER:
                for history in item["histories"]:
                    for change in history.get("items") or []:
                        if change.get("field") == "assignee":
                            wanted_users |= {str(v) for v in (change.get("from"), change.get("to")) if v}
        epic_fields = epic_link_fields(self.state.read_json(META_FILE) or {})
        for item in issues:
            epic = epic_link(item["issue"], epic_fields)
            if epic:
                wanted_keys.add(epic)
        wanted_users -= known_users | user_names
        wanted_users -= set(refs["users"])
        wanted_keys -= bundle_keys
        if not self.options.resolve_keys:
            wanted_keys = set()
        wanted_keys -= set(refs["keys"])

        if wanted_users or wanted_keys:
            self.progress(f"Looking up {len(wanted_users)} mentioned users and {len(wanted_keys)} issue keys")
        param = "accountId" if self.deployment is Deployment.CLOUD else "username"
        for name in sorted(wanted_users):
            refs["users"][name] = await self._lookup("/rest/api/2/user", {param: name})
            self.state.write_json(REFS_FILE, refs)
        for issue_key in sorted(wanted_keys):
            found = await self._lookup(f"/rest/api/2/issue/{issue_key}", {"fields": "key"})
            refs["keys"][issue_key] = {"id": str(found["id"]), "key": found.get("key")} if found else None
            self.state.write_json(REFS_FILE, refs)
        if not (self.state.path / REFS_FILE).exists():
            self.state.write_json(REFS_FILE, refs)

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    async def _attachments(self) -> None:
        done: JSON = self.state.read_json(ATTACHMENTS_FILE) or {}
        self.state.remove_partial_files()
        wanted: list[JSON] = []
        for key in self.options.projects:
            for issue_id in self.state.project(key)["ids"]:
                item = self.state.read_issue(issue_id)
                if not item.get("missing"):
                    wanted += issue_attachments(item)
        todo = [att for att in wanted if not self._finished(done.get(str(att["id"])), att)]
        if not todo:
            if not (self.state.path / ATTACHMENTS_FILE).exists():
                self.state.write_json(ATTACHMENTS_FILE, done)
            return
        total = sum(int(att["size"]) for att in todo if isinstance(att.get("size"), int))
        self.progress(f"Downloading {len(todo)} of {len(wanted)} attachments ({_megabytes(total)})")
        for number, att in enumerate(todo, start=1):
            done[str(att["id"])] = await self._attachment(att)
            self.state.write_json(ATTACHMENTS_FILE, done)
            if number % 50 == 0 and number < len(todo):
                self.progress(f"{number} of {len(todo)} attachments downloaded")

    def _finished(self, entry: JSON | None, att: JSON) -> bool:
        """Whether an attachment needs nothing more: stored and intact, or skipped for a reason that still holds."""
        if not isinstance(entry, dict):
            return False
        status = entry.get("status")
        if status == STORED:
            return holds(self.state.file_path(str(entry.get("sha256"))), str(entry.get("sha256")))
        if status == TOO_LARGE:
            # A run with a higher limit (or none) downloads what an earlier one skipped.
            limit = self.options.max_attachment_size
            return limit is not None and limit <= int(entry.get("limit") or 0)
        return status in (FORBIDDEN, GONE)

    def _content_url(self, att: JSON) -> str:
        att_id = str(att["id"])
        if self.deployment is Deployment.CLOUD:
            return f"/rest/api/3/attachment/content/{att_id}"
        content = str(att.get("content") or "")
        # Only a URL on the site itself is followed with the token; anything else is ignored.
        if content and self.client.trusted(content):
            return content
        return f"/rest/api/2/attachment/content/{att_id}"

    async def _attachment(self, att: JSON) -> JSON:
        url = self._content_url(att)
        limit = self.options.max_attachment_size
        declared = att.get("size")
        if limit is not None and isinstance(declared, int) and declared > limit:
            location = None
            if self.deployment is Deployment.CLOUD:
                try:
                    location = await self.client.locate(url)
                except JiraError as exc:
                    if exc.status not in (403, 404, 410):
                        raise
            return {"status": TOO_LARGE, "size": declared, "limit": limit, "media_id": media_id(location)}

        handle, partial = self.state.partial_file()
        try:
            with os.fdopen(handle, "wb") as target:
                got = await self.client.download(url, target, max_bytes=limit)
        except DownloadTooLargeError as exc:
            partial.unlink(missing_ok=True)
            # Jira said it was smaller (or did not say); the bytes said otherwise.
            return {"status": TOO_LARGE, "size": None, "limit": exc.limit, "media_id": media_id(exc.location)}
        except JiraError as exc:
            partial.unlink(missing_ok=True)
            if exc.status == 403:
                return {"status": FORBIDDEN}
            if exc.status in (404, 410):
                return {"status": GONE}
            raise
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        self.state.keep_file(partial, got.sha256)
        return {"status": STORED, "sha256": got.sha256, "size": got.size, "media_id": media_id(got.location)}

    # ------------------------------------------------------------------
    # Watchers
    # ------------------------------------------------------------------

    async def _watchers(self) -> None:
        done: JSON = self.state.read_json(WATCHERS_FILE) or {}
        todo: list[str] = []
        for key in self.options.projects:
            for issue_id in self.state.project(key)["ids"]:
                item = self.state.read_issue(issue_id)
                if not item.get("missing") and watch_count(item) > 0 and issue_id not in done:
                    todo.append(issue_id)
        if todo:
            self.progress(f"Listing the watchers of {len(todo)} issues")
        for issue_id in todo:
            try:
                found = await self.client.get(f"{self.api}/issue/{issue_id}/watchers", allow_404=True)
            except JiraError as exc:
                if exc.status != 403:
                    raise
                done[issue_id] = {"status": FORBIDDEN}
            else:
                listed = (found or {}).get("watchers") if isinstance(found, dict) else None
                done[issue_id] = {"status": LISTED, "watchers": [u for u in listed or [] if isinstance(u, dict)]}
            self.state.write_json(WATCHERS_FILE, done)
        if not (self.state.path / WATCHERS_FILE).exists():
            self.state.write_json(WATCHERS_FILE, done)

    # ------------------------------------------------------------------
    # Sprints
    # ------------------------------------------------------------------

    def _issues(self) -> list[JSON]:
        found = [
            self.state.read_issue(issue_id)
            for key in self.options.projects
            for issue_id in self.state.project(key)["ids"]
        ]
        return [item for item in found if not item.get("missing")]

    async def _agile(self) -> None:
        agile: JSON = self.meta.setdefault("agile", {})
        projects: JSON = agile.setdefault("projects", {})
        boards: JSON = agile.setdefault("boards", {})
        listed: JSON = agile.setdefault("sprints", {})
        for key in self.options.projects:
            if key in projects:
                continue
            try:
                found = await self.client.paged_values(
                    "/rest/agile/1.0/board", "values", params={"projectKeyOrId": key}
                )
            except JiraError as exc:
                if exc.status not in AGILE_UNAVAILABLE:
                    raise
                projects[key] = {"status": UNAVAILABLE, "http_status": exc.status}
                self.state.write_json(META_FILE, self.meta)
                continue
            for board in found:
                board_id = str(board.get("id") or "")
                if not board_id:
                    continue
                boards[board_id] = board
                if str(board.get("type") or "") != "scrum" or board.get("sprints_read"):
                    continue
                for sprint in await self._board_sprints(board_id):
                    if sprint.get("id") is not None:
                        listed.setdefault(str(sprint["id"]), sprint)
                board["sprints_read"] = True
            projects[key] = {"status": LISTED, "boards": [str(b["id"]) for b in found if b.get("id")]}
            self.state.write_json(META_FILE, self.meta)
            self.progress(f"{key}: {len(found)} boards")

        # The boards sprints were created on, for the project they are located in.
        wanted = {str(s.get("originBoardId")) for s in listed.values() if s.get("originBoardId")}
        for item in self._issues():
            for field_id in sprint_fields(self.meta):
                for named in sprints.issue_sprints(item["issue"].get("fields", {}).get(field_id)):
                    if named.board_id:
                        wanted.add(named.board_id)
        for board_id in sorted(wanted - set(boards), key=_number):
            if not board_id.isdigit():
                continue
            read: object = None
            try:
                read = await self.client.get(f"/rest/agile/1.0/board/{board_id}", allow_404=True)
            except JiraError as exc:
                if exc.status not in AGILE_UNAVAILABLE:
                    raise
            boards[board_id] = read if isinstance(read, dict) else {"id": board_id, "missing": True}
            self.state.write_json(META_FILE, self.meta)
        self.state.write_json(META_FILE, self.meta)

    async def _board_sprints(self, board_id: str) -> list[JSON]:
        try:
            return await self.client.paged_values(f"/rest/agile/1.0/board/{board_id}/sprint", "values")
        except JiraError as exc:
            # 400: a board that has no sprints (Jira refuses the list for a kanban board).
            if exc.status not in AGILE_UNAVAILABLE:
                raise
            return []

    # ------------------------------------------------------------------
    # Custom field contexts
    # ------------------------------------------------------------------

    async def _field_contexts(self) -> None:
        if self.deployment is not Deployment.CLOUD:
            return
        done: JSON = self.meta.setdefault("field_contexts", {})
        in_use = fields.fields_in_use(self._issues(), self.meta.get("fields") or [])
        wanted = [
            entry
            for entry in self.meta.get("fields") or []
            if str(entry.get("id")) in in_use
            and isinstance(fields.classify(entry), fields.FieldKind)
            and (entry.get("scope") or {}).get("type") != "PROJECT"
            and str(entry.get("id")) not in done
        ]
        if wanted:
            self.progress(f"Reading the contexts of {len(wanted)} custom fields")
        for entry in wanted:
            field_id = str(entry["id"])
            done[field_id] = await self._contexts(field_id, fields.classify(entry))
            self.state.write_json(META_FILE, self.meta)

    async def _contexts(self, field_id: str, kind: object) -> JSON:
        base = f"/rest/api/3/field/{field_id}/context"
        try:
            listed = await self.client.paged_values(base, "values")
            contexts: list[JSON] = []
            for context in listed:
                context_id = str(context.get("id") or "")
                if not context_id:
                    continue
                entry: JSON = {
                    "id": context_id,
                    "global": bool(context.get("isGlobalContext")),
                    "any_issue_type": bool(context.get("isAnyIssueType")),
                    "projects": [],
                    "issue_types": [],
                    "options": [],
                }
                if not entry["global"]:
                    mapped = await self.client.paged_values(
                        f"{base}/projectmapping", "values", params={"contextId": context_id}
                    )
                    entry["projects"] = [str(m["projectId"]) for m in mapped if m.get("projectId")]
                if not entry["any_issue_type"]:
                    mapped = await self.client.paged_values(
                        f"{base}/issuetypemapping", "values", params={"contextId": context_id}
                    )
                    entry["issue_types"] = [str(m["issueTypeId"]) for m in mapped if m.get("issueTypeId")]
                if isinstance(kind, fields.FieldKind) and kind.field_format == "list":
                    entry["options"] = await self.client.paged_values(f"{base}/{context_id}/option", "values")
                contexts.append(entry)
        except JiraError as exc:
            if exc.status not in AGILE_UNAVAILABLE:
                raise
            return {"status": UNAVAILABLE, "http_status": exc.status}
        return {"status": LISTED, "contexts": contexts}

    async def _lookup(self, path: str, params: JSON) -> JSON | None:
        try:
            found = await self.client.get(path, params, allow_404=True)
        except JiraError as exc:
            # A string that only looks like a key ("SHA-256") can be refused as a bad request.
            if exc.status == 400:
                return None
            raise
        return found if isinstance(found, dict) else None


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def _truncated(container: JSON, held: int) -> bool:
    total = container.get("total")
    return isinstance(total, int) and total > held


def _texts(item: JSON) -> list[str | JSON]:
    """The texts the bundle carries: wiki markup (Data Center) or ADF documents (Cloud)."""
    texts = [item["issue"].get("fields", {}).get("description")]
    texts += [comment.get("body") for comment in item["comments"]]
    return [text for text in texts if isinstance(text, str | dict)]


def watch_count(item: JSON) -> int:
    """How many watch the issue, as the issue itself says."""
    watches = item["issue"].get("fields", {}).get("watches")
    count = watches.get("watchCount") if isinstance(watches, dict) else None
    return count if isinstance(count, int) else 0


def epic_link_fields(meta: JSON) -> frozenset[str]:
    """The ids of the site's Epic Link fields, from its field list."""
    return frozenset(
        str(entry.get("id"))
        for entry in meta.get("fields") or []
        if (entry.get("schema") or {}).get("custom") == EPIC_LINK_FIELD
    )


def sprint_fields(meta: JSON) -> list[str]:
    """The ids of the site's Sprint fields, from its field list."""
    return sorted(
        str(entry.get("id"))
        for entry in meta.get("fields") or []
        if (entry.get("schema") or {}).get("custom") == fields.SPRINT_FIELD
    )


def _number(text: str) -> int:
    return int(text) if text.isdigit() else 0


def epic_link(issue: JSON, fields: frozenset[str]) -> str | None:
    """The epic key the issue's Epic Link field names, when that is its parent: it has no ``parent``.

    Of several Epic Link fields, the first (by field id) that holds a value.
    """
    values = issue.get("fields", {})
    parent = values.get("parent")
    if isinstance(parent, dict) and parent.get("id"):
        return None
    for field_id in sorted(fields):
        value = values.get(field_id)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def user_key(user: JSON | None, deployment: Deployment) -> str | None:
    """A user's identity in Jira: the Cloud account id, or the Data Center user key."""
    if not isinstance(user, dict):
        return None
    value = user.get("accountId") if deployment is Deployment.CLOUD else (user.get("key") or user.get("name"))
    return str(value) if value else None


def user_objects(item: JSON) -> list[JSON]:
    """Every user object one issue names: reporter, assignee, comment, worklog and history authors."""
    fields = item["issue"].get("fields", {})
    found = [fields.get("reporter"), fields.get("assignee"), fields.get("creator")]
    found += [comment.get("author") for comment in item["comments"]]
    found += [worklog.get("author") for worklog in item["worklogs"]]
    found += [history.get("author") for history in item["histories"]]
    found += [att.get("author") for att in issue_attachments(item)]
    found += custom_field_users(fields)
    return [user for user in found if isinstance(user, dict)]


def custom_field_users(values: JSON) -> list[JSON]:
    """The users an issue's custom fields hold (user pickers, single and multiple)."""
    found: list[JSON] = []
    for name, value in values.items():
        if not name.startswith("customfield_"):
            continue
        for entry in value if isinstance(value, list) else [value]:
            if is_user(entry):
                found.append(entry)
    return found


def is_user(value: object) -> bool:
    """Whether a field value is a user: a Cloud account, or a Data Center user with a key and a name."""
    if not isinstance(value, dict):
        return False
    if value.get("accountId"):
        return True
    return bool(value.get("key") and value.get("name") and "displayName" in value)


def _users_seen(issues: list[JSON], deployment: Deployment) -> tuple[set[str], set[str]]:
    keys: set[str] = set()
    names: set[str] = set()
    for item in issues:
        for user in user_objects(item):
            key = user_key(user, deployment)
            if key:
                keys.add(key)
            if user.get("name"):
                names.add(str(user["name"]))
    return keys, names
