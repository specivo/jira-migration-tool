"""A small async client for the parts of Jira's public REST API the export reads.

One client serves both dialects:

- **Cloud** (``*.atlassian.net``): email address and API token, HTTP Basic.
  Issues are listed with ``POST /rest/api/3/search/jql``, paged by
  ``nextPageToken``.
- **Data Center** (and Server): a personal access token, ``Authorization:
  Bearer``. Issues are listed with ``GET /rest/api/2/search``, paged by
  ``startAt``.

Issues, comments and change history are read through REST v3 on Cloud (text
as Atlassian Document Format) and REST v2 on Data Center (text as wiki
markup); see :mod:`.fetch`. The site-wide lists are read through v2 on both.

Jira Cloud limits request rates and answers 429 (sometimes 503) when a client
goes too fast, not always with a ``Retry-After``. Every request is retried on
those with exponential backoff and full jitter, waiting at least what
``Retry-After`` asks for, up to a fixed number of attempts.

Credentials are held only in the ``httpx`` client and never logged: log lines
and errors name the method, the path and the status, nothing more.

Attachments are downloaded with :meth:`JiraClient.download`, which follows
redirects itself, one hop at a time. A hop to the configured site (same
scheme, host and port) goes through the signed-in client; a hop anywhere else
(Jira Cloud answers ``303`` to a media host, with a short-lived token in the
URL) goes through a second client that holds no credentials and no cookies,
so the ``Authorization`` header never leaves the site. A redirect from
``https`` to plain ``http`` is refused. The JSON requests follow redirects
through ``httpx``, which drops ``Authorization`` on a redirect to another
origin as well.
"""

from __future__ import annotations

import asyncio
import email.utils
import hashlib
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, BinaryIO

import httpx

logger = logging.getLogger(__name__)

# Statuses Jira uses to say "slow down" or "try again".
RETRY_STATUSES = frozenset({429, 503})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
DEFAULT_PAGE_SIZE = 100
# Redirects a download follows before it gives up.
MAX_REDIRECTS = 5
_DEFAULT_PORTS = {"http": 80, "https": 443}

type Sleep = Callable[[float], Awaitable[None]]
type JSON = dict[str, Any]


class Deployment(StrEnum):
    CLOUD = "cloud"
    DATA_CENTER = "data_center"


class JiraError(Exception):
    """A request Jira refused or that could not be made. The message never holds credentials."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class CloudAuth:
    """Jira Cloud: the account's email address and an API token."""

    email: str
    token: str

    def __repr__(self) -> str:
        return "CloudAuth(email=***, token=***)"


@dataclass(frozen=True)
class TokenAuth:
    """Jira Data Center: a personal access token."""

    token: str

    def __repr__(self) -> str:
        return "TokenAuth(token=***)"


type Auth = CloudAuth | TokenAuth


class DownloadTooLargeError(Exception):
    """A download that went over its size limit; nothing of it is kept.

    ``location`` is the first redirect the download was answered with, if any.
    """

    def __init__(self, limit: int, location: str | None) -> None:
        super().__init__(f"larger than {limit} bytes")
        self.limit = limit
        self.location = location


@dataclass(frozen=True)
class Download:
    """What :meth:`JiraClient.download` wrote: the bytes' SHA-256 and size, and the first redirect."""

    sha256: str
    size: int
    location: str | None


@dataclass(frozen=True)
class ServerInfo:
    base_url: str
    version: str | None
    deployment: Deployment
    server_title: str | None = None


@dataclass(frozen=True)
class RetryPolicy:
    """How often and how long to wait when Jira answers 429 or 503."""

    max_retries: int = 6
    base_delay: float = 1.0
    max_delay: float = 60.0
    # A Retry-After above this is still honoured, but capped here.
    max_retry_after: float = 600.0

    def backoff(self, attempt: int, rand: Callable[[], float]) -> float:
        """Full jitter: a random wait between 0 and ``base * 2**attempt`` (capped)."""
        ceiling = min(self.max_delay, self.base_delay * float(1 << min(attempt, 30)))
        return rand() * ceiling


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header: delta-seconds or an HTTP date. None when absent or bad."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


class JiraClient:
    """Async access to one Jira site. Use as an async context manager."""

    def __init__(
        self,
        base_url: str,
        auth: Auth,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        retry: RetryPolicy | None = None,
        sleep: Sleep | None = None,
        rand: Callable[[], float] = random.random,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self.retry = retry or RetryPolicy()
        self._sleep = sleep
        self._rand = rand
        headers = {"Accept": "application/json", "User-Agent": "specivo-jira-migration"}
        basic: httpx.BasicAuth | None = None
        if isinstance(auth, CloudAuth):
            basic = httpx.BasicAuth(auth.email, auth.token)
        else:
            headers["Authorization"] = f"Bearer {auth.token}"
        self.auth_kind = "cloud" if isinstance(auth, CloudAuth) else "token"
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            auth=basic,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        # For redirect hops away from the site: no credentials, and cookies are cleared before each hop.
        self._bare = httpx.AsyncClient(
            headers={"User-Agent": headers["User-Agent"]},
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )
        self._origin = _origin(httpx.URL(self.base_url))
        # Waits taken, for the summary: (status, seconds).
        self.waits: list[tuple[int | None, float]] = []

    async def __aenter__(self) -> JiraClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()
        await self._bare.aclose()

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: JSON | None = None,
        allow_404: bool = False,
    ) -> Any:
        """Send one request, retrying on 429/503 and network errors; return the decoded JSON body.

        With *allow_404*, a 404 returns ``None`` instead of raising.
        """

        async def send() -> httpx.Response:
            return await self._http.request(method, path, params=params, json=json)

        response = await self._retrying(f"{method} {path}", send)
        return self._decode(method, path, response, allow_404=allow_404)

    async def _retrying(self, label: str, send: Callable[[], Awaitable[httpx.Response]]) -> httpx.Response:
        """Call *send* until it answers with something other than 429/503 or a network error, within the policy."""
        attempt = 0
        while True:
            status: int | None
            retry_after: float | None = None
            try:
                response = await send()
            except httpx.TransportError as exc:
                status = None
                reason = type(exc).__name__
            else:
                status = response.status_code
                if status not in RETRY_STATUSES:
                    return response
                reason = f"HTTP {status}"
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                await response.aclose()

            if attempt >= self.retry.max_retries:
                raise JiraError(f"{label}: gave up after {attempt + 1} attempts ({reason})", status=status)
            delay = self.retry.backoff(attempt, self._rand)
            if retry_after is not None:
                # Never sooner than Jira asked; jitter on top so parallel clients spread out.
                delay = min(retry_after, self.retry.max_retry_after) + self._rand()
            attempt += 1
            logger.warning(
                "%s: %s, waiting %.1f s before attempt %d of %d",
                label,
                reason,
                delay,
                attempt + 1,
                self.retry.max_retries + 1,
            )
            self.waits.append((status, delay))
            await (self._sleep or asyncio.sleep)(delay)

    def _decode(self, method: str, path: str, response: httpx.Response, *, allow_404: bool) -> Any:
        status = response.status_code
        if status == 404 and allow_404:
            return None
        if status in (401, 403):
            raise JiraError(
                f"{method} {path}: HTTP {status}; check the credentials and that the account can read this",
                status=status,
            )
        if status >= 400:
            raise JiraError(f"{method} {path}: HTTP {status}{_error_text(response)}", status=status)
        if status == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise JiraError(f"{method} {path}: the response is not JSON", status=status) from None

    async def get(self, path: str, params: dict[str, Any] | None = None, *, allow_404: bool = False) -> Any:
        return await self.request("GET", path, params=params, allow_404=allow_404)

    async def post(self, path: str, body: JSON) -> Any:
        return await self.request("POST", path, json=body)

    # ------------------------------------------------------------------
    # Downloads
    # ------------------------------------------------------------------

    def _absolute(self, url: str) -> httpx.URL:
        """*url* as an absolute URL: a path is taken under the site's base URL (its context path kept)."""
        target = httpx.URL(url)
        if not target.is_relative_url:
            return target
        return httpx.URL(self.base_url + "/" + url.lstrip("/"))

    def trusted(self, url: httpx.URL | str) -> bool:
        """Whether *url* is on the configured site (same scheme, host and port), the only place credentials go."""
        target = httpx.URL(url)
        if target.is_relative_url:
            return True
        return _origin(target) == self._origin

    async def _hop(self, url: httpx.URL) -> httpx.Response:
        """Send one GET for *url*, without following redirects; signed in only when *url* is on the site."""
        if self.trusted(url):
            http = self._http
        else:
            http = self._bare
            http.cookies.clear()

        async def send() -> httpx.Response:
            return await http.send(http.build_request("GET", url), stream=True, follow_redirects=False)

        # The label names the path only: a media host's URL carries a token in its query.
        return await self._retrying(f"GET {url.path}", send)

    def _next(self, current: httpx.URL, response: httpx.Response) -> httpx.URL:
        location = response.headers.get("Location")
        if not location:
            raise JiraError(f"GET {current.path}: HTTP {response.status_code} without a Location", status=None)
        target = current.join(location)
        if target.scheme not in ("http", "https") or (current.scheme == "https" and target.scheme != "https"):
            raise JiraError(f"GET {current.path}: refused a redirect to {target.scheme}:", status=None)
        return target

    async def locate(self, url: str) -> str | None:
        """The ``Location`` Jira answers a GET of *url* with, without following it; None when it answers directly.

        A 4xx or 5xx answer raises :class:`JiraError` with its status.
        """
        current = self._absolute(url)
        response = await self._hop(current)
        try:
            if response.status_code in REDIRECT_STATUSES:
                return str(self._next(current, response))
            if response.status_code >= 400:
                raise JiraError(f"GET {current.path}: HTTP {response.status_code}", status=response.status_code)
            return None
        finally:
            await response.aclose()

    async def download(self, url: str, target: BinaryIO, *, max_bytes: int | None = None) -> Download:
        """Stream the body of *url* into *target*, hashing it as it is written; follow redirects hop by hop.

        Credentials go only to the site (see :meth:`trusted`). Raises
        :class:`DownloadTooLargeError` when the body is over *max_bytes* (by its
        ``Content-Length`` or as it arrives), and :class:`JiraError` with the
        status on a 4xx or 5xx answer. What was written to *target* before an
        error is the caller's to discard.
        """
        current = self._absolute(url)
        first_location: str | None = None
        for _ in range(MAX_REDIRECTS + 1):
            response = await self._hop(current)
            try:
                status = response.status_code
                if status in REDIRECT_STATUSES:
                    current = self._next(current, response)
                    first_location = first_location or str(current)
                    continue
                if status >= 400:
                    raise JiraError(f"GET {current.path}: HTTP {status}", status=status)
                declared = response.headers.get("Content-Length")
                if max_bytes is not None and declared and declared.isdigit() and int(declared) > max_bytes:
                    raise DownloadTooLargeError(max_bytes, first_location)
                digest = hashlib.sha256()
                size = 0
                try:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if max_bytes is not None and size > max_bytes:
                            raise DownloadTooLargeError(max_bytes, first_location)
                        digest.update(chunk)
                        target.write(chunk)
                except httpx.TransportError as exc:
                    raise JiraError(f"GET {current.path}: the download broke off ({type(exc).__name__})") from None
                return Download(sha256=digest.hexdigest(), size=size, location=first_location)
            finally:
                await response.aclose()
        raise JiraError(f"GET {current.path}: more than {MAX_REDIRECTS} redirects")

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    async def server_info(self) -> ServerInfo:
        """Read ``serverInfo``: the version, and whether this is Cloud or Data Center."""
        data = await self.get("/rest/api/2/serverInfo")
        if not isinstance(data, dict):
            raise JiraError("GET /rest/api/2/serverInfo: unexpected response")
        kind = str(data.get("deploymentType") or "")
        deployment = Deployment.CLOUD if kind.lower() == "cloud" else Deployment.DATA_CENTER
        return ServerInfo(
            base_url=str(data.get("baseUrl") or self.base_url),
            version=data.get("version"),
            deployment=deployment,
            server_title=data.get("serverTitle"),
        )

    async def search_cloud(
        self, jql: str, *, next_page_token: str | None = None
    ) -> AsyncIterator[tuple[list[JSON], str | None]]:
        """Yield ``(issues, token for the page after)`` from Cloud's enhanced search.

        Stops on ``isLast``, on an empty page, on a missing or repeated token,
        and on a page that holds no issue not seen before: Cloud has been known
        to keep ``isLast`` false forever. Each issue holds only its id and key.
        """
        seen_tokens: set[str] = set()
        seen_ids: set[str] = set()
        token = next_page_token
        while True:
            body: JSON = {"jql": jql, "fields": ["key"], "maxResults": self.page_size}
            if token:
                body["nextPageToken"] = token
                seen_tokens.add(token)
            data = await self.post("/rest/api/3/search/jql", body) or {}
            issues = [issue for issue in data.get("issues") or [] if isinstance(issue, dict)]
            if not issues:
                return
            fresh = [issue for issue in issues if str(issue.get("id")) not in seen_ids]
            seen_ids.update(str(issue.get("id")) for issue in issues)
            token = data.get("nextPageToken") or None
            last = bool(data.get("isLast")) or token is None or token in seen_tokens or not fresh
            if token in seen_tokens:
                logger.warning("Search returned a page token it had returned before; treating the list as complete")
            elif not fresh:
                logger.warning("Search returned only issues it had returned before; treating the list as complete")
            if fresh:
                yield fresh, None if last else token
            if last:
                return

    async def search_data_center(self, jql: str, *, start_at: int = 0) -> AsyncIterator[tuple[list[JSON], int | None]]:
        """Yield ``(issues, startAt of the page after)`` from Data Center's ``/rest/api/2/search``."""
        while True:
            params = {"jql": jql, "fields": "key", "startAt": start_at, "maxResults": self.page_size}
            data = await self.get("/rest/api/2/search", params) or {}
            issues = [issue for issue in data.get("issues") or [] if isinstance(issue, dict)]
            if not issues:
                return
            start_at += len(issues)
            total = data.get("total")
            last = isinstance(total, int) and start_at >= total
            yield issues, None if last else start_at
            if last:
                return

    async def paged_values(self, path: str, key: str, *, params: dict[str, Any] | None = None) -> list[JSON]:
        """Read every item of a ``startAt``/``maxResults`` paged list (comments, changelog) at *path*."""
        values: list[JSON] = []
        start_at = 0
        while True:
            page_params = {**(params or {}), "startAt": start_at, "maxResults": self.page_size}
            data = await self.get(path, page_params) or {}
            items = [item for item in data.get(key) or [] if isinstance(item, dict)]
            if not items:
                return values
            values.extend(items)
            start_at += len(items)
            total = data.get("total")
            if data.get("isLast") is True or (isinstance(total, int) and start_at >= total):
                return values


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    scheme = url.scheme.lower()
    return scheme, url.host.lower(), url.port or _DEFAULT_PORTS.get(scheme)


def _error_text(response: httpx.Response) -> str:
    """Jira's own error messages, if the body has them; never the request."""
    try:
        data = response.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    messages = [str(m) for m in data.get("errorMessages") or []]
    errors = data.get("errors")
    if isinstance(errors, dict):
        messages += [f"{k}: {v}" for k, v in errors.items()]
    return f": {'; '.join(messages)[:500]}" if messages else ""
