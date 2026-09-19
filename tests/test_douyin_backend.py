"""Douyin backend client: envelope, auth, tasks, retries, capabilities.

All HTTP is mocked with ``httpx.MockTransport``; nothing here touches the
network or a real Douyin backend.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from sources.douyin_backend import (
    DouyinBackendAuthError,
    DouyinBackendClient,
    DouyinBackendError,
    DouyinBackendRateLimited,
    DouyinBackendSchemaError,
    DouyinBackendTaskError,
    DouyinBackendUnavailable,
)

API_KEY = "dtk_testkey_0123456789abcdef"


def run(coro):
    return asyncio.run(coro)


def envelope(data: Any = None, *, error: Any = None, meta: Any = None) -> dict[str, Any]:
    return {
        "success": error is None,
        "data": None if error is not None else data,
        "error": error,
        "meta": meta or {"request_id": "req-1"},
    }


class Backend:
    """Scripted backend: one entry per request, last entry repeats."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []
        self.concurrent = 0
        self.max_concurrent = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        entry = self.responses[index]
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, tuple):  # (status, body, headers)
            status, body = entry[0], entry[1]
            headers = entry[2] if len(entry) > 2 else None
            return httpx.Response(status, json=body, headers=headers)
        return httpx.Response(200, json=entry)

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


def make_client(backend: Backend, **kwargs: Any) -> DouyinBackendClient:
    payload: dict[str, Any] = {
        "base_url": "http://backend.test",
        "api_key": API_KEY,
        "timeout": 5.0,
        "max_retries": 2,
        "backoff_seconds": 0.0,
        "task_wait_seconds": 0.0,
        "task_poll_interval_seconds": 0.01,
        "max_task_wait_seconds": 5.0,
        "concurrency": 2,
        "client_factory": lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(backend.handler), timeout=5.0
        ),
    }
    payload.update(kwargs)
    return DouyinBackendClient(**payload)


# ---------------------------------------------------------------------------
# envelope + auth
# ---------------------------------------------------------------------------
def test_request_sends_api_key_header_and_never_leaks_it() -> None:
    backend = Backend([envelope({"ok": True})])
    client = make_client(backend)
    status, body = run(client.request("GET", "/api/v1/auth/me"))
    assert status == 200 and body["success"] is True
    request = backend.requests[0]
    assert request.headers["X-API-Key"] == API_KEY
    assert API_KEY not in request.url.path


def test_error_message_is_sanitized() -> None:
    backend = Backend(
        [
            (
                400,
                envelope(
                    error={
                        "code": "INVALID_PARAM",
                        "message": f"bad request with key {API_KEY} in it",
                    }
                ),
            )
        ]
    )
    client = make_client(backend)
    with pytest.raises(DouyinBackendError) as excinfo:
        run(client.request("GET", "/api/v1/archive"))
    assert API_KEY not in str(excinfo.value)
    assert "<redacted>" in str(excinfo.value)
    assert excinfo.value.code == "INVALID_PARAM"


def test_missing_envelope_keys_is_a_schema_error() -> None:
    backend = Backend([{"unexpected": True}])
    client = make_client(backend)
    with pytest.raises(DouyinBackendSchemaError):
        run(client.request("GET", "/api/v1/archive"))


def test_401_is_an_auth_error_without_retry() -> None:
    backend = Backend([(401, envelope(error={"code": "UNAUTHENTICATED", "message": "no key"}))])
    client = make_client(backend, max_retries=3)
    with pytest.raises(DouyinBackendAuthError):
        run(client.request("GET", "/api/v1/auth/me"))
    assert len(backend.requests) == 1


def test_connection_failure_is_reported_as_unavailable() -> None:
    backend = Backend([httpx.ConnectError("connection refused")])
    client = make_client(backend, max_retries=2)
    with pytest.raises(DouyinBackendUnavailable) as excinfo:
        run(client.request("GET", "/healthz"))
    assert "unreachable" in str(excinfo.value)
    assert len(backend.requests) == 2  # transient -> one retry


def test_429_is_retried_with_retry_after() -> None:
    backend = Backend(
        [
            (
                429,
                envelope(error={"code": "RATE_LIMITED", "message": "slow down", "retry_after": 0}),
                {"Retry-After": "0"},
            ),
            envelope({"items": []}),
        ]
    )
    client = make_client(backend, max_retries=3)
    status, body = run(client.request("GET", "/api/v1/archive"))
    assert status == 200
    assert len(backend.requests) == 2


def test_429_after_retries_raises_rate_limited() -> None:
    backend = Backend(
        [
            (
                429,
                envelope(error={"code": "RATE_LIMITED", "message": "slow down", "retry_after": 0}),
            )
        ]
    )
    client = make_client(backend, max_retries=2)
    with pytest.raises(DouyinBackendRateLimited):
        run(client.request("GET", "/api/v1/archive"))
    assert len(backend.requests) == 2


def test_5xx_is_retried_then_succeeds() -> None:
    backend = Backend(
        [
            (503, envelope(error={"code": "UNAVAILABLE", "message": "busy"})),
            envelope({"items": [{"content_id": "1"}]}),
        ]
    )
    client = make_client(backend, max_retries=3)
    status, body = run(client.request("GET", "/api/v1/archive"))
    assert status == 200 and body["data"]["items"]


def test_concurrency_is_bounded() -> None:
    backend = Backend([envelope({"ok": True})])
    client = make_client(backend, concurrency=1)

    async def hammer() -> None:
        await asyncio.gather(
            client.request("GET", "/api/v1/archive"),
            client.request("GET", "/api/v1/archive"),
            client.request("GET", "/api/v1/archive"),
        )

    run(hammer())
    assert len(backend.requests) == 3
    assert client._semaphore._value == 1  # released again, never exceeded


# ---------------------------------------------------------------------------
# async task model
# ---------------------------------------------------------------------------
def test_immediate_200_result_needs_no_polling() -> None:
    backend = Backend([envelope({"content_id": "123"})])
    client = make_client(backend)
    result = run(client.content_detail(aweme_id="123"))
    assert result.ok and result.state == "done"
    assert result.data == {"content_id": "123"}
    assert backend.paths == ["/api/v1/douyin/video"]


def test_202_is_polled_until_done() -> None:
    backend = Backend(
        [
            (202, envelope({"task_id": "t-1", "state": "queued"})),
            (200, envelope({"task_id": "t-1", "state": "running"})),
            (200, envelope({"task_id": "t-1", "state": "running"})),
            (
                200,
                envelope(
                    {
                        "task_id": "t-1",
                        "state": "done",
                        "endpoint": "douyin.content_detail",
                        "data": {"content_id": "123", "title": "苹果烘干"},
                        "result_meta": {"cached": False},
                    }
                ),
            ),
        ]
    )
    client = make_client(backend)
    result = run(client.content_detail(aweme_id="123"))
    assert result.ok
    assert result.data["content_id"] == "123"
    assert result.meta.get("cached") is False
    assert backend.paths == [
        "/api/v1/douyin/video",
        "/api/v1/tasks/t-1",
        "/api/v1/tasks/t-1",
        "/api/v1/tasks/t-1",
    ]


def test_failed_task_reports_code_and_retryable() -> None:
    backend = Backend(
        [
            (202, envelope({"task_id": "t-2", "state": "queued"})),
            (
                200,
                envelope(
                    {
                        "task_id": "t-2",
                        "state": "failed",
                        "error": {
                            "code": "CONTENT_NOT_FOUND",
                            "message": "video deleted",
                            "retryable": False,
                        },
                    }
                ),
            ),
        ]
    )
    client = make_client(backend)
    with pytest.raises(DouyinBackendTaskError) as excinfo:
        run(client.content_detail(aweme_id="123"))
    assert excinfo.value.code == "CONTENT_NOT_FOUND"
    assert excinfo.value.retryable is False


def test_task_timeout_is_bounded() -> None:
    backend = Backend(
        [
            (202, envelope({"task_id": "t-3", "state": "queued"})),
            (200, envelope({"task_id": "t-3", "state": "running"})),
        ]
    )
    client = make_client(backend, max_task_wait_seconds=0.05, task_poll_interval_seconds=0.01)
    with pytest.raises(DouyinBackendTaskError) as excinfo:
        run(client.content_detail(aweme_id="123"))
    assert "did not finish" in str(excinfo.value)
    assert excinfo.value.retryable is True


def test_202_without_task_id_is_a_schema_error() -> None:
    backend = Backend([(202, envelope({"state": "queued"}))])
    client = make_client(backend)
    with pytest.raises(DouyinBackendSchemaError):
        run(client.content_detail(aweme_id="123"))


def test_unknown_task_state_is_rejected() -> None:
    backend = Backend(
        [
            (202, envelope({"task_id": "t-4", "state": "queued"})),
            (200, envelope({"task_id": "t-4", "state": "weird"})),
        ]
    )
    client = make_client(backend)
    with pytest.raises(DouyinBackendSchemaError):
        run(client.content_detail(aweme_id="123"))


# ---------------------------------------------------------------------------
# capabilities / health
# ---------------------------------------------------------------------------
OPENAPI_WITHOUT_SEARCH = {
    "openapi": "3.1.0",
    "info": {"version": "5.0.3"},
    "paths": {
        "/api/v1/{platform}/video": {"get": {}},
        "/api/v1/{platform}/user/posts": {"get": {}},
        "/api/v1/archive": {"get": {}},
        "/api/v1/tasks/{task_id}": {"get": {}},
        "/api/v1/downloads": {"post": {}},
    },
}

OPENAPI_WITH_SEARCH = {
    "openapi": "3.1.0",
    "info": {"version": "9.9.9"},
    "paths": {
        **OPENAPI_WITHOUT_SEARCH["paths"],
        "/api/v1/douyin/search": {"get": {}},
    },
}


def health_backend(openapi: dict) -> Backend:
    return Backend(
        [
            (200, {"status": "ok"}),
            (200, envelope({"username": "admin", "role": "admin", "scopes": ["douyin:read"]})),
            (200, envelope({"version": "5.0.3", "commit": "4ef5ed5", "uptime_seconds": 12})),
            (200, openapi),
        ]
    )


def test_health_reports_missing_keyword_search() -> None:
    client = make_client(health_backend(OPENAPI_WITHOUT_SEARCH))
    capabilities = run(client.health())
    assert capabilities.reachable and capabilities.authorized
    assert capabilities.version == "5.0.3"
    assert capabilities.content_read and capabilities.archive_search
    assert capabilities.task_support
    assert capabilities.keyword_search is False
    assert any("no keyword search" in note for note in capabilities.notes)
    lines = "\n".join(capabilities.summary_lines())
    assert "keyword search endpoint: NOT provided" in lines


def test_health_detects_an_installed_keyword_search_route() -> None:
    client = make_client(health_backend(OPENAPI_WITH_SEARCH))
    capabilities = run(client.health())
    assert capabilities.keyword_search is True
    assert capabilities.search_endpoint == "/api/v1/douyin/search"


def test_health_when_backend_is_down() -> None:
    backend = Backend([httpx.ConnectError("refused")])
    client = make_client(backend, max_retries=1)
    capabilities = run(client.health())
    assert capabilities.reachable is False
    assert capabilities.authorized is False
    assert any("not running" in line for line in capabilities.summary_lines())


def test_health_without_credentials() -> None:
    backend = Backend(
        [
            (200, {"status": "ok"}),
            (401, envelope(error={"code": "UNAUTHENTICATED", "message": "no key"})),
            (200, OPENAPI_WITHOUT_SEARCH),
        ]
    )
    client = make_client(backend, api_key="", max_retries=1)
    capabilities = run(client.health())
    assert capabilities.reachable is True
    assert capabilities.authorized is False
    assert any("401/403" in note for note in capabilities.notes)


def test_keyword_search_raises_when_the_backend_has_no_route() -> None:
    backend = Backend([(200, OPENAPI_WITHOUT_SEARCH)])
    client = make_client(backend)
    with pytest.raises(DouyinBackendSchemaError):
        run(client.keyword_search(query="苹果干"))


# ---------------------------------------------------------------------------
# documented endpoints
# ---------------------------------------------------------------------------
def test_content_detail_requires_an_id_or_url() -> None:
    client = make_client(Backend([envelope({})]))
    with pytest.raises(ValueError):
        run(client.content_detail())


def test_content_detail_passes_url_and_wait_parameter() -> None:
    backend = Backend([envelope({"content_id": "1"})])
    client = make_client(backend, task_wait_seconds=7.0)
    run(client.content_detail(url="https://www.douyin.com/video/1"))
    request = backend.requests[0]
    assert request.url.params["url"] == "https://www.douyin.com/video/1"
    assert request.url.params["wait"] == "7"


def test_user_posts_pagination_parameters() -> None:
    backend = Backend([envelope({"items": [], "cursor": None, "has_more": False})])
    client = make_client(backend)
    run(client.user_posts(sec_user_id="MS4wLjAB", cursor="CUR", count=20))
    params = backend.requests[0].url.params
    assert params["sec_user_id"] == "MS4wLjAB"
    assert params["cursor"] == "CUR"
    assert params["count"] == "20"


def test_mix_posts_requires_mix_id() -> None:
    backend = Backend([envelope({"items": []})])
    client = make_client(backend)
    run(client.mix_posts(mix_id="mix-1", count=5))
    assert backend.requests[0].url.params["mix_id"] == "mix-1"


def test_archive_search_sends_q_and_limit() -> None:
    backend = Backend([envelope({"items": []})])
    client = make_client(backend)
    run(client.archive_search(q="苹果干", limit=15))
    params = backend.requests[0].url.params
    assert params["q"] == "苹果干" and params["limit"] == "15" and params["platform"] == "douyin"


def test_parse_posts_a_json_body() -> None:
    backend = Backend([envelope({"content_id": "1"})])
    client = make_client(backend)
    run(client.parse_url("https://v.douyin.com/abc/"))
    request = backend.requests[0]
    assert request.method == "POST"
    assert json.loads(request.content)["url"] == "https://v.douyin.com/abc/"
