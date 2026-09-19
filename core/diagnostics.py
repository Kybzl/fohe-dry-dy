"""Effective configuration report (Milestone 3.6, section 30).

Configuration arrives from four places -- code defaults, ``config.yaml``,
``.env`` and CLI overrides -- and hidden precedence bugs are expensive.  This
module prints what the running process will actually use, without ever echoing
a secret.
"""

from __future__ import annotations

from core.backend_resolver import backend_candidates
from core.browser_config import (
    BUNDLED_CHANNEL,
    detect_installed_channels,
    resolve_browser_config,
)
from core.config import AppSettings


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


#: operations whose effective model is worth showing (Milestone 8.3.2)
QWEN_ROUTING_OPERATIONS: tuple[str, ...] = (
    "preview_filter",
    "segment_detection",
    "clip_tagging",
)


def _model_source(settings: AppSettings, field: str) -> str:
    """Which layer supplies one Qwen model name (never a secret)."""

    configured = settings.ai.provider_option("qwen", field, "")
    if configured:
        return f"config ai.qwen.{field}"
    if settings.secret("QWEN_VISION_MODEL"):
        return "env QWEN_VISION_MODEL"
    return "built-in default"


def describe_qwen_routing(settings: AppSettings) -> list[str]:
    """Effective Qwen model per operation, from the *real* provider object.

    ``--check-config`` used to print one ambiguous model name (env first), which
    hid the fact that ``preview_filter`` and the analysis operations resolve
    through different configuration fields.  This prints what the pipeline will
    actually send, plus which layer supplied each name.
    """

    from core.dependencies import build_provider

    try:
        provider = build_provider(settings, name="qwen")
    except Exception as exc:  # pragma: no cover - defensive
        return [f"[warn] qwen routing unavailable: {type(exc).__name__}: {exc}"]

    lines = ["[info] qwen routing (effective, per operation):"]
    from ai.qwen import DEFAULT_MODEL as QWEN_DEFAULT_MODEL

    env_model = settings.secret("QWEN_VISION_MODEL")
    default_model = env_model or QWEN_DEFAULT_MODEL
    source = "env QWEN_VISION_MODEL" if env_model else "built-in default"
    lines.append(f"[info]   {'default':<18}{default_model}   ({source})")
    for operation in QWEN_ROUTING_OPERATIONS:
        try:
            model = provider.model_for(operation)
        except Exception as exc:  # pragma: no cover - defensive
            lines.append(f"[warn]   {operation:<18}unavailable ({type(exc).__name__})")
            continue
        if operation == "preview_filter":
            origin = _model_source(settings, "preview_model")
        else:
            origin = _model_source(settings, "analysis_model")
        lines.append(f"[info]   {operation:<18}{model or '(unset)'}   ({origin})")
    fallback_model = getattr(provider, "fallback_model", "") or ""
    lines.append(
        f"[info]   {'escalation':<18}{fallback_model or '(same as operation)'}"
    )
    return lines


def describe_effective_config(settings: AppSettings, *, detect_browsers: bool = True) -> list[str]:
    """Effective values from defaults + config.yaml + .env + CLI, no secrets."""

    douyin = settings.sources.douyin
    base_url = settings.douyin_base_url()
    origin = settings.douyin_backend_source()
    candidates = backend_candidates(settings)
    local = "local" if any(candidate.local for candidate in candidates) else "remote"
    browser = resolve_browser_config(settings, detect=detect_browsers)

    qwen_key = bool(settings.secret("QWEN_API_KEY"))
    # the effective model is per operation; show the preview one here and the
    # full routing below (the old single value was env-first and misleading)
    qwen_model = (
        settings.ai.provider_option("qwen", "preview_model", "")
        or settings.secret("QWEN_VISION_MODEL")
        or "(default)"
    )

    lines = [
        f"[info] config file: {settings.project_root / 'config.yaml'}",
        f"[info] python project root: {settings.project_root}",
        "[info] --- effective values (no secrets are shown) ---",
        f"[info] douyin backend base url: {base_url or '(unset)'} (from {origin})",
        f"[info] backend kind: {local}"
        + (f", trust_env={douyin.trust_env}" if douyin.trust_env is not None else ", trust_env=auto"),
        f"[info] backend candidates: "
        + (", ".join(f"{c.base_url} ({c.origin})" for c in candidates) or "(none)"),
        f"[info] configured local backend: {douyin.base_url or '(unset)'}",
        f"[info] backend session cookie configured: {_yes_no(bool(settings.douyin_session_cookie()))}",
        f"[info] backend api key configured: {_yes_no(bool(settings.douyin_api_key()))}",
        f"[info] browser profile: {browser.profile_dir}",
        f"[info] browser channel: {browser.channel_label} "
        f"(requested={browser.requested_channel})",
        f"[info] browser executable: {browser.executable_path or '(playwright managed)'}",
        f"[info] browser headless: {browser.headless}",
        f"[info] browser search enabled: {_yes_no(douyin.browser_search.enabled)}",
        f"[info] qwen configured: {_yes_no(qwen_key)} (model={qwen_model})",
        f"[info] volcano configured: {_yes_no(bool(settings.secret('VOLCANO_API_KEY')))}",
        f"[info] ai provider: {settings.ai.active_provider}"
        + (
            f" (fallback={settings.ai.fallback_provider})"
            if settings.ai.fallback_provider
            else ""
        ),
        f"[info] output root: {settings.paths.library_root}",
        f"[info] database: {settings.paths.database}",
        f"[info] cache: {settings.paths.cache_dir}",
        f"[info] media backend: {settings.media.backend}",
    ]
    if qwen_key:
        lines.extend(describe_qwen_routing(settings))
    if detect_browsers:
        installed = detect_installed_channels()
        lines.append(
            f"[info] installed browser channels: {', '.join(installed) or '(none detected)'}"
            f" (bundled {BUNDLED_CHANNEL} always available)"
        )
    return lines
