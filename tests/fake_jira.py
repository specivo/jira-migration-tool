"""A Jira site played back from a cassette file, for tests without a network.

A cassette (``tests/fixtures/*.json``) lists interactions: a request to match
and the responses to give, in order (the last one repeats). The files here are
hand-written to the shapes in Atlassian's public REST API reference; recorded
ones (see the README) use the same format, so they can replace them.

Matching: the host (the site's unless the request names a ``host``),
method and path exactly; for every query parameter the request names under
``params``, the request must have that value; for a POST, every key under
``json`` must equal the body's value (``null`` means the key is absent). The
first interaction that matches answers. A request that matches none fails the
test with what it was.

A response gives its body as ``json``, ``text`` or ``base64`` (bytes, for
attachment content), and may give ``headers`` (``Location`` for a redirect).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import respx

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@dataclass
class Interaction:
    method: str
    path: str
    params: dict[str, str]
    body: dict[str, Any] | None
    responses: list[dict[str, Any]]
    host: str | None = None
    served: int = 0
    # An override answers only with its own responses, once each, then steps aside.
    once: bool = False

    def matches(self, request: httpx.Request, site_host: str | None = None) -> bool:
        if self.once and self.served >= len(self.responses):
            return False
        if request.url.host != (self.host or site_host or request.url.host):
            return False
        if request.method != self.method or request.url.path != self.path:
            return False
        query = request.url.params
        if any(query.get(name) != value for name, value in self.params.items()):
            return False
        if self.body is not None:
            sent = json.loads(request.content or b"{}")
            for name, value in self.body.items():
                if value is None and name in sent:
                    return False
                if value is not None and sent.get(name) != value:
                    return False
        return True

    def respond(self) -> httpx.Response:
        spec = self.responses[min(self.served, len(self.responses) - 1)]
        self.served += 1
        headers = dict(spec.get("headers") or {})
        if "json" in spec:
            return httpx.Response(spec["status"], json=spec["json"], headers=headers)
        if "base64" in spec:
            return httpx.Response(spec["status"], content=base64.b64decode(spec["base64"]), headers=headers)
        return httpx.Response(spec["status"], text=spec.get("text", ""), headers=headers)


@dataclass
class FakeJira:
    site: str
    interactions: list[Interaction]
    requests: list[httpx.Request] = field(default_factory=list)

    @classmethod
    def load(cls, name: str) -> FakeJira:
        data = json.loads((FIXTURES / name).read_text())
        interactions = [
            Interaction(
                method=item["request"]["method"],
                path=item["request"]["path"],
                params={k: str(v) for k, v in (item["request"].get("params") or {}).items()},
                body=item["request"].get("json"),
                responses=item["responses"],
                host=item["request"].get("host"),
            )
            for item in data["interactions"]
        ]
        return cls(site=data["site"], interactions=interactions)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        site_host = urlsplit(self.site).hostname
        for interaction in self.interactions:
            if interaction.matches(request, site_host):
                return interaction.respond()
        body = request.content.decode() if request.content else ""
        raise AssertionError(f"no fixture for {request.method} {request.url} {body}")

    def calls(self, method: str, path: str, *, host: str | None = None) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if r.method == method and r.url.path == path and (host is None or r.url.host == host)
        ]

    def override(self, method: str, path: str, *responses: dict[str, Any], host: str | None = None) -> None:
        """Answer the next requests to *method* *path* with *responses*, then go back to the cassette."""
        self.interactions.insert(0, Interaction(method, path, {}, None, list(responses), host=host, once=True))

    def hosts(self) -> set[str]:
        """The site's host and every other host the cassette answers for (a media host, say)."""
        site = urlsplit(self.site).hostname or ""
        return {site} | {i.host for i in self.interactions if i.host}

    @contextmanager
    def mock(self) -> Iterator[FakeJira]:
        with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
            for host in sorted(self.hosts()):
                router.route(host=host).mock(side_effect=self)
            yield self


async def no_sleep(seconds: float) -> None:
    del seconds
