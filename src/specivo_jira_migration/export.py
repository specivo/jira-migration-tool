"""Export Jira projects to an import bundle: read into the state directory, then write.

:func:`export_bundle` runs both halves. Reading (see :mod:`.fetch`) is the slow,
fallible part and is resumable; writing (see :mod:`.mapping`) reads only the
state directory and is repeatable.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from specivo_migration_sdk import BundleWriter, timestamp

from specivo_jira_migration.client import Auth, Deployment, JiraClient, RetryPolicy, Sleep
from specivo_jira_migration.fetch import DEFAULT_MAX_ATTACHMENT_SIZE, Fetcher, FetchError, FetchOptions
from specivo_jira_migration.mapping import MapOptions, Records, build_records
from specivo_jira_migration.state import (
    ATTACHMENTS_FILE,
    META_FILE,
    REFS_FILE,
    WATCHERS_FILE,
    StateDir,
    StateError,
    state_path_for,
)

SOURCE_SYSTEM = "jira"
GENERATOR_NAME = "specivo-jira-migration"

type Progress = Callable[[str], None]

# What a directory bundle the writer started (and did not finish) can hold.
_BUNDLE_ENTRIES = frozenset(
    {"lookups.json", "public_access.json", "files"}
    | {
        f"{name}.jsonl"
        for name in (
            "users",
            "groups",
            "projects",
            "memberships",
            "versions",
            "categories",
            "custom_fields",
            "issues",
            "journals",
            "relations",
            "wiki_pages",
            "wiki_redirects",
            "watchers",
            "attachments",
            "time_entries",
        )
    }
)


def generator_version() -> str:
    try:
        return version(GENERATOR_NAME)
    except PackageNotFoundError:  # pragma: no cover - running from an uninstalled checkout
        return "0"


class ExportError(Exception):
    """The export cannot run as asked: a target in the way, a state of another export, an unknown project."""


@dataclass(kw_only=True)
class ExportOptions:
    url: str
    auth: Auth
    projects: Sequence[str]
    out: Path
    zip: bool | None = None
    instance: str | None = None
    limit: int | None = None
    status_category_map: Mapping[str, str] = field(default_factory=dict)
    resolve_keys: bool = True
    keep_state: bool = False
    restart: bool = False
    page_size: int = 100
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    # Attachments larger than this (bytes) are not downloaded; None: no limit.
    max_attachment_size: int | None = DEFAULT_MAX_ATTACHMENT_SIZE


@dataclass
class ExportSummary:
    path: Path
    instance: str
    deployment: Deployment
    counts: dict[str, int]
    not_carried_over: list[tuple[str, int]]
    hidden_emails: int
    conversion_failures: int
    resumed: bool
    state_path: Path | None
    # Attachment bytes written to the bundle, each distinct content once.
    attachment_bytes: int = 0


def _silent(message: str) -> None:
    del message


def normalise_url(url: str) -> str:
    """The site's base URL, without a trailing slash; ``https://`` is assumed when no scheme is given."""
    text = url.strip().rstrip("/")
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ExportError(f"{url!r} is not a Jira site URL (https://example.atlassian.net)")
    if parts.username or parts.password:
        raise ExportError("put credentials in --email/--api-token or --pat, never in the URL")
    return f"{parts.scheme}://{parts.netloc.lower()}{parts.path}"


def instance_for(base_url: str) -> str:
    """The bundle's ``source.instance``: the site's host, and its path for a site under one."""
    parts = urlsplit(base_url)
    return f"{parts.netloc.lower()}{parts.path.rstrip('/')}"


async def export_bundle(
    options: ExportOptions,
    progress: Progress = _silent,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep | None = None,
) -> ExportSummary:
    """Read the projects in *options* from Jira and write their bundle to ``options.out``."""
    url = normalise_url(options.url)
    projects = _project_keys(options.projects)
    out = options.out
    state = StateDir(state_path_for(out))
    if options.restart:
        state.remove()
    _check_target(out, has_state=state.path.exists())

    state_options: dict[str, Any] = {
        "url": url,
        "projects": projects,
        "limit": options.limit,
        "resolve_keys": options.resolve_keys,
    }
    try:
        resumed = state.open(options=state_options, exported_at=timestamp(datetime.now(UTC).replace(microsecond=0)))
    except StateError as exc:
        raise ExportError(str(exc)) from None
    if resumed:
        progress(f"Continuing the export kept in {state.path}")

    async with JiraClient(url, options.auth, page_size=options.page_size, retry=options.retry, sleep=sleep,
                          transport=transport) as client:  # fmt: skip
        try:
            await Fetcher(
                client,
                state,
                FetchOptions(
                    projects=projects,
                    limit=options.limit,
                    resolve_keys=options.resolve_keys,
                    max_attachment_size=options.max_attachment_size,
                ),
                progress,
            ).run()
        except FetchError as exc:
            raise ExportError(str(exc)) from None

    server = state.data["server"]
    site_url = str(server["base_url"]).rstrip("/") or url
    instance = (options.instance or "").strip() or instance_for(site_url)
    deployment = Deployment(server["deployment"])
    records = _records(state, projects, site_url, deployment, options.status_category_map)

    progress(f"Writing {out}")
    with BundleWriter(
        out,
        source_system=SOURCE_SYSTEM,
        source_instance=instance,
        source_version=server.get("version") or None,
        generator_name=GENERATOR_NAME,
        generator_version=generator_version(),
        exported_at=state.data["exported_at"],
        zip=options.zip,
    ) as writer:
        # The bytes come from the state directory; content shared by several attachments is stored once.
        for attachment in records.attachments:
            stored = writer.add_file(state.file_path(attachment.sha256))
            if stored.sha256 != attachment.sha256:
                raise ExportError(f"{state.path}: a downloaded attachment has changed since; pass --restart")
        writer.add_all(records.all())
        for what, count in records.not_carried_over:
            writer.not_carried_over(what, count=count)
        counts = dict(writer.counts)

    if not options.keep_state:
        state.remove()
    return ExportSummary(
        path=out,
        instance=instance,
        deployment=deployment,
        counts=counts,
        not_carried_over=records.not_carried_over,
        hidden_emails=records.hidden_emails,
        conversion_failures=records.conversion_failures,
        resumed=resumed,
        state_path=state.path if options.keep_state else None,
        attachment_bytes=_distinct_bytes(records),
    )


def _records(
    state: StateDir, projects: list[str], site_url: str, deployment: Deployment, status_map: Mapping[str, str]
) -> Records:
    meta = state.read_json(META_FILE) or {}
    refs = state.read_json(REFS_FILE) or {}
    downloads = state.read_json(ATTACHMENTS_FILE) or {}
    watchers = state.read_json(WATCHERS_FILE) or {}
    issues = {key: [state.read_issue(issue_id) for issue_id in state.project(key)["ids"]] for key in projects}
    return build_records(
        meta,
        issues,
        refs,
        MapOptions(
            site_url=site_url,
            deployment=deployment,
            project_keys=projects,
            status_category_map=status_map,
        ),
        downloads,
        watchers,
    )


def _distinct_bytes(records: Records) -> int:
    sizes = {attachment.sha256: attachment.filesize or 0 for attachment in records.attachments}
    return sum(sizes.values())


def _project_keys(keys: Sequence[str]) -> list[str]:
    result: list[str] = []
    for key in keys:
        cleaned = key.strip().upper()
        if not cleaned:
            continue
        if cleaned not in result:
            result.append(cleaned)
    if not result:
        raise ExportError("name at least one project key (--project KEY)")
    return result


def _check_target(out: Path, *, has_state: bool) -> None:
    """Refuse a target that holds anything but an unfinished bundle of this export."""
    if not out.exists():
        return
    if out.is_file() or (out / "manifest.json").exists():
        raise ExportError(f"{out} already holds a bundle; give another --out, or move it away")
    if not out.is_dir():
        raise ExportError(f"{out} is in the way")
    entries = {entry.name for entry in out.iterdir()}
    if not entries:
        return
    if has_state and entries <= _BUNDLE_ENTRIES:
        # A write that failed half way: the bundle is written again from the state.
        shutil.rmtree(out)
        return
    raise ExportError(f"{out} is not empty; give a new or empty directory")
