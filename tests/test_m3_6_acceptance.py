"""Milestone 3.6 regressions.

Focus: reject-reason precedence, material normalization, backend resolution /
preflight, browser configuration alignment, discovery-vs-zero-result and the
regressions section 31 asks for.

Nothing in this module performs a real network call, a paid Qwen call or a
real Douyin request: every external collaborator is injected.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Sequence

import pytest

from core.backend_resolver import (
    BackendCandidate,
    BackendProbe,
    ORIGIN_FALLBACK,
    apply_backend_selection,
    backend_candidates,
    resolve_douyin_backend,
)
from core.browser_config import (
    BUNDLED_CHANNEL,
    channel_executable,
    detect_installed_channels,
    resolve_browser_config,
)
from core.config import load_settings
from core.dependencies import build_browser_search, build_library
from core.models import (
    ClipTagging,
    MaterialForm,
    MaterialState,
    PreviewFrame,
    PreviewSource,
    RejectReason,
    SourceVideoStatus,
    SubtitlePolicy,
    TaskRequest,
    TaskStatus,
    VideoCandidate,
)
from core.normalization import (
    canonical_material,
    default_material_state,
    material_base,
    normalize_tagging,
)
from core.task_runner import TaskRunner


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. terminal rejection reason precedence (section 1 / 20)
# ---------------------------------------------------------------------------
def _library(settings):
    return build_library(settings)


def test_terminal_reject_reason_survives_duplicate_discovery(settings) -> None:
    library = _library(settings)
    library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="7600000000000000001",
        source_url="https://www.douyin.com/video/7600000000000000001",
        title="苹果干烘干",
        status=SourceVideoStatus.REJECTED_PREVIEW,
        reject_reason=RejectReason.NO_MATERIAL,
    )
    # the same post shows up again under another keyword / another run
    library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="7600000000000000001",
        source_url="https://www.douyin.com/video/7600000000000000001",
        status=SourceVideoStatus.REJECTED,
        reject_reason=RejectReason.DUPLICATE_VIDEO,
        matched_queries=["苹果烘干机"],
    )

    stored = library.get_source_video("douyin", "7600000000000000001")
    assert stored is not None
    assert stored.reject_reason is RejectReason.NO_MATERIAL
    assert stored.status is SourceVideoStatus.REJECTED_PREVIEW


def test_real_verdict_still_overwrites_a_bookkeeping_row(settings) -> None:
    """The guard must not freeze a duplicate bookkeeping row forever."""

    library = _library(settings)
    library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="7600000000000000002",
        source_url="https://www.douyin.com/video/7600000000000000002",
        status=SourceVideoStatus.REJECTED,
        reject_reason=RejectReason.ALREADY_PROCESSED,
    )
    library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="7600000000000000002",
        source_url="https://www.douyin.com/video/7600000000000000002",
        status=SourceVideoStatus.REJECTED_PREVIEW,
        reject_reason=RejectReason.LOW_QUALITY,
    )
    stored = library.get_source_video("douyin", "7600000000000000002")
    assert stored is not None
    assert stored.reject_reason is RejectReason.LOW_QUALITY


def test_merge_discovery_only_merges_queries(settings) -> None:
    library = _library(settings)
    library.upsert_source_video(
        task_id=None,
        platform="douyin",
        platform_video_id="7600000000000000003",
        source_url="https://www.douyin.com/video/7600000000000000003",
        status=SourceVideoStatus.REJECTED_PREVIEW,
        reject_reason=RejectReason.NO_MATERIAL,
    )
    library.merge_discovery(
        platform="douyin",
        platform_video_id="7600000000000000003",
        query="苹果干烘干",
        source_url="https://www.douyin.com/video/7600000000000000003",
        title="苹果干烘干",
        author="某厂",
    )
    library.merge_discovery(
        platform="douyin",
        platform_video_id="7600000000000000003",
        query="苹果热泵烘干",
    )
    stored = library.get_source_video("douyin", "7600000000000000003")
    assert stored is not None
    assert stored.matched_queries == ["苹果干烘干", "苹果热泵烘干"]
    assert stored.reject_reason is RejectReason.NO_MATERIAL
    assert stored.title == "苹果干烘干"


def test_merge_discovery_inserts_unknown_video(settings) -> None:
    library = _library(settings)
    record = library.merge_discovery(
        platform="douyin",
        platform_video_id="7600000000000000004",
        query="苹果片烘干",
        source_url="https://www.douyin.com/video/7600000000000000004",
    )
    assert record is not None
    assert record.status is SourceVideoStatus.DISCOVERED
    assert record.matched_queries == ["苹果片烘干"]


def test_duplicate_candidate_causes_no_second_ai_preview(settings, monkeypatch) -> None:
    """§13/§21: one preview per unique candidate, however many keywords saw it."""

    from core import dependencies as dependencies_module

    previews = {"calls": 0}
    queries = {"seen": []}
    candidate = VideoCandidate(
        platform="douyin",
        platform_video_id="7600000000000000005",
        source_url="https://www.douyin.com/video/7600000000000000005",
        title="苹果干烘干现场",
        author="某厂",
        duration=30.0,
        matched_queries=["苹果干烘干"],
    )

    def fake_dependencies(*args: Any, **kwargs: Any):
        deps = dependencies_module.build_dependencies(
            settings, source_name="mock", provider_name="mock", media_backend="mock"
        )

        async def search(query: str, limit: int):
            queries["seen"].append(query)
            return [candidate.model_copy(deep=True)]

        async def get_preview(video_id: str) -> PreviewSource:
            previews["calls"] += 1
            return PreviewSource(
                platform="douyin",
                platform_video_id=video_id,
                duration=30.0,
                frames=[PreviewFrame(timestamp=1.0)],
            )

        deps.source.search = search  # type: ignore[method-assign]
        deps.source.get_preview = get_preview  # type: ignore[method-assign]
        return deps

    monkeypatch.setattr(
        "core.task_runner.build_dependencies", fake_dependencies, raising=True
    )
    monkeypatch.setattr(
        "core.orchestrator.CollectionOrchestrator._plan_queries",
        lambda self, request: ["苹果干烘干", "苹果热泵烘干", "苹果烘干机"],
    )

    runner = TaskRunner(settings)
    result = runner.run(
        TaskRequest(
            material="苹果干",
            target_clip_count=199,
            subtitle_policy=SubtitlePolicy.STRICT,
            library_root=settings.paths.library_root,
            source="douyin",
            provider="mock",
        )
    )

    assert len(queries["seen"]) == 3, "every keyword still runs its own search"
    assert previews["calls"] == 1, "a rediscovered candidate must not be previewed twice"
    assert len(result.source_videos) == 1, "no duplicate report rows"
    assert not any("本地淘汰 [duplicate_video]" in message for message in result.messages)
    stored = runner.library.get_source_video("douyin", candidate.platform_video_id)
    assert stored is not None
    assert stored.matched_queries == ["苹果干烘干", "苹果热泵烘干", "苹果烘干机"], (
        "matched_queries must merge while the stored verdict stays untouched"
    )
    # the merge for the later keywords is recorded in the library
    assert runner.library.clips_for_task(result.task_id)


# ---------------------------------------------------------------------------
# 18. material normalization
# ---------------------------------------------------------------------------
def test_material_base_strips_product_form() -> None:
    assert material_base("苹果干") == "苹果"
    assert material_base("香蕉干") == "香蕉"
    assert material_base("苹果") == "苹果"
    assert material_base("苹果干烘干") == "苹果"


def test_default_material_state_follows_the_name() -> None:
    assert default_material_state("苹果干") is MaterialState.DRIED
    assert default_material_state("香蕉干") is MaterialState.DRIED
    assert default_material_state("苹果片") is MaterialState.UNKNOWN


def test_canonical_material_unifies_the_reference() -> None:
    assert canonical_material("苹果干", reference="苹果干") == "苹果"
    assert canonical_material("苹果", reference="苹果干") == "苹果"
    assert canonical_material("", reference="苹果干") == "苹果"
    # a different material is left alone: never invent a wrong tag
    assert canonical_material("香蕉", reference="苹果干") == "香蕉"


def test_normalize_tagging_never_derives_state_from_the_request() -> None:
    """Milestone 3.7 changed this: the state is an observation, not task intent."""

    tagging = ClipTagging(material="苹果干", material_form=MaterialForm.SLICE)
    normalized = normalize_tagging(tagging, request_material="苹果干")
    assert normalized.material == "苹果"
    assert normalized.material_state is MaterialState.UNKNOWN, (
        "an unobserved state must stay unknown even for a 苹果干 task"
    )
    assert normalized.material_form is MaterialForm.SLICE

    for state in (MaterialState.FRESH, MaterialState.PREPARED, MaterialState.DRYING):
        observed = ClipTagging(
            material="苹果", material_form=MaterialForm.SLICE, material_state=state
        )
        assert (
            normalize_tagging(observed, request_material="苹果干").material_state is state
        ), "the observed state always wins over the requested material"


def test_library_folder_stays_user_facing_while_tags_are_canonical(settings) -> None:
    from core.dependencies import build_dependencies
    from core.orchestrator import CollectionOrchestrator

    dependencies = build_dependencies(
        settings, source_name="mock", provider_name="mock", media_backend="mock"
    )
    orchestrator = CollectionOrchestrator(dependencies)
    result = run(
        orchestrator.collect(
            TaskRequest(
                material="苹果干",
                target_clip_count=1,
                subtitle_policy=SubtitlePolicy.STRICT,
                library_root=settings.paths.library_root,
                source="mock",
                provider="mock",
                media_backend="mock",
            )
        )
    )
    assert result.clips, "the mock workflow must produce a clip"
    clip = result.clips[0]
    assert clip.material == "苹果"
    # Milestone 3.7: the physical category is explicit and independent of the
    # semantic observation, and the state is never invented from the request.
    assert clip.library_category == "苹果干"
    assert clip.material_state in set(MaterialState)
    assert "苹果干" in str(clip.file_path), "the library folder keeps the user material"


# ---------------------------------------------------------------------------
# 30. config precedence
# ---------------------------------------------------------------------------
def test_backend_url_precedence_env_over_config(monkeypatch, tmp_path) -> None:
    # The developer machine may legitimately define this in .env.  Start from
    # a clean process environment so the test can prove config -> env ->
    # preflight precedence deterministically.
    monkeypatch.delenv("DOUYIN_BACKEND_BASE_URL", raising=False)
    settings = load_settings(
        env_path=tmp_path / ".env",
        overrides={"sources": {"douyin": {"base_url": "http://config.invalid:8000"}}}
    )
    assert settings.douyin_base_url() == "http://config.invalid:8000"
    assert settings.douyin_backend_source() == "config"

    monkeypatch.setenv("DOUYIN_BACKEND_BASE_URL", "https://env.example")
    assert settings.douyin_base_url() == "https://env.example"
    assert settings.douyin_backend_source() == "env"

    settings.douyin_backend_override = "https://preflight.example"
    assert settings.douyin_base_url() == "https://preflight.example"
    assert settings.douyin_backend_source() == "preflight"


def test_effective_config_report_never_leaks_secrets(monkeypatch, tmp_path) -> None:
    from core.diagnostics import describe_effective_config

    monkeypatch.setenv("QWEN_API_KEY", "sk-super-secret-value")
    monkeypatch.setenv("DOUYIN_BACKEND_SESSION_COOKIE", "dtk_session=top-secret")
    settings = load_settings(
        overrides={
            "storage": {
                "library_root": str(tmp_path / "library"),
                "database_path": str(tmp_path / "library.db"),
                "cache_root": str(tmp_path / "cache"),
            }
        }
    )
    report = "\n".join(describe_effective_config(settings, detect_browsers=False))
    assert "sk-super-secret-value" not in report
    assert "top-secret" not in report
    assert "qwen configured: yes" in report
    assert str(tmp_path / "library") in report


# ---------------------------------------------------------------------------
# 3. backend selection: local unavailable -> configured remote
# ---------------------------------------------------------------------------
def test_backend_candidates_order(monkeypatch) -> None:
    settings = load_settings(
        overrides={
            "sources": {
                "douyin": {
                    "base_url": "http://127.0.0.1:8000",
                    "fallback_base_urls": ["https://remote.example", "http://127.0.0.1:8000"],
                }
            }
        }
    )
    candidates = backend_candidates(settings)
    assert [candidate.base_url for candidate in candidates] == [
        "http://127.0.0.1:8000",
        "https://remote.example",
    ]
    assert candidates[0].local is True
    assert candidates[1].origin == ORIGIN_FALLBACK


def test_local_backend_unavailable_selects_configured_remote() -> None:
    settings = load_settings(
        overrides={
            "sources": {
                "douyin": {
                    "base_url": "http://127.0.0.1:8000",
                    "fallback_base_urls": ["https://remote.example"],
                }
            }
        }
    )
    probes: list[str] = []

    async def prober(_settings, candidate: BackendCandidate) -> BackendProbe:
        probes.append(candidate.base_url)
        if candidate.local:
            return BackendProbe(
                base_url=candidate.base_url,
                origin=candidate.origin,
                reachable=False,
                detail="connection refused",
            )
        return BackendProbe(
            base_url=candidate.base_url,
            origin=candidate.origin,
            reachable=True,
            authorized=True,
            version="5.0.3",
        )

    selection = run(resolve_douyin_backend(settings, prober=prober))
    assert probes == ["http://127.0.0.1:8000", "https://remote.example"]
    assert selection.usable is True
    assert selection.base_url == "https://remote.example"
    assert selection.origin == ORIGIN_FALLBACK
    assert any("selected remote backend" in line for line in selection.summary_lines())


def test_all_backends_dead_reports_blocked() -> None:
    settings = load_settings(
        overrides={"sources": {"douyin": {"base_url": "http://127.0.0.1:8000"}}}
    )

    async def prober(_settings, candidate: BackendCandidate) -> BackendProbe:
        return BackendProbe(
            base_url=candidate.base_url, origin=candidate.origin, reachable=False
        )

    selection = run(resolve_douyin_backend(settings, prober=prober))
    assert selection.blocked is True
    assert "backend_blocked" in "\n".join(selection.summary_lines())


def test_apply_backend_selection_keeps_the_configured_url_untouched() -> None:
    settings = load_settings(
        overrides={"sources": {"douyin": {"base_url": "http://127.0.0.1:8000"}}}
    )
    from core.backend_resolver import BackendSelection

    selection = BackendSelection(
        selected=BackendProbe(
            base_url="http://127.0.0.1:8000",
            reachable=True,
            authorized=True,
            origin="config",
        )
    )
    apply_backend_selection(settings, selection)
    assert settings.douyin_backend_override is None
    assert settings.douyin_base_url() == "http://127.0.0.1:8000"


# ---------------------------------------------------------------------------
# 6. a dead backend blocks before the collection loop
# ---------------------------------------------------------------------------
def test_dead_backend_blocks_before_collection_loop(settings, monkeypatch) -> None:
    import app as app_module
    from core.backend_resolver import BackendSelection

    started = {"runner": 0}

    class ExplodingRunner:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            started["runner"] += 1
            raise AssertionError("the collection loop must not start")

    async def blocked(*_args: Any, **_kwargs: Any) -> BackendSelection:
        return BackendSelection(reason="no usable dtk backend among [http://127.0.0.1:8000]")

    monkeypatch.setattr("core.backend_resolver.resolve_douyin_backend", blocked, raising=True)
    monkeypatch.setattr(app_module, "TaskRunner", ExplodingRunner, raising=True)

    args = app_module.parse_args(["--douyin-search", "苹果干烘干", "--target", "2"])
    code = app_module.run_task(args, settings)

    assert code == 1
    assert started["runner"] == 0


def test_backend_preflight_runs_before_the_keyword_loop(settings, monkeypatch) -> None:
    import app as app_module
    from core.backend_resolver import BackendSelection

    calls = {"preflight": 0}

    async def selection(*_args: Any, **_kwargs: Any) -> BackendSelection:
        calls["preflight"] += 1
        return BackendSelection(
            selected=BackendProbe(
                base_url="https://remote.example",
                reachable=True,
                authorized=True,
                origin=ORIGIN_FALLBACK,
            )
        )

    class RecordingRunner:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.library = build_library(settings)

        def run(self, request: TaskRequest, **_kwargs: Any):
            from core.models import PipelineResult

            calls["run"] = calls.get("run", 0) + 1
            return PipelineResult(
                task_id=1,
                material=request.material,
                status=TaskStatus.PARTIAL,
                messages=["stub"],
            )

    monkeypatch.setattr("core.backend_resolver.resolve_douyin_backend", selection, raising=True)
    monkeypatch.setattr(app_module, "TaskRunner", RecordingRunner, raising=True)

    args = app_module.parse_args(["--douyin-search", "苹果干烘干", "--target", "2"])
    code = app_module.run_task(args, settings)

    assert calls["preflight"] == 1
    assert calls["run"] == 1
    assert code == 1  # no clips were produced by the stub
    assert settings.douyin_base_url() == "https://remote.example"


# ---------------------------------------------------------------------------
# 7/8. browser configuration alignment + channel selection
# ---------------------------------------------------------------------------
def test_browser_config_is_shared_by_every_entry_point(settings) -> None:
    launch = resolve_browser_config(settings)
    backend = build_browser_search(settings)
    assert backend.profile_dir == launch.profile_dir
    assert (backend.browser_channel or BUNDLED_CHANNEL) == launch.channel_label
    assert backend.browser_executable_path == launch.executable_path
    assert backend.headless == launch.headless

    headful = build_browser_search(settings, headless=False)
    diagnostic = build_browser_search(settings)
    assert headful.profile_dir == diagnostic.profile_dir
    assert headful.browser_channel == diagnostic.browser_channel


def test_browser_channel_auto_prefers_an_installed_channel(tmp_path: Path, monkeypatch) -> None:
    fake_root = tmp_path / "programfiles"
    chrome = fake_root / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome.parent.mkdir(parents=True, exist_ok=True)
    chrome.write_bytes(b"stub")
    env = {"PROGRAMFILES": str(fake_root)}

    assert channel_executable("chrome", env=env) == str(chrome)
    assert detect_installed_channels(env=env) == ("chrome",)

    settings = load_settings()
    settings.sources.douyin.browser_search.browser_channel = "auto"
    launch = resolve_browser_config(settings, env=env)
    assert launch.channel == "chrome"
    assert launch.executable_path == str(chrome)


def test_browser_channel_explicit_and_missing(tmp_path: Path) -> None:
    settings = load_settings()
    browser = settings.sources.douyin.browser_search

    browser.browser_channel = "chromium"
    launch = resolve_browser_config(settings, detect=False)
    assert launch.channel is None
    assert launch.channel_label == BUNDLED_CHANNEL

    browser.browser_channel = "msedge"
    launch = resolve_browser_config(settings, env={})
    assert launch.channel is None, "an uninstalled channel falls back to the bundled browser"
    assert "not installed" in launch.note


def test_browser_diagnostics_record_the_channel(settings) -> None:
    from sources.douyin_browser_search import DouyinBrowserSearchBackend

    backend = build_browser_search(settings)
    diagnostics = backend._base_diagnostics(requested_url="https://example")  # noqa: SLF001
    payload = diagnostics.as_dict()
    assert payload["browser_channel"]
    assert payload["profile_dir"] == str(backend.profile_dir)
    assert payload["headless"] == backend.headless
    assert "cookie" not in " ".join(payload.keys()).lower()


def test_unknown_browser_channel_falls_back_with_a_note(settings) -> None:
    settings.sources.douyin.browser_search.browser_channel = "opera"
    launch = resolve_browser_config(settings, detect=False)
    assert launch.channel is None
    assert "unknown browser_channel" in launch.note


# ---------------------------------------------------------------------------
# 4. zero results vs blocked discovery (CLI wording)
# ---------------------------------------------------------------------------
def test_cli_distinguishes_zero_results_from_blocked(capsys, settings) -> None:
    from app import _print_discovery_outcome
    from core.models import PipelineResult, PipelineStats

    zero = PipelineResult(
        task_id=1,
        material="苹果干",
        status=TaskStatus.PARTIAL,
        stats=PipelineStats(searched_candidates=0),
        discovery_status="no_results",
        discovery_states={"browser": "no_results"},
    )
    _print_discovery_outcome(zero)
    zero_text = capsys.readouterr().out
    assert "0 个结果" in zero_text
    assert "discovery_blocked" not in zero_text

    blocked = PipelineResult(
        task_id=2,
        material="苹果干",
        status=TaskStatus.PARTIAL,
        stats=PipelineStats(),
        discovery_blocked=True,
        discovery_detail="no discovery backend can run",
        discovery_states={"browser": "verification_required"},
    )
    _print_discovery_outcome(blocked)
    blocked_text = capsys.readouterr().out
    assert "discovery_blocked" in blocked_text
    assert "verification_required" in blocked_text


# ---------------------------------------------------------------------------
# miscellaneous guards
# ---------------------------------------------------------------------------
def test_normalize_tagging_is_idempotent() -> None:
    tagging = ClipTagging(material="苹果干")
    once = normalize_tagging(tagging, request_material="苹果干")
    twice = normalize_tagging(once, request_material="苹果干")
    assert once == twice


def test_platform_names_are_not_mistaken_for_material_suffixes() -> None:
    """``果`` is a material character, not a product form suffix."""

    assert material_base("芒果干") == "芒果"
    assert canonical_material("芒果", reference="芒果干") == "芒果"


# ---------------------------------------------------------------------------
# browser classification: a passive anti-bot SDK is not a CAPTCHA
# ---------------------------------------------------------------------------
class _FakeMouse:
    def __init__(self, page: "_FakePage") -> None:
        self.page = page

    async def wheel(self, _x: int, _y: int) -> None:
        self.page.scrolls += 1


class _FakeLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    async def count(self) -> int:
        return self._count


class _FakePage:
    """Minimal Playwright page double (mirrors the M3.5 fixtures)."""

    def __init__(
        self,
        *,
        html: str,
        title: str = "发现更多精彩视频 - 抖音搜索",
        visible_text: str = "",
        has_containers: bool = True,
    ) -> None:
        self.html = html
        self.title_text = title
        self.visible_text = visible_text
        self.url = ""
        self.scrolls = 0
        self.navigations = 0
        self.closed = False
        self.mouse = _FakeMouse(self)
        self._has_containers = has_containers

    async def goto(self, url: str, **_kwargs: Any):
        self.url = url
        self.navigations += 1
        return type("Response", (), {"status": 200})()

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    async def title(self) -> str:
        return self.title_text

    async def content(self) -> str:
        return self.html

    async def inner_text(self, _selector: str) -> str:
        return self.visible_text

    async def eval_on_selector_all(self, selector: str, _script: str) -> list[Any]:
        if "href" in selector:
            import re

            return re.findall(r'href="([^"]+)"', self.html)
        return []

    def locator(self, _selector: str) -> _FakeLocator:
        return _FakeLocator(1 if self._has_containers else 0)

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.page = page

    async def new_page(self) -> _FakePage:
        return self.page

    def set_default_navigation_timeout(self, _ms: int) -> None:
        return None

    async def close(self) -> None:
        return None


def _browser_backend(page: _FakePage, **kwargs: Any):
    from sources.douyin_browser_search import DouyinBrowserSearchBackend

    async def factory() -> _FakeContext:
        return _FakeContext(page)

    payload: dict[str, Any] = {
        "context_factory": factory,
        "headless": True,
        "page_settle_seconds": 0.01,
        "scroll_delay_seconds": 0.0,
        "max_scrolls_per_query": 0,
        "upstream_retry_count": 0,
    }
    payload.update(kwargs)
    return DouyinBrowserSearchBackend(**payload)


#: the shape of the real page: anti-bot SDK present, search UI rendered, but no
#: result links and no rendered challenge
REAL_SEARCH_SHELL_HTML = """
<html><head><title>发现更多精彩视频 - 抖音搜索</title></head><body>
<div data-e2e="scroll-list" class="gZq36zrh"></div>
<iframe src="https://lf-rc1.yhgfb-cn-static.com/obj/rc-verifycenter/rmc-nocaptcha/1.0.0.52/index.html"></iframe>
<script src="https://lf-cdn.sec.bytescm.com/captcha/index.js"></script>
</body></html>
"""


def test_passive_antibot_sdk_is_not_a_verification_wall() -> None:
    """The real 2026 search page loads the SDK without any rendered challenge."""

    from sources.douyin_search import BrowserSearchStatus

    page = _FakePage(
        html=REAL_SEARCH_SHELL_HTML,
        visible_text="精选 推荐 关注 朋友 我的 直播 综合 视频 用户 直播 筛选",
    )
    outcome = run(_browser_backend(page).search("苹果干烘干", 5))
    assert outcome.status != BrowserSearchStatus.VERIFICATION_REQUIRED.value
    assert outcome.status != BrowserSearchStatus.LOGIN_REQUIRED.value
    assert outcome.status == BrowserSearchStatus.SEARCH_DOM_CHANGED.value


def test_rendered_challenge_text_stays_verification_required() -> None:
    from sources.douyin_search import BrowserSearchStatus

    page = _FakePage(
        html="<html><body><div>请完成安全验证</div></body></html>",
        visible_text="请完成安全验证",
        has_containers=False,
    )
    outcome = run(_browser_backend(page).search("苹果干烘干", 5))
    assert outcome.status == BrowserSearchStatus.VERIFICATION_REQUIRED.value


def test_script_only_captcha_payload_is_not_verification_when_ui_rendered() -> None:
    """A captcha *script* inside a rendered search UI is not a challenge."""

    from sources.douyin_search import BrowserSearchStatus

    html = (
        '<html><body><div data-e2e="scroll-list"><a href="/video/7652321152866089979">x</a>'
        "</div><script>var captcha = 'sr-captcha';</script></body></html>"
    )
    page = _FakePage(html=html, visible_text="综合 视频 直播")
    outcome = run(_browser_backend(page).search("苹果干烘干", 5))
    assert outcome.status == BrowserSearchStatus.OK.value
    assert len(outcome.candidates) == 1


def test_browser_stops_after_repeated_empty_results_per_task() -> None:
    """A yield guard: 3 empty searches, then the browser is skipped (not blocked)."""

    from sources.douyin_search import BrowserSearchStatus

    page = _FakePage(html=REAL_SEARCH_SHELL_HTML, visible_text="综合 视频")
    backend = _browser_backend(page, empty_result_limit=3)
    statuses = [run(backend.search(f"苹果干烘干{i}", 5)).status for i in range(3)]
    assert statuses == [BrowserSearchStatus.SEARCH_DOM_CHANGED.value] * 3
    navigations = page.navigations

    skipped = run(backend.search("苹果干烘干4", 5))
    assert skipped.status == BrowserSearchStatus.NO_RESULTS.value
    assert "skipping the remaining keywords" in skipped.detail
    assert page.navigations == navigations, "the browser must not be driven again"
    # it is a yield guard, not a wall: the state never becomes a blocked one
    assert backend.unavailable_reason() == ""


def test_browser_empty_streak_resets_after_a_hit() -> None:
    from sources.douyin_search import BrowserSearchStatus

    page = _FakePage(html=REAL_SEARCH_SHELL_HTML, visible_text="综合")
    backend = _browser_backend(page, empty_result_limit=2)
    run(backend.search("q1", 5))
    page.html = '<html><body><div data-e2e="scroll-list"><a href="/video/7652321152866089979">x</a></div></body></html>'
    hit = run(backend.search("q2", 5))
    assert hit.status == BrowserSearchStatus.OK.value
    assert backend._consecutive_empty == 0  # noqa: SLF001 - the guard resets
