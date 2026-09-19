"""Milestone 8.1: interactive verification gate (same-context human checkpoint).

Everything runs against fake Playwright page/context objects.  No test solves,
replays or automates a real CAPTCHA, and nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

from core.plan_runner import PlanRunResult
from core.plans import PauseReason, PlanStatus
from sources.douyin_browser_search import (
    INTERACTIVE_NOTICE,
    DouyinBrowserSearchBackend,
    SessionCheck,
)
from sources.douyin_search import BrowserSearchStatus


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# fakes (subset of the Playwright API the backend uses)
# ---------------------------------------------------------------------------
CHALLENGE_HTML = """
<html><head><title>验证中间页</title></head><body>
<iframe src="https://rmc.bytedance.com/verifycenter/captcha/v2?subtype=slide"></iframe>
<script src="https://lf-cdn.sec.bytescm.com/captcha/index.js"></script>
</body></html>
"""

VIDEO_HTML = """
<html><body><div data-e2e="scroll-list">
<a href="//www.douyin.com/video/7652321152866089979">一</a>
<a href="/video/7654146961570338534">二</a>
</div></body></html>
"""

EMPTY_HTML = "<html><body><div class='empty'>没有相关结果</div></body></html>"


class FakeMouse:
    def __init__(self, page: "FakePage") -> None:
        self.page = page

    async def wheel(self, _x: int, _y: int) -> None:
        self.page.scrolls += 1


class FakeLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    async def count(self) -> int:
        return self._count


class FakePage:
    """A page whose content/title can be flipped by the "operator"."""

    def __init__(
        self,
        *,
        html: str,
        title: str,
        has_containers: bool = True,
        status: int = 200,
    ) -> None:
        self.html = html
        self.title_text = title
        self.status = status
        self.has_containers = has_containers
        self.navigations = 0
        self.scrolls = 0
        self.url = ""
        self.closed = False
        self.mouse = FakeMouse(self)

    # -- the "human" side of the test --------------------------------------
    def human_completes_challenge(self, *, results: bool = True) -> None:
        if results:
            self.html, self.title_text, self.has_containers = VIDEO_HTML, "苹果干烘干 - 抖音", True
        else:
            # the search UI rendered (hydration finished) but carries no
            # recognisable /video/ links: not usable, and - since M8.2 - not a
            # waiting state either
            self.html, self.title_text, self.has_containers = EMPTY_HTML, "苹果干烘干 - 抖音", True

    # -- page API ----------------------------------------------------------
    async def goto(self, url: str, **_kwargs: Any) -> Any:
        self.url = url
        self.navigations += 1
        return type("Response", (), {"status": self.status})()

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    async def title(self) -> str:
        return self.title_text

    async def content(self) -> str:
        return self.html

    async def eval_on_selector_all(self, selector: str, _script: str) -> list[Any]:
        if "href" in selector:
            return re.findall(r'href="([^"]+)"', self.html)
        return []

    def locator(self, _selector: str) -> FakeLocator:
        return FakeLocator(1 if self.has_containers else 0)

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = False
        self.new_page_calls = 0

    async def new_page(self) -> FakePage:
        self.new_page_calls += 1
        return self.page

    def set_default_navigation_timeout(self, _ms: int) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


def make_backend(
    page: FakePage, context: FakeContext | None = None, **kwargs: Any
) -> DouyinBrowserSearchBackend:
    ctx = context or FakeContext(page)

    async def factory() -> FakeContext:
        return ctx

    payload: dict[str, Any] = {
        "context_factory": factory,
        "headless": False,
        "keep_page_on_challenge": True,
        "page_settle_seconds": 0.01,
        "scroll_delay_seconds": 0.0,
        "max_scrolls_per_query": 2,
        "max_results_per_query": 10,
        "upstream_retry_count": 0,
        "upstream_retry_backoff_seconds": 0.0,
    }
    payload.update(kwargs)
    return DouyinBrowserSearchBackend(**payload)


async def _noop_sleep(_seconds: float) -> None:
    return None


async def _operator_confirms() -> bool:
    return True


# ---------------------------------------------------------------------------
# 12.1 challenge detected -> the page/context survive for the human
# ---------------------------------------------------------------------------
def test_challenge_keeps_the_same_page_and_context_alive() -> None:
    page = FakePage(html=CHALLENGE_HTML, title="验证中间页")
    context = FakeContext(page)
    backend = make_backend(page, context)

    outcome = run(backend.search("苹果干烘干", 5))

    assert outcome.status == BrowserSearchStatus.VERIFICATION_REQUIRED.value
    assert page.closed is False, "the challenged page must stay open for the operator"
    assert context.closed is False, "the persistent context must stay alive"
    assert backend._challenge_page is page
    assert context.new_page_calls == 1


def test_challenged_page_is_closed_when_the_feature_is_off() -> None:
    page = FakePage(html=CHALLENGE_HTML, title="验证中间页")
    backend = make_backend(page, keep_page_on_challenge=False)
    run(backend.search("苹果干烘干", 5))
    assert page.closed is True, "the plain discovery path still tears its pages down"
    assert backend._challenge_page is None


# ---------------------------------------------------------------------------
# 12.2/12.3 same context, no relaunch between verification and re-check
# ---------------------------------------------------------------------------
def test_human_checkpoint_reuses_the_same_page_without_relaunch() -> None:
    page = FakePage(html=CHALLENGE_HTML, title="验证中间页")
    context = FakeContext(page)
    backend = make_backend(page, context)
    run(backend.search("苹果干烘干", 5))
    assert context.new_page_calls == 1

    page.human_completes_challenge()  # the operator solved it in that window
    check = run(
        backend.ensure_interactive_session(
            "苹果干烘干",
            limit=5,
            on_message=lambda _text: None,
            wait_for_operator=_operator_confirms,
            sleep=_noop_sleep,
        )
    )

    assert check.usable is True
    assert check.video_count == 2
    assert context.new_page_calls == 1, "no new page/browser may be launched"
    assert context.closed is False
    assert backend._challenge_page is None, "the kept page is consumed by the re-check"
    assert check.page_url.startswith("https://www.douyin.com/search/")


def test_search_after_human_checkpoint_needs_no_new_context() -> None:
    page = FakePage(html=CHALLENGE_HTML, title="验证中间页")
    context = FakeContext(page)
    backend = make_backend(page, context)

    async def flow() -> None:
        await backend.search("苹果干烘干", 5)
        first_context = backend._context
        page.human_completes_challenge()
        await backend.ensure_interactive_session(
            "苹果干烘干", on_message=lambda _t: None, wait_for_operator=_operator_confirms
        )
        assert backend._context is first_context, "the verified context must be reused"
        # the sticky wall is cleared, so real discovery continues immediately
        outcome = await backend.search("苹果干烘干", 5)
        assert outcome.status == BrowserSearchStatus.OK.value
        assert len(outcome.candidates) == 2
        await backend.close()

    run(flow())
    assert context.closed is True, "closing the session releases the browser"


# ---------------------------------------------------------------------------
# 12.4/12.5/12.6 blocked vs cleared-without-evidence vs usable
# ---------------------------------------------------------------------------
def test_verification_still_present_remains_blocked() -> None:
    page = FakePage(html=CHALLENGE_HTML, title="验证中间页")
    backend = make_backend(page)
    messages: list[str] = []
    check = run(
        backend.ensure_interactive_session(
            "苹果干烘干",
            limit=5,
            on_message=messages.append,
            wait_for_operator=_operator_confirms,
            poll_seconds=0.0,
            max_rounds=2,
            sleep=_noop_sleep,
        )
    )
    assert check.usable is False
    assert check.status == BrowserSearchStatus.VERIFICATION_REQUIRED.value
    assert check.rounds == 2
    assert page.closed is False, "a still-challenged page stays open for another try"
    assert any(INTERACTIVE_NOTICE.splitlines()[0] in text for text in messages)
    assert not any("session_usable" in text for text in messages)


def test_cleared_challenge_without_results_is_not_usable() -> None:
    page = FakePage(html=CHALLENGE_HTML, title="验证中间页")
    backend = make_backend(page)
    run(backend.search("苹果干烘干", 5))
    page.human_completes_challenge(results=False)
    check = run(
        backend.ensure_interactive_session(
            "苹果干烘干",
            on_message=lambda _t: None,
            wait_for_operator=_operator_confirms,
            max_rounds=1,
        )
    )
    assert check.usable is False, "a page without /video/ links is not session_usable"
    assert check.video_ids == []
    assert check.status in {
        BrowserSearchStatus.NO_RESULTS.value,
        BrowserSearchStatus.SEARCH_DOM_CHANGED.value,
    }


def test_cleared_challenge_with_real_links_is_usable() -> None:
    page = FakePage(html=CHALLENGE_HTML, title="验证中间页")
    backend = make_backend(page)
    run(backend.search("苹果干烘干", 5))
    page.human_completes_challenge()
    check = run(
        backend.ensure_interactive_session(
            "苹果干烘干", on_message=lambda _t: None, wait_for_operator=_operator_confirms
        )
    )
    assert check.usable is True
    assert check.video_ids == ["7652321152866089979", "7654146961570338534"]
    lines = "\n".join(check.summary_lines())
    assert "session_usable" in lines
    assert "/video/7652321152866089979" in lines


def test_an_already_usable_session_does_not_ask_the_operator() -> None:
    page = FakePage(html=VIDEO_HTML, title="苹果干烘干 - 抖音")
    backend = make_backend(page)
    called = {"n": 0}

    async def operator() -> bool:
        called["n"] += 1
        return True

    check = run(
        backend.ensure_interactive_session(
            "苹果干烘干", on_message=lambda _t: None, wait_for_operator=operator
        )
    )
    assert check.usable is True
    assert called["n"] == 0, "a usable session must not block on the operator"
    assert check.rounds == 0


def test_gateway_error_is_not_treated_as_a_human_wall() -> None:
    page = FakePage(
        html="<html><body><h1>502 Bad Gateway</h1>kngx</body></html>", title=""
    )
    backend = make_backend(page)
    messages: list[str] = []
    check = run(
        backend.ensure_interactive_session(
            "苹果干烘干",
            on_message=messages.append,
            wait_for_operator=_operator_confirms,
            max_rounds=1,
        )
    )
    assert check.usable is False
    assert check.status == BrowserSearchStatus.UPSTREAM_BAD_GATEWAY.value
    assert not any("人工验证" in text for text in messages), (
        "an upstream gateway page never asks a human to solve a CAPTCHA"
    )


# ---------------------------------------------------------------------------
# 12.7 Ctrl+C cleanup
# ---------------------------------------------------------------------------
def test_ctrl_c_closes_playwright_and_keeps_the_profile(tmp_path: Path) -> None:
    profile = tmp_path / "browser_data" / "douyin"
    profile.mkdir(parents=True)
    marker = profile / "keep-me.txt"
    marker.write_text("profile", encoding="utf-8")
    page = FakePage(html=CHALLENGE_HTML, title="验证中间页")
    context = FakeContext(page)
    backend = make_backend(page, context, profile_dir=profile)
    run(backend.search("苹果干烘干", 5))

    async def cancelled() -> None:
        task = asyncio.ensure_future(
            backend.wait_for_human_verification(
                "苹果干烘干",
                on_message=lambda _t: None,
                poll_seconds=0.05,
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await backend.close()

    run(cancelled())
    assert page.closed is True
    assert context.closed is True
    assert marker.exists(), "the persistent profile must never be deleted"


def test_verify_command_reports_cancellation_without_a_traceback(settings, monkeypatch, capsys):
    import app as app_module

    page = FakePage(html=CHALLENGE_HTML, title="验证中间页")
    context = FakeContext(page)
    backend = make_backend(page, context)

    async def boom(*_args: Any, **_kwargs: Any) -> SessionCheck:
        raise KeyboardInterrupt

    backend.ensure_interactive_session = boom  # type: ignore[assignment]
    monkeypatch.setattr(
        "core.dependencies.build_browser_search", lambda *a, **k: backend
    )
    run(backend.open())  # the browser was already running when Ctrl+C arrived

    usable, lines = run(app_module._verify_douyin_browser(settings, query="苹果干烘干"))
    output = "\n".join(lines)
    assert usable is False
    assert "已取消" in output
    assert "Traceback" not in output
    assert context.closed is True


def test_verify_command_reports_session_usable(settings, monkeypatch) -> None:
    import app as app_module

    page = FakePage(html=VIDEO_HTML, title="苹果干烘干 - 抖音")
    context = FakeContext(page)
    backend = make_backend(page, context)
    monkeypatch.setattr(
        "core.dependencies.build_browser_search", lambda *a, **k: backend
    )

    usable, lines = run(app_module._verify_douyin_browser(settings, query="苹果干烘干"))
    output = "\n".join(lines)
    assert usable is True
    assert "session_usable" in output
    assert "真实抖音搜索结果" in output
    assert context.closed is True


def test_verify_command_reports_blocked_when_still_challenged(settings, monkeypatch) -> None:
    import app as app_module

    page = FakePage(html=CHALLENGE_HTML, title="验证中间页")
    backend = make_backend(page)

    async def blocked(*_args: Any, **_kwargs: Any) -> SessionCheck:
        return SessionCheck(
            usable=False,
            status=BrowserSearchStatus.VERIFICATION_REQUIRED.value,
            detail="Douyin requires a manual slider/verification challenge",
            page_url="https://www.douyin.com/search/x",
            page_title="验证中间页",
        )

    backend.ensure_interactive_session = blocked  # type: ignore[assignment]
    monkeypatch.setattr(
        "core.dependencies.build_browser_search", lambda *a, **k: backend
    )
    usable, lines = run(app_module._verify_douyin_browser(settings, query="苹果干烘干"))
    output = "\n".join(lines)
    assert usable is False
    assert "verification_required" in output
    assert "不会做任何绕过" in output


# ---------------------------------------------------------------------------
# 12.8/12.9 plan state across the human gate
# ---------------------------------------------------------------------------
def _paused_plan(settings):
    """A plan paused on human verification, with real budgets and linkage."""

    from core.dependencies import build_library
    from core.models import (
        ClipRecord,
        MaterialForm,
        MaterialState,
        PipelineResult,
        PipelineStats,
        ProcessStage,
        ReviewStatus,
        SubtitleType,
        TaskRequest,
        TaskStatus,
    )
    from core.plan_runner import PlanRunner
    from core.plan_service import PlanService
    from storage.plans import PlanRepository

    library = build_library(settings)
    service = PlanService(library, settings, repository=PlanRepository(library.database))
    settings.collection_planning.max_items_per_plan = 1
    plan, _action = service.create_plan("苹果干", name="M8.1 人工验证门")
    item = plan.items[0]
    service.edit_item(
        plan.id,
        item.id,
        requested_clips=1,
        max_candidates=8,
        max_downloads=3,
        max_tokens=40000,
        queries=["苹果干烘干", "苹果干制作"],
    )
    assert service.approve(plan.id, note="M8.1").ok

    async def blocked(request: TaskRequest) -> PipelineResult:
        return PipelineResult(
            task_id=901,
            material="苹果干",
            status=TaskStatus.PARTIAL,
            stats=PipelineStats(searched_candidates=0, unique_candidates=0, prescreened=0),
            clips=[],
            ai_usage={"ai_calls": 0, "total_tokens": 0},
            discovery_blocked=True,
            discovery_states={"browser": "verification_required"},
        )

    outcome = run(
        PlanRunner(plan.id, library=library, settings=settings, executor=blocked).run()
    )
    assert outcome.pause_reason == PauseReason.HUMAN_VERIFICATION_REQUIRED.value
    return library, service, plan, item


def test_interactive_run_resumes_the_paused_plan_in_the_same_context(
    settings, monkeypatch
) -> None:
    import app as app_module
    from core.plan_service import PlanService

    library, service, plan, item = _paused_plan(settings)
    before = service.get_plan(plan.id)
    before_tasks = service.repo.linked_task_ids(plan.id)
    page = FakePage(html=VIDEO_HTML, title="苹果干烘干 - 抖音")
    context = FakeContext(page)
    backend = make_backend(page, context)
    monkeypatch.setattr("core.dependencies.build_browser_search", lambda *a, **k: backend)

    captured: dict[str, Any] = {}

    class StubRunner:
        def __init__(self, plan_id: int, **kwargs: Any) -> None:
            captured.update(kwargs)
            captured["plan_id"] = plan_id

        async def run(self) -> PlanRunResult:
            return PlanRunResult(
                plan_id=plan.id, status=PlanStatus.COMPLETED, messages=["stub"]
            )

    monkeypatch.setattr("core.plan_runner.PlanRunner", StubRunner)
    code = app_module.run_collection_plan(settings, plan.id, interactive=True)
    assert code == 0
    assert captured["browser_backend"] is backend, (
        "the plan must run on the already-verified browser session"
    )
    after = service.get_plan(plan.id)
    assert after.status is PlanStatus.RUNNING
    assert after.pause_reason in ("", PauseReason.NONE.value)
    events = [entry["event"] for entry in service.repo.events(plan.id)]
    assert "human_verification_completed" in events
    assert events.index("human_verification_completed") < events.index("resumed")
    # checkpoints and budgets survive the human gate untouched
    assert after.progress.queries_attempted == before.progress.queries_attempted
    assert after.progress.ai_tokens == before.progress.ai_tokens == 0
    assert after.max_downloads == before.max_downloads
    assert after.items[0].max_tokens == before.items[0].max_tokens
    assert service.repo.linked_task_ids(plan.id) == before_tasks
    assert before_tasks, "the blocked attempt already linked a task; the gate keeps it"
    assert context.closed is True, "the browser is closed when the run returns"


def test_explicit_cdp_url_is_injected_without_interactive_gate(settings, monkeypatch) -> None:
    """A normal plan run must not discard the operator's explicit CDP target."""

    import app as app_module

    _library, _service, plan, _item = _paused_plan(settings)
    page = FakePage(html=VIDEO_HTML, title="香菇烘干机 - 抖音")
    context = FakeContext(page)
    backend = make_backend(page, context)
    built: dict[str, Any] = {}

    def fake_build_browser_search(*_args: Any, **kwargs: Any) -> Any:
        built.update(kwargs)
        return backend

    monkeypatch.setattr("core.dependencies.build_browser_search", fake_build_browser_search)
    captured: dict[str, Any] = {}

    class StubRunner:
        def __init__(self, plan_id: int, **kwargs: Any) -> None:
            captured.update(kwargs)
            captured["plan_id"] = plan_id

        async def run(self) -> PlanRunResult:
            return PlanRunResult(
                plan_id=plan.id, status=PlanStatus.COMPLETED, messages=["stub"]
            )

    monkeypatch.setattr("core.plan_runner.PlanRunner", StubRunner)
    code = app_module.run_collection_plan(
        settings,
        plan.id,
        cdp_url="http://127.0.0.1:9230",
    )

    assert code == 0
    assert built["cdp_url"] == "http://127.0.0.1:9230"
    assert captured["browser_backend"] is backend
    assert backend.shared is True


def test_interactive_run_stays_paused_when_verification_fails(settings, monkeypatch) -> None:
    import app as app_module
    from core.plan_service import PlanService

    library, service, plan, item = _paused_plan(settings)
    page = FakePage(html=CHALLENGE_HTML, title="验证中间页")
    backend = make_backend(page)

    async def still_blocked(*_args: Any, **_kwargs: Any) -> SessionCheck:
        return SessionCheck(
            usable=False,
            status=BrowserSearchStatus.VERIFICATION_REQUIRED.value,
            detail="Douyin requires a manual slider/verification challenge",
            page_title="验证中间页",
        )

    backend.ensure_interactive_session = still_blocked  # type: ignore[assignment]
    monkeypatch.setattr("core.dependencies.build_browser_search", lambda *a, **k: backend)
    ran: dict[str, Any] = {}

    class StubRunner:
        """M8.3: the runner object may exist, but it must never be awaited."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            ran["constructed"] = True

        async def run(self) -> Any:  # pragma: no cover - must not be reached
            ran["ran"] = True
            raise AssertionError("the plan must not run while the session is blocked")

    monkeypatch.setattr("core.plan_runner.PlanRunner", StubRunner)

    code = app_module.run_collection_plan(settings, plan.id, interactive=True)
    assert code == 1
    assert "ran" not in ran, "the plan must not run while the session is blocked"
    after = service.get_plan(plan.id)
    assert after.status is PlanStatus.PAUSED
    assert after.pause_reason == PauseReason.HUMAN_VERIFICATION_REQUIRED.value
    events = [entry["event"] for entry in service.repo.events(plan.id)]
    assert "human_verification_failed" in events
    assert "human_verification_completed" not in events


def test_interactive_run_refuses_archived_plans(settings, monkeypatch) -> None:
    import app as app_module
    from core.plan_service import PlanService

    library, service, plan, item = _paused_plan(settings)
    service.archive(plan.id)
    called = {"n": 0}
    monkeypatch.setattr(
        "core.dependencies.build_browser_search",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )
    assert app_module.run_collection_plan(settings, plan.id, interactive=True) == 1
    assert called["n"] == 0, "an archived plan must not even open a browser"


def test_plan_state_is_unchanged_before_the_operator_acts(settings) -> None:
    library, service, plan, item = _paused_plan(settings)
    stored = service.get_plan(plan.id)
    assert stored.status is PlanStatus.PAUSED
    assert stored.pause_reason == PauseReason.HUMAN_VERIFICATION_REQUIRED.value
    assert stored.items[0].max_tokens == 40000
    assert stored.max_downloads == 3
    assert stored.progress.ai_tokens == 0


# ---------------------------------------------------------------------------
# 12.10/12.11 diagnostics + documentation
# ---------------------------------------------------------------------------
def test_check_command_recommends_the_interactive_flow(settings, monkeypatch, capsys) -> None:
    import app as app_module

    page = FakePage(html=CHALLENGE_HTML, title="验证中间页")
    backend = make_backend(page)
    monkeypatch.setattr("core.dependencies.build_browser_search", lambda *a, **k: backend)

    ok, lines = asyncio.run(app_module._douyin_browser_doctor(settings))
    output = "\n".join(lines)
    assert ok is False
    assert "--verify-douyin-browser" in output
    assert "persistent profile exists" in output
    assert "browser config aligned" in output
    assert "non-interactive diagnostic" in output
    assert "--init-douyin-browser" not in output.split("--verify-douyin-browser")[0]


def test_check_command_does_not_wait_for_a_human(settings, monkeypatch) -> None:
    """The diagnostic path stays non-interactive: it must return immediately."""

    import app as app_module

    page = FakePage(html=CHALLENGE_HTML, title="验证中间页")
    backend = make_backend(page)
    waited: list[float] = []

    async def ensure(*_args: Any, **_kwargs: Any) -> SessionCheck:
        raise AssertionError("the diagnostic must not call the interactive gate")

    async def sleep(seconds: float) -> None:
        waited.append(seconds)

    backend.ensure_interactive_session = ensure  # type: ignore[assignment]
    backend.wait_for_human_verification = ensure  # type: ignore[assignment]
    monkeypatch.setattr("core.dependencies.build_browser_search", lambda *a, **k: backend)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    ok, lines = run(app_module._douyin_browser_doctor(settings))
    assert ok is False
    assert waited == [], "the non-interactive check must never sleep/wait"
    assert backend._challenge_page is None


def test_documentation_points_to_the_interactive_flow() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "--verify-douyin-browser" in readme
    assert "--interactive-verification" in readme
    section = readme.split("--init-douyin-browser")[-1]
    assert "不可靠" in section or "not guaranteed" in readme or "不保证" in readme
