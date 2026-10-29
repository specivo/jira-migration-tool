"""Write a bundle from the hand-written Cloud fixture site: ``python -m tests.write_fixture_bundle OUT [KEYS]``.

*KEYS* are the projects, comma-separated (default ``ACME,SCRUM``); the site
also has ``OPS``, so the three can be written as one bundle or one each.

Runs the command itself against the fake site (no network, no waits), so a
test outside this project (Specivo's end-to-end test, which loads the bundle
into a database) can get a bundle without depending on this project's code.
Prints the bundle's path. Exit status as the command's.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest import mock

from specivo_jira_migration.cli import main as cli_main
from tests.fake_jira import FakeJira, no_sleep


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2):
        print("usage: python -m tests.write_fixture_bundle OUT [KEYS]", file=sys.stderr)
        return 2
    projects = argv[1] if len(argv) == 2 else "ACME,SCRUM"
    fake = FakeJira.load("cloud.json")
    env = {"JIRA_EMAIL": "admin@example.com", "JIRA_API_TOKEN": "cloud-token-for-tests"}
    with fake.mock(), mock.patch.dict(os.environ, env), mock.patch.object(asyncio, "sleep", no_sleep):
        return cli_main(["--url", fake.site, "--project", projects, "--out", argv[0], "--quiet", "--validate"])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
