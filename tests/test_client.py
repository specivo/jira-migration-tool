"""The HTTP client: authentication, retries on rate limits, and both kinds of paging."""

from __future__ import annotations

import base64
import hashlib
import io
import logging
from datetime import UTC, datetime

import httpx
import pytest
import respx

from specivo_jira_migration.client import (
    CloudAuth,
    Deployment,
    DownloadTooLargeError,
    JiraClient,
    JiraError,
    RetryPolicy,
    TokenAuth,
    parse_retry_after,
)
from tests.conftest import CLOUD_URL, DC_URL, Sleeps

SEARCH = f"{CLOUD_URL}/rest/api/3/search/jql"


def _client(sleeps: Sleeps, *, url: str = CLOUD_URL, auth=None, rand=lambda: 0.5, **kw) -> JiraClient:
    return JiraClient(url, auth or CloudAuth("admin@example.com", "secret-token"), sleep=sleeps, rand=rand, **kw)


def _page(ids, token=None, is_last=False):
    body = {"issues": [{"id": str(i), "key": f"ACME-{i}"} for i in ids], "isLast": is_last}
    if token is not None:
        body["nextPageToken"] = token
    return httpx.Response(200, json=body)


class TestRetryAfter:
    def test_seconds(self):
        assert parse_retry_after("7") == 7.0

    def test_http_date(self):
        now = datetime(2024, 3, 1, 9, 0, 0, tzinfo=UTC)
        assert parse_retry_after("Fri, 01 Mar 2024 09:00:30 GMT", now=now) == 30.0

    @pytest.mark.parametrize("value", [None, "", "soon", "-"])
    def test_absent_or_unreadable(self, value):
        assert parse_retry_after(value) is None

    def test_a_date_in_the_past_is_no_wait(self):
        now = datetime(2024, 3, 1, 9, 0, 0, tzinfo=UTC)
        assert parse_retry_after("Fri, 01 Mar 2024 08:00:00 GMT", now=now) == 0.0


class TestAuthentication:
    @respx.mock
    async def test_cloud_uses_basic_auth(self, sleeps):
        route = respx.get(f"{CLOUD_URL}/rest/api/2/serverInfo").mock(
            return_value=httpx.Response(200, json={"deploymentType": "Cloud", "version": "1001.0.0"})
        )
        async with _client(sleeps) as client:
            info = await client.server_info()
        assert info.deployment is Deployment.CLOUD
        expected = base64.b64encode(b"admin@example.com:secret-token").decode()
        assert route.calls.last.request.headers["Authorization"] == f"Basic {expected}"

    @respx.mock
    async def test_data_center_uses_a_bearer_token(self, sleeps):
        route = respx.get(f"{DC_URL}/rest/api/2/serverInfo").mock(
            return_value=httpx.Response(200, json={"deploymentType": "Server", "version": "9.12.4"})
        )
        async with _client(sleeps, url=DC_URL, auth=TokenAuth("pat-value")) as client:
            info = await client.server_info()
        assert info.deployment is Deployment.DATA_CENTER
        assert info.version == "9.12.4"
        assert route.calls.last.request.headers["Authorization"] == "Bearer pat-value"

    def test_credentials_are_not_in_the_repr(self):
        assert "secret" not in repr(CloudAuth("admin@example.com", "secret"))
        assert "secret" not in repr(TokenAuth("secret"))

    @respx.mock
    async def test_a_refusal_names_no_credentials(self, sleeps):
        respx.get(f"{CLOUD_URL}/rest/api/2/serverInfo").mock(return_value=httpx.Response(401))
        async with _client(sleeps) as client:
            with pytest.raises(JiraError) as caught:
                await client.server_info()
        assert caught.value.status == 401
        assert "secret-token" not in str(caught.value)
        assert "admin@example.com" not in str(caught.value)


class TestRateLimits:
    @respx.mock
    async def test_429_with_retry_after_waits_at_least_that_long(self, sleeps, caplog):
        respx.get(f"{CLOUD_URL}/rest/api/2/field").mock(
            side_effect=[httpx.Response(429, headers={"Retry-After": "5"}), httpx.Response(200, json=[])]
        )
        caplog.set_level(logging.WARNING)
        async with _client(sleeps, rand=lambda: 0.25) as client:
            assert await client.get("/rest/api/2/field") == []
        assert sleeps.waits == [5.25]
        assert "HTTP 429, waiting 5.2 s" in caplog.text
        assert "secret-token" not in caplog.text

    @respx.mock
    async def test_429_without_retry_after_backs_off_exponentially_with_jitter(self, sleeps):
        respx.get(f"{CLOUD_URL}/rest/api/2/field").mock(
            side_effect=[httpx.Response(429), httpx.Response(429), httpx.Response(503), httpx.Response(200, json=[])]
        )
        policy = RetryPolicy(max_retries=5, base_delay=2.0, max_delay=60.0)
        async with _client(sleeps, rand=lambda: 0.5, retry=policy) as client:
            await client.get("/rest/api/2/field")
        # Full jitter: rand() * min(max_delay, base * 2**attempt).
        assert sleeps.waits == [1.0, 2.0, 4.0]

    def test_backoff_is_capped(self):
        policy = RetryPolicy(base_delay=1.0, max_delay=10.0)
        assert policy.backoff(20, lambda: 1.0) == 10.0

    @respx.mock
    async def test_a_huge_retry_after_is_capped(self, sleeps):
        respx.get(f"{CLOUD_URL}/rest/api/2/field").mock(
            side_effect=[httpx.Response(429, headers={"Retry-After": "86400"}), httpx.Response(200, json=[])]
        )
        async with _client(sleeps, rand=lambda: 0.0, retry=RetryPolicy(max_retry_after=120)) as client:
            await client.get("/rest/api/2/field")
        assert sleeps.waits == [120.0]

    @respx.mock
    async def test_gives_up_after_the_retries(self, sleeps):
        respx.get(f"{CLOUD_URL}/rest/api/2/field").mock(return_value=httpx.Response(429))
        async with _client(sleeps, retry=RetryPolicy(max_retries=2)) as client:
            with pytest.raises(JiraError, match="gave up after 3 attempts"):
                await client.get("/rest/api/2/field")
        assert len(sleeps.waits) == 2

    @respx.mock
    async def test_network_errors_are_retried(self, sleeps):
        respx.get(f"{CLOUD_URL}/rest/api/2/field").mock(
            side_effect=[httpx.ConnectError("refused"), httpx.Response(200, json=[{"id": "summary"}])]
        )
        async with _client(sleeps) as client:
            assert await client.get("/rest/api/2/field") == [{"id": "summary"}]
        assert len(sleeps.waits) == 1

    @respx.mock
    async def test_other_errors_are_not_retried(self, sleeps):
        respx.get(f"{CLOUD_URL}/rest/api/2/field").mock(
            return_value=httpx.Response(400, json={"errorMessages": ["The JQL is not valid."], "errors": {}})
        )
        async with _client(sleeps) as client:
            with pytest.raises(JiraError, match="HTTP 400: The JQL is not valid."):
                await client.get("/rest/api/2/field")
        assert sleeps.waits == []


class TestCloudSearch:
    async def _ids(self, client: JiraClient, token=None) -> list[list[str]]:
        return [
            [i["id"] for i in issues]
            async for issues, _ in client.search_cloud("project = ACME", next_page_token=token)
        ]

    @respx.mock
    async def test_stops_on_an_empty_page_when_islast_never_turns_true(self, sleeps):
        route = respx.post(SEARCH).mock(side_effect=[_page([1, 2], "t2"), _page([3], "t3"), _page([], "t4")])
        async with _client(sleeps) as client:
            assert await self._ids(client) == [["1", "2"], ["3"]]
        assert route.call_count == 3

    @respx.mock
    async def test_stops_on_a_repeated_token(self, sleeps):
        route = respx.post(SEARCH).mock(side_effect=[_page([1], "t2"), _page([2], "t2"), _page([3], "t9")])
        async with _client(sleeps) as client:
            assert await self._ids(client) == [["1"], ["2"]]
        assert route.call_count == 2

    @respx.mock
    async def test_stops_on_a_page_of_issues_seen_before(self, sleeps):
        route = respx.post(SEARCH).mock(side_effect=[_page([1, 2], "t2"), _page([1, 2], "t3"), _page([3], "t4")])
        async with _client(sleeps) as client:
            assert await self._ids(client) == [["1", "2"]]
        assert route.call_count == 2

    @respx.mock
    async def test_stops_on_islast(self, sleeps):
        route = respx.post(SEARCH).mock(side_effect=[_page([1], "t2", is_last=True)])
        async with _client(sleeps) as client:
            assert await self._ids(client) == [["1"]]
        assert route.call_count == 1

    @respx.mock
    async def test_sends_the_token_and_an_explicit_field_list(self, sleeps):
        route = respx.post(SEARCH).mock(side_effect=[_page([1], "t2"), _page([], None)])
        async with _client(sleeps, page_size=50) as client:
            pages = [token async for _, token in client.search_cloud("project = ACME", next_page_token="t1")]
        import json

        first, second = (json.loads(call.request.content) for call in route.calls)
        assert first == {"jql": "project = ACME", "fields": ["key"], "maxResults": 50, "nextPageToken": "t1"}
        assert second["nextPageToken"] == "t2"
        assert pages == ["t2"]


class TestDataCenterSearch:
    @respx.mock
    async def test_pages_with_start_at_until_total(self, sleeps):
        route = respx.get(f"{DC_URL}/rest/api/2/search").mock(
            side_effect=[
                httpx.Response(200, json={"startAt": 0, "total": 3, "issues": [{"id": "1"}, {"id": "2"}]}),
                httpx.Response(200, json={"startAt": 2, "total": 3, "issues": [{"id": "3"}]}),
            ]
        )
        async with _client(sleeps, url=DC_URL, auth=TokenAuth("t")) as client:
            pages = [(len(i), nxt) async for i, nxt in client.search_data_center("project = SUP")]
        assert pages == [(2, 2), (1, None)]
        assert [call.request.url.params["startAt"] for call in route.calls] == ["0", "2"]


class TestPagedLists:
    @respx.mock
    async def test_reads_every_page(self, sleeps):
        url = f"{CLOUD_URL}/rest/api/2/issue/1/changelog"
        respx.get(url, params={"startAt": "0"}).mock(
            return_value=httpx.Response(200, json={"total": 3, "isLast": False, "values": [{"id": "a"}, {"id": "b"}]})
        )
        respx.get(url, params={"startAt": "2"}).mock(
            return_value=httpx.Response(200, json={"total": 3, "isLast": True, "values": [{"id": "c"}]})
        )
        async with _client(sleeps) as client:
            values = await client.paged_values("/rest/api/2/issue/1/changelog", "values")
        assert [v["id"] for v in values] == ["a", "b", "c"]

    @respx.mock
    async def test_stops_on_an_empty_page(self, sleeps):
        respx.get(f"{CLOUD_URL}/rest/api/2/issue/1/comment").mock(
            side_effect=[
                httpx.Response(200, json={"comments": [{"id": "1"}]}),
                httpx.Response(200, json={"comments": []}),
            ]
        )
        async with _client(sleeps) as client:
            values = await client.paged_values("/rest/api/2/issue/1/comment", "comments")
        assert values == [{"id": "1"}]


MEDIA = "https://api.media.example.com"
CONTENT = f"{CLOUD_URL}/rest/api/3/attachment/content/10201"
MEDIA_FILE = f"{MEDIA}/file/5e1f0000-0000-4000-8000-000000010201/binary"
BYTES = b"synthetic attachment bytes\n" * 100


def _redirect(location: str, status: int = 303) -> httpx.Response:
    return httpx.Response(status, headers={"Location": location})


class TestDownloads:
    @respx.mock
    async def test_the_bytes_are_hashed_as_they_are_written(self, sleeps):
        respx.get(CONTENT).mock(return_value=httpx.Response(200, content=BYTES))
        target = io.BytesIO()
        async with _client(sleeps) as client:
            got = await client.download("/rest/api/3/attachment/content/10201", target)
        assert target.getvalue() == BYTES
        assert got.sha256 == hashlib.sha256(BYTES).hexdigest()
        assert got.size == len(BYTES)
        assert got.location is None

    @respx.mock
    async def test_credentials_never_go_to_another_host(self, sleeps):
        site = respx.get(CONTENT).mock(return_value=_redirect(f"{MEDIA_FILE}?token=media-token&dl=true", 303))
        media = respx.get(MEDIA_FILE).mock(
            return_value=httpx.Response(200, content=BYTES, headers={"Set-Cookie": "media=1"})
        )
        async with _client(sleeps) as client:
            client._http.cookies.set("atlassian.xsrf.token", "site-cookie", domain="example.atlassian.net")
            got = await client.download("/rest/api/3/attachment/content/10201", io.BytesIO())
            await client.download("/rest/api/3/attachment/content/10201", io.BytesIO())
        assert site.calls[0].request.headers["Authorization"].startswith("Basic ")
        for call in media.calls:
            assert "Authorization" not in call.request.headers
            assert "Cookie" not in call.request.headers
            assert call.request.url.params["token"] == "media-token"
        assert got.location == f"{MEDIA_FILE}?token=media-token&dl=true"

    @respx.mock
    async def test_a_bearer_token_is_not_sent_away_either(self, sleeps):
        respx.get(f"{DC_URL}/secure/attachment/1/a.png").mock(return_value=_redirect(f"{MEDIA}/a.png", 302))
        media = respx.get(f"{MEDIA}/a.png").mock(return_value=httpx.Response(200, content=b"png"))
        async with _client(sleeps, url=DC_URL, auth=TokenAuth("pat-secret")) as client:
            await client.download(f"{DC_URL}/secure/attachment/1/a.png", io.BytesIO())
        assert "Authorization" not in media.calls[0].request.headers

    @respx.mock
    async def test_a_redirect_within_the_site_keeps_the_credentials(self, sleeps):
        respx.get(f"{DC_URL}/secure/attachment/1/a.png").mock(return_value=_redirect("/files/1/a.png", 302))
        moved = respx.get(f"{DC_URL}/files/1/a.png").mock(return_value=httpx.Response(200, content=b"png"))
        async with _client(sleeps, url=DC_URL, auth=TokenAuth("pat-secret")) as client:
            await client.download("/secure/attachment/1/a.png", io.BytesIO())
        assert moved.calls[0].request.headers["Authorization"] == "Bearer pat-secret"

    @pytest.mark.parametrize(
        ("url", "trusted"),
        [
            (f"{CLOUD_URL}/x", True),
            ("/rest/api/3/x", True),
            ("https://EXAMPLE.atlassian.net:443/x", True),
            ("http://example.atlassian.net/x", False),
            ("https://example.atlassian.net:8443/x", False),
            ("https://example.atlassian.net.evil.example/x", False),
            (f"{MEDIA}/x", False),
        ],
    )
    async def test_only_the_site_is_trusted(self, sleeps, url, trusted):
        async with _client(sleeps) as client:
            assert client.trusted(url) is trusted

    @respx.mock
    async def test_httpx_drops_the_header_on_a_redirect_to_another_host(self, sleeps):
        # JSON requests let httpx follow redirects; it strips Authorization when the origin changes.
        respx.get(f"{CLOUD_URL}/rest/api/2/serverInfo").mock(return_value=_redirect(f"{MEDIA}/info", 302))
        away = respx.get(f"{MEDIA}/info").mock(return_value=httpx.Response(200, json={"deploymentType": "Cloud"}))
        async with _client(sleeps) as client:
            await client.server_info()
        assert "Authorization" not in away.calls[0].request.headers

    @respx.mock
    async def test_a_redirect_to_plain_http_is_refused(self, sleeps):
        respx.get(CONTENT).mock(return_value=_redirect("http://api.media.example.com/file/x/binary"))
        async with _client(sleeps) as client:
            with pytest.raises(JiraError, match="refused a redirect"):
                await client.download(CONTENT, io.BytesIO())

    @respx.mock
    async def test_too_many_redirects(self, sleeps):
        respx.get(url__startswith=MEDIA).mock(side_effect=lambda r: _redirect(str(r.url) + "x", 302))
        respx.get(CONTENT).mock(return_value=_redirect(f"{MEDIA}/a"))
        async with _client(sleeps) as client:
            with pytest.raises(JiraError, match="more than 5 redirects"):
                await client.download(CONTENT, io.BytesIO())

    @respx.mock
    async def test_over_the_limit_by_content_length(self, sleeps):
        respx.get(CONTENT).mock(return_value=_redirect(MEDIA_FILE))
        respx.get(MEDIA_FILE).mock(return_value=httpx.Response(200, content=BYTES))
        target = io.BytesIO()
        async with _client(sleeps) as client:
            with pytest.raises(DownloadTooLargeError) as raised:
                await client.download(CONTENT, target, max_bytes=100)
        assert raised.value.location == MEDIA_FILE
        assert target.getvalue() == b""

    @respx.mock
    async def test_over_the_limit_as_it_arrives(self, sleeps):
        async def chunks():
            for _ in range(10):
                yield b"x" * 64

        respx.get(CONTENT).mock(return_value=httpx.Response(200, content=chunks()))
        async with _client(sleeps) as client:
            with pytest.raises(DownloadTooLargeError):
                await client.download(CONTENT, io.BytesIO(), max_bytes=200)

    @respx.mock
    @pytest.mark.parametrize("status", [403, 404])
    async def test_a_refusal_raises_with_its_status(self, sleeps, status):
        respx.get(CONTENT).mock(return_value=httpx.Response(status, json={"errorMessages": ["no"]}))
        async with _client(sleeps) as client:
            with pytest.raises(JiraError) as raised:
                await client.download(CONTENT, io.BytesIO())
        assert raised.value.status == status

    @respx.mock
    async def test_a_rate_limited_media_host_is_retried(self, sleeps):
        respx.get(CONTENT).mock(return_value=_redirect(MEDIA_FILE))
        respx.get(MEDIA_FILE).mock(
            side_effect=[httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200, content=BYTES)]
        )
        async with _client(sleeps) as client:
            got = await client.download(CONTENT, io.BytesIO())
        assert got.size == len(BYTES)
        assert len(sleeps.waits) == 1

    @respx.mock
    async def test_the_token_in_a_media_url_is_never_logged(self, sleeps, caplog):
        respx.get(CONTENT).mock(return_value=_redirect(f"{MEDIA_FILE}?token=media-token"))
        respx.get(MEDIA_FILE).mock(side_effect=[httpx.Response(503), httpx.Response(500)])
        caplog.set_level(logging.WARNING)
        async with _client(sleeps) as client:
            with pytest.raises(JiraError) as raised:
                await client.download(CONTENT, io.BytesIO())
        assert "media-token" not in caplog.text + str(raised.value)
        assert "secret-token" not in caplog.text + str(raised.value)

    @respx.mock
    async def test_locate_does_not_follow_the_redirect(self, sleeps):
        respx.get(CONTENT).mock(return_value=_redirect(MEDIA_FILE))
        media = respx.get(MEDIA_FILE).mock(return_value=httpx.Response(200, content=BYTES))
        async with _client(sleeps) as client:
            assert await client.locate("/rest/api/3/attachment/content/10201") == MEDIA_FILE
        assert not media.called
