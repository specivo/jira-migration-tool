"""The ``specivo-jira-migration`` command.

Reads projects from Jira Cloud or Jira Data Center through the REST API and
writes a Specivo import bundle: a directory by default, or a ``.zip`` with
``--zip``.

Credentials come from options or, better, the environment (``JIRA_EMAIL`` and
``JIRA_API_TOKEN`` for Cloud, ``JIRA_PAT`` for Data Center), so they stay out
of the process list and shell history. They are never written anywhere.

Exit status: 0 when the bundle was written (and, with ``--validate``, is
valid); 1 when the export failed or the bundle is not valid; 2 for a usage
error.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from specivo_migration_sdk import validate_bundle
from specivo_migration_sdk.limits import format_size, parse_size

from specivo_jira_migration.client import Auth, CloudAuth, JiraError, TokenAuth
from specivo_jira_migration.export import ExportError, ExportOptions, export_bundle, generator_version
from specivo_jira_migration.fetch import DEFAULT_MAX_ATTACHMENT_SIZE

URL_ENV = "JIRA_URL"
EMAIL_ENV = "JIRA_EMAIL"
TOKEN_ENV = "JIRA_API_TOKEN"
PAT_ENV = "JIRA_PAT"
PROJECTS_ENV = "JIRA_PROJECTS"
_STATUS_CATEGORIES = ("backlog", "active", "done", "closed")


def _status_map(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in value.split(","):
        if not item.strip():
            continue
        name, sep, category = item.partition("=")
        if not sep or not name.strip() or not category.strip():
            raise argparse.ArgumentTypeError(f"expected NAME=CATEGORY pairs, got {item!r}")
        if category.strip() not in _STATUS_CATEGORIES:
            raise argparse.ArgumentTypeError(f"{name}: {category!r} is not one of {', '.join(_STATUS_CATEGORIES)}")
        result[name.strip()] = category.strip()
    return result


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def _size(value: str) -> int | None:
    """A size such as ``100M`` or ``2G``; ``0`` means no limit."""
    try:
        size = parse_size(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return size or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="specivo-jira-migration",
        description=(
            "Export Jira projects (Cloud or Data Center) to a Specivo import bundle. "
            "Load the bundle with: python -m specivo.cli.import_data --source bundle --path OUT"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {generator_version()}")
    source = parser.add_argument_group("source")
    source.add_argument("--url", help=f"The Jira site, e.g. https://example.atlassian.net (default: ${URL_ENV}).")
    source.add_argument("--email", help=f"Jira Cloud: the account's email address (default: ${EMAIL_ENV}).")
    source.add_argument(
        "--api-token",
        help=f"Jira Cloud: an API token (default: ${TOKEN_ENV}; prefer the variable, kept out of the process list).",
    )
    source.add_argument(
        "--pat",
        help=f"Jira Data Center: a personal access token (default: ${PAT_ENV}; prefer the variable).",
    )
    source.add_argument(
        "--project",
        action="append",
        default=[],
        metavar="KEY",
        help=f"A project to export, by key. Repeatable, or comma-separated (default: ${PROJECTS_ENV}).",
    )
    source.add_argument(
        "--instance",
        help="Name of this Jira site in the bundle (default: its host name). Use the same name on every export.",
    )

    output = parser.add_argument_group("output")
    output.add_argument("--out", type=Path, required=True, help="Where to write the bundle: a new or empty directory.")
    output.add_argument("--zip", action="store_true", help="Write a .zip instead of a directory.")
    output.add_argument("--validate", action="store_true", help="Check the bundle once written, as Specivo will.")
    output.add_argument("--quiet", action="store_true", help="Print nothing but errors.")

    mapping = parser.add_argument_group("mapping")
    mapping.add_argument(
        "--status-category-map",
        type=_status_map,
        default={},
        metavar="NAME=CATEGORY,...",
        help="Place statuses in Specivo's backlog/active/done/closed grouping (default: from Jira's status category).",
    )
    mapping.add_argument(
        "--max-attachment-size",
        type=_size,
        default=DEFAULT_MAX_ATTACHMENT_SIZE,
        metavar="SIZE",
        help=(
            f"Leave out attachments larger than this, e.g. 500M or 2G; 0 for no limit "
            f"(default: {format_size(DEFAULT_MAX_ATTACHMENT_SIZE)}). They are counted in the manifest."
        ),
    )
    mapping.add_argument(
        "--no-resolve-keys",
        action="store_true",
        help="Do not ask Jira about issue keys in text that are not in the bundle; they stay plain text.",
    )

    run = parser.add_argument_group("run")
    run.add_argument("--limit", type=_positive, help="Export at most this many issues per project, for a trial run.")
    run.add_argument("--page-size", type=_positive, default=100, help="Items per page when listing (default: 100).")
    run.add_argument(
        "--keep-state",
        action="store_true",
        help="Keep the state directory (OUT.jira-state) after a successful run; it holds what Jira returned.",
    )
    run.add_argument("--restart", action="store_true", help="Discard the state of an earlier, unfinished run.")
    return parser


def _auth(args: argparse.Namespace) -> Auth | str:
    """Credentials from the options, else the environment; a message when they are missing or mixed."""
    if args.pat and (args.email or args.api_token):
        return "give either --email and --api-token (Cloud) or --pat (Data Center), not both"
    if args.pat:
        return TokenAuth(args.pat)
    email = args.email or os.environ.get(EMAIL_ENV)
    token = args.api_token or os.environ.get(TOKEN_ENV)
    pat = os.environ.get(PAT_ENV)
    if args.email or args.api_token or not pat:
        if email and token:
            return CloudAuth(email, token)
        return (
            f"give --email and --api-token (or {EMAIL_ENV} and {TOKEN_ENV}) for Jira Cloud, "
            f"or --pat (or {PAT_ENV}) for Jira Data Center"
        )
    if email and token:
        return f"{EMAIL_ENV}/{TOKEN_ENV} and {PAT_ENV} are all set: unset the ones for the other kind of site"
    return TokenAuth(pat)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    def usage(message: str) -> int:
        parser.print_usage(sys.stderr)
        print(f"{parser.prog}: error: {message}", file=sys.stderr)
        return 2

    url = args.url or os.environ.get(URL_ENV)
    if not url:
        return usage(f"give --url, or set {URL_ENV}")
    auth = _auth(args)
    if isinstance(auth, str):
        return usage(auth)
    projects = [key for value in (args.project or [os.environ.get(PROJECTS_ENV, "")]) for key in value.split(",")]
    if not any(key.strip() for key in projects):
        return usage(f"give --project KEY (repeatable), or set {PROJECTS_ENV}")

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    def progress(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr, flush=True)

    options = ExportOptions(
        url=url,
        auth=auth,
        projects=projects,
        out=args.out,
        zip=True if args.zip else None,
        instance=args.instance,
        limit=args.limit,
        status_category_map=args.status_category_map,
        resolve_keys=not args.no_resolve_keys,
        keep_state=args.keep_state,
        restart=args.restart,
        page_size=args.page_size,
        max_attachment_size=args.max_attachment_size,
    )
    try:
        summary = asyncio.run(export_bundle(options, progress))
    except ExportError as exc:
        print(f"{parser.prog}: error: {exc}", file=sys.stderr)
        return 2
    except JiraError as exc:
        print(f"{parser.prog}: export failed: {exc}", file=sys.stderr)
        print("Run the same command again to continue where it stopped.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"{parser.prog}: interrupted; run the same command again to continue", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - one line, never a traceback that could hold a header
        print(f"{parser.prog}: export failed: {type(exc).__name__}: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(summary.path)
        counts = ", ".join(f"{key} {value}" for key, value in summary.counts.items() if value)
        progress(f"Records: {counts}")
        if summary.counts.get("attachments"):
            megabytes = summary.attachment_bytes / (1024 * 1024)
            progress(f"Attachment files: {megabytes:.1f} MB (each distinct content stored once)")
        if summary.hidden_emails:
            progress(
                f"Users without a visible email address: {summary.hidden_emails} "
                "(Specivo gives each a placeholder address and lists them in its import report)"
            )
        if summary.not_carried_over:
            progress(f"Not carried over: {len(summary.not_carried_over)} kinds of data, listed in the manifest")
        if summary.conversion_failures:
            progress(f"Markup that could not be converted, kept as written: {summary.conversion_failures} texts")
        if summary.state_path:
            progress(f"State kept in {summary.state_path} (it holds what Jira returned; delete it when done)")

    if args.validate:
        report = validate_bundle(summary.path)
        print(report.format_text())
        if not report.ok:
            return 1
        print("The bundle is valid.")
    return 0
