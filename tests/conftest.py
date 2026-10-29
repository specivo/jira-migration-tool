"""Shared helpers: the fixture sites, an export runner, and a way to read a bundle back."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from specivo_jira_migration.client import CloudAuth, RetryPolicy, TokenAuth
from specivo_jira_migration.export import ExportOptions, ExportSummary, export_bundle
from tests.fake_jira import FakeJira

CLOUD_URL = "https://example.atlassian.net"
DC_URL = "https://jira.example.com"
# Obviously fake credentials; the fake site never checks them.
CLOUD_AUTH = CloudAuth("admin@example.com", "cloud-token-for-tests")
DC_AUTH = TokenAuth("dc-token-for-tests")


@dataclass
class Bundle:
    root: Path
    manifest: dict[str, Any]
    lookups: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    records: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def get(self, stem: str) -> list[dict[str, Any]]:
        return self.records.get(stem, [])

    def by_ref(self, stem: str) -> dict[str, dict[str, Any]]:
        return {record["source_ref"]: record for record in self.get(stem)}

    def not_carried(self) -> dict[str, int]:
        return {item["what"]: item.get("count") for item in self.manifest.get("not_carried_over", [])}


def read_bundle(root: Path) -> Bundle:
    bundle = Bundle(root=root, manifest=json.loads((root / "manifest.json").read_text()))
    if (root / "lookups.json").exists():
        bundle.lookups = json.loads((root / "lookups.json").read_text())
    for path in sorted(root.glob("*.jsonl")):
        bundle.records[path.stem] = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return bundle


class Sleeps:
    """Stands in for asyncio.sleep and remembers each wait."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


@pytest.fixture
def cloud() -> Iterator[FakeJira]:
    fake = FakeJira.load("cloud.json")
    with fake.mock():
        yield fake


@pytest.fixture
def datacenter() -> Iterator[FakeJira]:
    fake = FakeJira.load("datacenter.json")
    with fake.mock():
        yield fake


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def run_export(tmp_path: Path, sleeps: Sleeps):
    """Run an export against whichever fake site is active; return (summary, bundle)."""
    counter = iter(range(1000))

    async def run(
        projects: list[str] | None = None,
        *,
        url: str = CLOUD_URL,
        auth: CloudAuth | TokenAuth = CLOUD_AUTH,
        out: Path | None = None,
        **options: Any,
    ) -> tuple[ExportSummary, Bundle]:
        target = out or tmp_path / f"bundle-{next(counter)}"
        summary = await export_bundle(
            ExportOptions(
                url=url,
                auth=auth,
                projects=projects or ["ACME", "SCRUM"],
                out=target,
                retry=options.pop("retry", RetryPolicy(max_retries=3)),
                **options,
            ),
            sleep=sleeps,
        )
        return summary, read_bundle(target) if not summary.path.suffix == ".zip" else Bundle(target, {})

    return run
