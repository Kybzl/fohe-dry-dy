"""Milestone 3.x reliability regressions.

Focus: probe once per task, sticky backend/browser failures, composite
short-circuit, blocked-vs-zero-results, localhost proxy policy, clean Ctrl+C.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from core.dependencies import build_library
from core.models import TaskRequest, TaskStatus
from core.task_runner import TaskRunner
from sources.douyin import DouyinSource
from sources.douyin_backend import (
    DouyinBackendClient,
    DouyinBackendUnavailable,
    is_loopback_url,
)
from sources.douyin_browser_search import DouyinBrowserSearchBackend
from sources.douyin_search import (
    ArchiveSearchBackend,
    BrowserSearchStatus,
    CompositeSearchBackend,
    DiscoveryBlockedError,
    KeywordSearchBackend,
    ManualUrlSearchBackend,
)

OPENAPI_PATHS = {
    "/api/v1/{platform}/video": {"get": {}},
    "/api/v1/archive": {"get": {}},
    "/api/v1/tasks/{task_id}": {"get": {}},
}


def run(coro):
    return asyncio.run(coro)


class CountingBackend:
    """Counts every HTTP request so 'probe once' can be asserted."""

    def __init__(
        self,
        *,
        openapi_status: int = 200,
        archive_status: int = 200,
        items: list[dict] | None = None,
    ) -> None:
        self.openapi_status = openapi_status
        self.archive_status = archive_status
        self.items = items or []
        self.requests: list[str] = []
        self.path_counts: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        self.path_counts[path] = self.path_counts.get(path, 0) + 1
        if path == "/openapi.json":
            if self.openapi_status != 200:
                return httpx.Response(self.openapi_status, text="502 Bad Gateway kngx")
            return httpx.Response(
                200,
                json={"openapi": "3.1.0", "info": {"version": "5.0.3"}, "paths": OPENAPI_PATHS},
            )
        if path == "/api/v1/archive":
            if self.archive_status != 200:
                return httpx.Response(self.archive_status, text="502 Bad Gateway kngx")
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"items": self.items, "cursor": None, "has_more": False},
                    "error": None,
                    "meta": {},
                },
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"items": self.items, "cursor": None, "has_more": False},
                "error": None,
                "meta": {},
            },
        )


def make_client(backend: CountingBackend, *, base_url: str = "http://127.0.0.1:8000", **kw) -> DouyinBackendClient:
    payload: dict[str, Any] = {
        "base_url": base_url,
        "api_key": "k",
        "timeout": 2.0,
        "max_retries": 1,
        "backoff_seconds": 0.0,
        "task_wait_seconds": 0.0,
        "max_task_wait_seconds": 1.0,
        "client_factory": lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(backend.handler), timeout=2.0
        ),
    }
    payload.update(kw)
    return DouyinBackendClient(**payload)


# ---------------------------------------------------------------------------
# 1. capability probe happens once per task, not per keyword
# ---------------------------------------------------------------------------
def test_capability_probe_runs_once_per_client() -> None:
    backend = CountingBackend()
    client = make_client(backend)

    async def flow() -> None:
        for _ in range(11):  # 11 search keywords
            await client.probe_capabilities()
            await client.discover_search_endpoint()
        await client.aclose()

    run(flow())
    assert backend.path_counts.get("/openapi.json") == 1, backend.requests


def test_capability_probe_runs_once_across_keywords_through_the_source() -> None:
    backend = CountingBackend()
    client = make_client(backend)
    composite = CompositeSearchBackend(
        [KeywordSearchBackend(client), ArchiveSearchBackend(client)]
    )

    async def flow() -> None:
        for query in ("苹果热泵烘干", "苹果烘干机", "苹果烘干房", "苹果片烘干"):
            await composite.search(query, 5)
        await client.aclose()

    run(flow())
    assert backend.path_counts.get("/openapi.json") == 1
    assert backend.path_counts.get("/api/v1/archive") == 4, "one archive call per keyword"


# ---------------------------------------------------------------------------
# 2. exhausted 502 is sticky for the current task
# ---------------------------------------------------------------------------
def test_backend_502_is_sticky_and_never_re_retried() -> None:
    backend = CountingBackend(openapi_status=502)
    client = make_client(backend)

    async def flow() -> None:
        state = await client.probe_capabilities()
        assert state.available is False
        assert state.status == "backend_unavailable"
        assert state.http_status == 502
        requests_after_probe = len(backend.requests)
        # every later call must fast-fail without touching the network
        for _ in range(5):
            with pytest.raises(DouyinBackendUnavailable):
                await client.request("GET", "/api/v1/archive")
        assert len(backend.requests) == requests_after_probe
        await client.aclose()

    run(flow())
    assert backend.path_counts.get("/openapi.json") == 1


def test_archive_backend_fast_fails_after_a_backend_502() -> None:
    backend = CountingBackend(openapi_status=502)
    client = make_client(backend)
    archive = ArchiveSearchBackend(client)

    async def flow():
        await client.probe_capabilities()
        before = len(backend.requests)
        outcome = await archive.search("苹果干烘干", 5)
        return outcome, before

    outcome, before = run(flow())
    assert outcome.status == BrowserSearchStatus.BACKEND_UNAVAILABLE.value
    assert outcome.candidates == []
    assert len(backend.requests) == before, "no HTTP while the backend is marked down"


def test_local_backend_502_logs_an_actionable_hint() -> None:
    backend = CountingBackend(archive_status=502)
    client = make_client(backend, max_retries=1)

    async def flow() -> str:
        await client.probe_capabilities()
        try:
            await client.archive_search(q="苹果干", limit=3)
        except Exception as exc:  # noqa: BLE001 - message is what matters here
            return str(exc)
        raise AssertionError("expected a backend error")

    message = run(flow())
    assert "502" in message
    assert "local Douyin backend" in message
    assert "system proxy" in message
    assert "CAPTCHA" not in message, "a backend 502 is not a Douyin CAPTCHA"


# ---------------------------------------------------------------------------
# 3./4. blocked discovery short-circuits and is distinguishable
# ---------------------------------------------------------------------------
class BlockedBrowser:
    """Browser backend stub stuck in ``verification_required``."""

    name = "browser"
    exclusive = False

    def __init__(self) -> None:
        self.calls = 0

    async def probe(self) -> tuple[bool, str]:
        return False, f"{BrowserSearchStatus.VERIFICATION_REQUIRED.value} (login/verification wall)"

    def unavailable_reason(self) -> str:
        return BrowserSearchStatus.VERIFICATION_REQUIRED.value

    async def search(self, query: str, limit: int):
        self.calls += 1
        raise AssertionError("a blocked browser must not be searched")


def test_all_backends_blocked_raises_discovery_blocked() -> None:
    dtk = CountingBackend(openapi_status=502)
    client = make_client(dtk)
    browser = BlockedBrowser()
    composite = CompositeSearchBackend(
        [KeywordSearchBackend(client), browser, ArchiveSearchBackend(client)]
    )

    async def flow():
        states = await composite.backend_states()
        with pytest.raises(DiscoveryBlockedError) as excinfo:
            await composite.search("苹果干烘干", 5)
        return states, excinfo.value

    states, error = run(flow())
    assert states["browser"] == BrowserSearchStatus.VERIFICATION_REQUIRED.value
    assert states["keyword"] == BrowserSearchStatus.BACKEND_UNAVAILABLE.value
    assert states["archive"] == BrowserSearchStatus.BACKEND_UNAVAILABLE.value
    assert error.states["browser"] == BrowserSearchStatus.VERIFICATION_REQUIRED.value
    assert dtk.path_counts.get("/openapi.json") == 1


def test_blocked_discovery_stops_keyword_iteration_early(settings, monkeypatch) -> None:
    """The orchestrator must not walk all 10 keywords once discovery is blocked."""

    from core import dependencies as dependencies_module

    calls = {"search": 0}

    def fake_dependencies(*args: Any, **kwargs: Any):
        deps = dependencies_module.build_dependencies(
            settings, source_name="douyin", provider_name="mock", media_backend="mock"
        )

        async def blocked_search(query: str, limit: int):
            calls["search"] += 1
            raise DiscoveryBlockedError(
                "no discovery backend can run (browser: verification_required)",
                states={"browser": "verification_required", "dtk": "backend_unavailable"},
            )

        deps.source.search = blocked_search  # type: ignore[method-assign]
        return deps

    runner = TaskRunner(settings)
    monkeypatch.setattr(
        "core.task_runner.build_dependencies", fake_dependencies, raising=True
    )
    result = runner.run(
        TaskRequest(material="苹果干", target_clip_count=2, source="douyin", provider="mock")
    )

    assert calls["search"] == 1, "discovery blocked must stop after the first keyword"
    assert result.discovery_blocked is True
    assert result.discovery_status == "discovery_blocked"
    assert any("discovery_blocked" in message for message in result.messages)
    assert result.status is TaskStatus.PARTIAL


def test_zero_result_search_is_not_reported_as_blocked(settings, monkeypatch) -> None:
    from core import dependencies as dependencies_module

    calls = {"search": 0}

    def fake_dependencies(*args: Any, **kwargs: Any):
        deps = dependencies_module.build_dependencies(
            settings, source_name="douyin", provider_name="mock", media_backend="mock"
        )

        async def empty_search(query: str, limit: int):
            calls["search"] += 1
            return []

        deps.source.search = empty_search  # type: ignore[method-assign]
        return deps

    runner = TaskRunner(settings)
    monkeypatch.setattr(
        "core.task_runner.build_dependencies", fake_dependencies, raising=True
    )
    result = runner.run(
        TaskRequest(material="苹果干", target_clip_count=2, source="douyin", provider="mock")
    )

    assert calls["search"] > 1, "a normal 0-result search keeps trying other keywords"
    assert result.discovery_blocked is False
    assert result.discovery_status != "discovery_blocked"
    assert result.stats.searched_candidates == 0
    assert any("没有找到可用片段" in message for message in result.messages)


# ---------------------------------------------------------------------------
# 5. localhost proxy handling
# ---------------------------------------------------------------------------
def test_loopback_detection() -> None:
    assert is_loopback_url("http://127.0.0.1:8000") is True
    assert is_loopback_url("http://localhost:8000") is True
    assert is_loopback_url("http://[::1]:8000") is True
    assert is_loopback_url("https://demo.douyin.wtf") is False


def test_local_backend_does_not_use_environment_proxy(monkeypatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    client = DouyinBackendClient(base_url="http://127.0.0.1:8000")
    assert client.uses_environment_proxy is False
    http_client = client._get_client()  # noqa: SLF001 - the flag is the assertion
    assert http_client.trust_env is False
    assert not http_client._mounts, "no proxy mounts may be configured"


def test_localhost_backend_never_uses_environment_proxy(monkeypatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    client = DouyinBackendClient(base_url="http://localhost:8000")
    assert client.uses_environment_proxy is False
    assert client._get_client().trust_env is False  # noqa: SLF001


def test_remote_backend_keeps_environment_proxy_support() -> None:
    client = DouyinBackendClient(base_url="https://demo.douyin.wtf")
    assert client.uses_environment_proxy is True


def test_trust_env_can_be_forced_from_config() -> None:
    # an operator behind a corporate proxy for a localhost tunnel can opt in
    client = DouyinBackendClient(base_url="http://127.0.0.1:8000", trust_env=True)
    assert client.uses_environment_proxy is True
    client = DouyinBackendClient(base_url="https://demo.douyin.wtf", trust_env=False)
    assert client.uses_environment_proxy is False


# ---------------------------------------------------------------------------
# 6. clean Ctrl+C
# ---------------------------------------------------------------------------
def test_keyboard_interrupt_returns_a_clean_cancelled_result(settings, monkeypatch) -> None:
    from core import dependencies as dependencies_module

    def fake_dependencies(*args: Any, **kwargs: Any):
        deps = dependencies_module.build_dependencies(
            settings, source_name="douyin", provider_name="mock", media_backend="mock"
        )

        async def interrupted_search(query: str, limit: int):
            raise KeyboardInterrupt

        deps.source.search = interrupted_search  # type: ignore[method-assign]
        return deps

    monkeypatch.setattr(
        "core.task_runner.build_dependencies", fake_dependencies, raising=True
    )
    runner = TaskRunner(settings)
    result = runner.run(
        TaskRequest(material="苹果干", target_clip_count=2, source="douyin", provider="mock")
    )

    assert isinstance(result, type(result))  # returns, never raises
    assert result.status is TaskStatus.CANCELLED
    assert any("中断" in message for message in result.messages)
    # the running task row is marked cancelled, not left as running/succeeded
    assert result.task_id is not None, "the interrupted task id must be reported"
    task = runner.library.get_task(result.task_id) if result.task_id else None
    if task is not None:
        assert task.status is TaskStatus.CANCELLED
    leftovers = [path for path in settings.paths.cache_dir.rglob("*") if path.is_file()]
    assert leftovers == [], leftovers


def test_cancel_event_still_reports_cancelled(settings) -> None:
    import threading

    cancel = threading.Event()
    cancel.set()
    runner = TaskRunner(settings)
    runner._cancel_event = cancel  # noqa: SLF001 - simulating an operator stop
    result = runner.run(
        TaskRequest(material="苹果干", target_clip_count=2, source="douyin", provider="mock")
    )
    assert result.status is TaskStatus.CANCELLED


def test_cleanup_removes_staged_sources_and_derived_files(settings) -> None:
    """Even an interrupted run must leave ``cache/`` empty."""

    from core.dependencies import build_dependencies
    from core.orchestrator import CollectionOrchestrator

    cache = settings.paths.cache_dir
    (cache / "frames").mkdir(parents=True, exist_ok=True)
    (cache / "previews").mkdir(parents=True, exist_ok=True)
    (cache / "thumbnails").mkdir(parents=True, exist_ok=True)
    (cache / "local_local_deadbeef.mp4").write_bytes(b"staged")
    (cache / "local_local_deadbeef.mp4.meta.json").write_text("{}", encoding="utf-8")
    (cache / "frames" / "vid_analysis_000.jpg").write_bytes(b"frame")
    (cache / "previews" / "vid_preview_000.jpg").write_bytes(b"preview")
    (cache / "thumbnails" / "clip_cand0.jpg").write_bytes(b"thumb")
    (cache / "partial.mp4.part").write_bytes(b"partial")

    dependencies = build_dependencies(
        settings, source_name="mock", provider_name="mock", media_backend="mock"
    )
    orchestrator = CollectionOrchestrator(dependencies)
    messages = run(orchestrator._cleanup_cache([]))  # noqa: SLF001 - cleanup path

    leftovers = [path for path in cache.rglob("*") if path.is_file()]
    assert leftovers == [], leftovers
    assert any("暂存源视频" in message for message in messages)


def test_cancellable_copy_leaves_no_partial_file(tmp_path: Path) -> None:
    """Ctrl+C during a large copy must not leave a half-written source."""

    from media.downloader import LocalFileDownloader

    source = tmp_path / "big.mp4"
    source.write_bytes(b"x" * (8 << 20))  # 8 MiB: many chunks
    dest = tmp_path / "cache" / "staged.mp4"
    downloader = LocalFileDownloader()

    async def flow() -> bool:
        task = asyncio.ensure_future(downloader.download(source.as_uri(), dest))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return dest.exists()

    assert run(flow()) is False
