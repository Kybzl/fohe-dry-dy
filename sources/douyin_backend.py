"""HTTP client for the self-hosted Douyin backend.

Implemented against **Douyin_TikTok_Download_API (dtk) v5.0.3** -- see
``docs/douyin_backend.md`` and the upstream OpenAPI document.  Contract facts
this client relies on (all verified against `/openapi.json` of v5.0.3):

* every JSON answer is ``{success, data, error, meta}``; branch on
  ``error.code``, never on ``error.message``
* authentication is an API key in ``X-API-Key`` (or ``Authorization: Bearer``)
* data endpoints are **asynchronous by default**: a submit answers ``202``
  with ``{task_id, state}``; poll ``GET /api/v1/tasks/{task_id}`` until
  ``state`` is ``done``/``failed``.  On a finished task the payload sits at
  ``body.data.data``, and a failed task is still HTTP 200.
* list endpoints page with an opaque ``cursor`` and report ``has_more``
* ``?wait=<seconds>`` (max 30) makes a submit block server-side

The API key is never logged, never stored and never echoed back in errors.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from media.ffmpeg import DEFAULT_REMOTE_USER_AGENT

LOGGER = logging.getLogger(__name__)

#: upstream project + version this client was written against
UPSTREAM_PROJECT = "Evil0ctal/Douyin_TikTok_Download_API"
UPSTREAM_API_VERSION = "v5.0.3"
UPSTREAM_API_PREFIX = "/api/v1"

#: statuses that are worth retrying (section 34)
TRANSIENT_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: statuses that mean "the upstream/gateway is unusable right now"
TRANSIENT_UPSTREAM_STATUSES = frozenset({500, 502, 503, 504})

#: host names that mean "this machine": no environment proxy may be used
LOOPBACK_HOSTS: frozenset[str] = frozenset(
    {"127.0.0.1", "localhost", "::1", "0.0.0.0", "[::1]"}
)


def is_loopback_url(url: str) -> bool:
    """True when ``url`` points at the local machine."""

    host = (urlparse(url).hostname or "").lower()
    return host in LOOPBACK_HOSTS


class DouyinBackendError(RuntimeError):
    """Base class for backend failures (message is always sanitized)."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


class DouyinBackendUnavailable(DouyinBackendError):
    """The backend is not reachable (connection refused, DNS, timeout)."""


class DouyinBackendAuthError(DouyinBackendError):
    """The backend answered 401/403: the API key is missing or refused."""


class DouyinBackendRateLimited(DouyinBackendError):
    """The backend asked us to slow down (HTTP 429)."""


class DouyinBackendTaskError(DouyinBackendError):
    """The asynchronous task ended in ``failed``."""


class DouyinBackendSchemaError(DouyinBackendError):
    """The answer did not match the documented envelope/contract."""


@dataclass
class BackendCapabilities:
    """What the configured backend can actually do (section 6/41)."""

    base_url: str = ""
    reachable: bool = False
    authorized: bool = False
    version: str = ""
    commit: str = ""
    account: str = ""
    scopes: list[str] = field(default_factory=list)
    search_endpoint: str | None = None
    content_read: bool = False
    archive_search: bool = False
    media_read: bool = False
    task_support: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def keyword_search(self) -> bool:
        return bool(self.search_endpoint)

    def summary_lines(self) -> list[str]:
        lines: list[str] = []
        if not self.reachable:
            lines.append(f"[warn] Douyin backend not running ({self.base_url})")
            lines.extend(f"[warn] {note}" for note in self.notes)
            return lines
        lines.append(f"[ok] Douyin backend reachable ({self.base_url})")
        lines.append(
            f"[{'ok' if self.authorized else 'warn'}] API authentication "
            f"{'accepted' if self.authorized else 'missing/refused'}"
            + (f" (account={self.account})" if self.account else "")
        )
        if self.version:
            lines.append(f"[ok] backend version: {self.version}" + (f" commit={self.commit}" if self.commit else ""))
        lines.append(
            f"[{'ok' if self.content_read else 'warn'}] content endpoint "
            f"{'available' if self.content_read else 'unavailable'}"
        )
        lines.append(
            f"[{'ok' if self.archive_search else 'warn'}] archive search (q=) "
            f"{'available' if self.archive_search else 'unavailable'}"
        )
        lines.append(
            f"[{'ok' if self.task_support else 'warn'}] async task endpoint "
            f"{'available' if self.task_support else 'unavailable'}"
        )
        if self.keyword_search:
            lines.append(f"[ok] keyword search endpoint: {self.search_endpoint}")
        else:
            lines.append(
                "[warn] keyword search endpoint: NOT provided by this backend version"
            )
        lines.extend(f"[warn] {note}" for note in self.notes)
        return lines


@dataclass
class BackendCapabilityState:
    """Result of the one-shot capability probe (cached per client)."""

    probed: bool = False
    available: bool | None = None
    keyword_search: bool = False
    keyword_search_endpoint: str | None = None
    archive_supported: bool = False
    content_supported: bool = False
    task_supported: bool = False
    http_status: int | None = None
    status: str = "unknown"
    detail: str = ""

    @property
    def viable_for_search(self) -> bool:
        """Whether any *server side* search/read path can still be used."""

        return bool(self.available and (self.keyword_search or self.archive_supported))

    def as_dict(self) -> dict[str, Any]:
        return {
            "probed": self.probed,
            "available": self.available,
            "keyword_search": self.keyword_search,
            "archive_supported": self.archive_supported,
            "http_status": self.http_status,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class TaskResult:
    """Outcome of a (possibly asynchronous) backend call."""

    state: str
    data: Any = None
    error: Mapping[str, Any] | None = None
    task_id: str | None = None
    request_id: str = ""
    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.state == "done" and self.error is None


class DouyinBackendClient:
    """Thin, contract-checked HTTP client for the dtk backend."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        session_cookie: str = "",
        timeout: float = 30.0,
        max_retries: int = 2,
        backoff_seconds: float = 1.5,
        task_wait_seconds: float = 20.0,
        task_poll_interval_seconds: float = 1.0,
        max_task_wait_seconds: float = 90.0,
        concurrency: int = 2,
        user_agent: str = DEFAULT_REMOTE_USER_AGENT,
        search_endpoint_candidates: list[str] | None = None,
        client_factory: Callable[[], Any] | None = None,
        trust_env: bool | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self._api_key = api_key or ""
        #: ``dtk_session`` value for instances that authenticate with a console
        #: session instead of an API key (value never logged or persisted)
        self._session_cookie = (session_cookie or "").strip()
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.backoff_seconds = backoff_seconds
        self.task_wait_seconds = max(0.0, min(task_wait_seconds, 30.0))
        self.task_poll_interval_seconds = max(0.2, task_poll_interval_seconds)
        self.max_task_wait_seconds = max(self.task_poll_interval_seconds, max_task_wait_seconds)
        self.user_agent = user_agent
        self.search_endpoint_candidates = list(
            search_endpoint_candidates or ["/api/v1/douyin/search"]
        )
        # Environment proxies must never be used for a locally hosted backend:
        # ``trust_env=None`` means "auto" - off for loopback, on elsewhere.
        self.trust_env = trust_env
        self._client_factory = client_factory
        self._client: Any = None
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._openapi_cache: dict[str, Any] | None = None
        self._capabilities = BackendCapabilityState()
        self._unavailable_reason: str = ""
        self.request_count = 0

    # -- environment proxy policy -----------------------------------------
    @property
    def uses_environment_proxy(self) -> bool:
        """Whether httpx may pick up ``HTTP_PROXY``/``HTTPS_PROXY``."""

        if self.trust_env is not None:
            return bool(self.trust_env)
        return not is_loopback_url(self.base_url)

    # -- plumbing ----------------------------------------------------------
    def _get_client(self) -> Any:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:  # pragma: no cover - real network path
                import httpx

                self._client = httpx.AsyncClient(
                    timeout=self.timeout,
                    headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                    # a self-hosted backend on this machine must not be routed
                    # through a system/environment proxy
                    trust_env=self.uses_environment_proxy,
                )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            close = getattr(self._client, "aclose", None)
            if close is not None:
                await close()
            self._client = None

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}" if self.base_url else path

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._api_key:
            # Documented key header; the bearer form is equivalent but sending
            # the secret in one header only keeps the surface smaller.
            headers["X-API-Key"] = self._api_key
        elif self._session_cookie:
            cookie = self._session_cookie
            if "=" not in cookie:
                cookie = f"dtk_session={cookie}"
            headers["Cookie"] = cookie
        return headers

    def _sanitize(self, text: object) -> str:
        """Remove the API key from any text that could reach a log or report."""

        message = str(text or "")
        if self._api_key and self._api_key in message:
            message = message.replace(self._api_key, "<redacted>")
        if self._session_cookie and self._session_cookie in message:
            message = message.replace(self._session_cookie, "<redacted>")
        return message[:400]

    @property
    def authenticated(self) -> bool:
        """True when any credential is configured (never reveals its value)."""

        return bool(self._api_key or self._session_cookie)

    # -- raw request -------------------------------------------------------
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        raw: bool = False,
        force: bool = False,
    ) -> tuple[int, Any]:
        """Perform one HTTP call with bounded retries for transient failures."""

        if not force and self._capabilities.available is False:
            # the backend already failed this task: fail fast instead of
            # repeating the whole retry cycle for every keyword
            raise DouyinBackendUnavailable(
                self._unavailable_reason or "Douyin backend marked unavailable",
                retryable=True,
            )
        client = self._get_client()
        url = self._url(path)
        query = {k: v for k, v in (params or {}).items() if v is not None}
        attempt = 0
        while True:
            attempt += 1
            try:
                async with self._semaphore:
                    self.request_count += 1
                    response = await client.request(
                        method,
                        url,
                        params=query or None,
                        json=dict(json_body) if json_body is not None else None,
                        headers=self._headers(),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = DouyinBackendUnavailable(
                    f"Douyin backend unreachable at {self.base_url}: {self._sanitize(exc)}",
                    retryable=True,
                )
                if attempt >= self.max_retries:
                    raise error from exc
                await asyncio.sleep(self.backoff_seconds * attempt)
                continue

            status = int(getattr(response, "status_code", 0))
            if status in TRANSIENT_STATUSES and attempt < self.max_retries:
                retry_after = self._retry_after(response)
                delay = retry_after if retry_after is not None else self.backoff_seconds * attempt
                LOGGER.warning(
                    "backend %s %s -> HTTP %s; retrying in %.1fs (attempt %s/%s)",
                    method,
                    path,
                    status,
                    delay,
                    attempt,
                    self.max_retries,
                )
                await asyncio.sleep(max(0.1, delay))
                continue
            if raw:
                try:
                    return status, self._decode(response)
                except DouyinBackendSchemaError:
                    # a gateway/proxy answering with text (e.g. "502 Bad
                    # Gateway") is still a status the caller can classify
                    return status, {"raw_text": self._sanitize(getattr(response, "text", ""))[:200]}
            if status >= 400:
                try:
                    return status, self._envelope(response, status, method, path)
                except DouyinBackendSchemaError:
                    raise self._status_error(status, method, path, getattr(response, "text", ""))
            return status, self._envelope(response, status, method, path)

    def _status_error(
        self,
        status: int,
        method: str,
        path: str,
        body: object,
    ) -> DouyinBackendError:
        """Classify a failure whose body was not a dtk envelope.

        This is the common real-world case of a gateway (or a non-dtk service)
        answering on the configured port with plain text such as
        ``502 Bad Gateway``.
        """

        snippet = self._sanitize(str(body or "").strip())[:120]
        detail = f"{method} {path} failed: HTTP {status} {snippet}".strip()
        if status in TRANSIENT_UPSTREAM_STATUSES:
            self.mark_unavailable(status=status, path=path)
            detail = f"{detail}{self._local_backend_hint(status)}"
        if status in (401, 403):
            return DouyinBackendAuthError(detail, status=status)
        if status == 429:
            return DouyinBackendRateLimited(detail, status=status, retryable=True)
        return DouyinBackendError(
            detail, status=status, retryable=status in TRANSIENT_STATUSES
        )

    @staticmethod
    def _retry_after(response: Any) -> float | None:
        header = None
        headers = getattr(response, "headers", None)
        if headers:
            header = headers.get("Retry-After") or headers.get("retry-after")
        try:
            return float(header) if header is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _decode(response: Any) -> Any:
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError):
            text = getattr(response, "text", "")
            raise DouyinBackendSchemaError(
                f"backend returned a non-JSON answer: {str(text)[:120]}"
            )

    def _envelope(self, response: Any, status: int, method: str, path: str) -> dict[str, Any]:
        """Validate the documented envelope and raise on failures."""

        body = self._decode(response)
        if not isinstance(body, dict) or "success" not in body or "data" not in body:
            raise DouyinBackendSchemaError(
                f"unexpected backend answer for {method} {path} (no envelope keys)"
            )
        error = body.get("error")
        if body.get("success") is True and error is None:
            return body

        code = ""
        message = ""
        retryable = status in TRANSIENT_STATUSES
        retry_after = None
        if isinstance(error, dict):
            code = str(error.get("code") or "")
            message = self._sanitize(error.get("message") or "")
            retry_after = error.get("retry_after")
            retryable = bool(error.get("retryable", retryable))
        detail = f"{method} {path} failed: HTTP {status} {code} {message}".strip()
        if status in TRANSIENT_UPSTREAM_STATUSES:
            self.mark_unavailable(status=status, path=path)
            detail = f"{detail}{self._local_backend_hint(status)}"

        if status in (401, 403) or code in ("UNAUTHENTICATED", "FORBIDDEN", "INVALID_API_KEY"):
            raise DouyinBackendAuthError(detail, code=code, status=status)
        if status == 429 or code in ("RATE_LIMITED", "TOO_MANY_REQUESTS"):
            raise DouyinBackendRateLimited(
                detail,
                code=code,
                status=status,
                retryable=True,
                retry_after=float(retry_after) if retry_after else None,
            )
        raise DouyinBackendError(detail, code=code, status=status, retryable=retryable)

    def _local_backend_hint(self, status: int) -> str:
        """Actionable hint when a *local* backend answers a gateway error."""

        if not is_loopback_url(self.base_url):
            return ""
        if status in (502, 503, 504):
            return (
                f" | local Douyin backend returned HTTP {status}; verify that dtk is "
                "actually running on this port and that localhost traffic is not "
                "being routed through a system proxy"
            )
        return ""

    # -- availability ------------------------------------------------------
    def mark_unavailable(self, *, status: int | None = None, path: str = "") -> None:
        """Remember that the backend is unusable for the rest of this task."""

        if self._capabilities.available is False:
            return
        reason = f"backend unavailable"
        if status is not None:
            reason = f"backend unavailable (HTTP {status})"
        if path:
            reason = f"{reason} at {path}"
        self._capabilities.available = False
        self._capabilities.status = "backend_unavailable"
        self._capabilities.http_status = status
        self._capabilities.detail = self._sanitize(reason)
        self._unavailable_reason = self._capabilities.detail
        LOGGER.warning(
            "douyin backend marked unavailable for this task: %s", self._capabilities.detail
        )

    @property
    def available(self) -> bool:
        """False once a transient failure exhausted its retries (fast fail)."""

        return self._capabilities.available is not False

    @property
    def unavailable_reason(self) -> str:
        return self._unavailable_reason

    @property
    def capabilities(self) -> BackendCapabilityState:
        return self._capabilities

    # -- asynchronous task model -------------------------------------------
    async def submit(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        use_wait: bool = True,
        poll_timeout: float | None = None,
    ) -> TaskResult:
        """Submit work and return the result, polling when the backend defers.

        ``200`` -> immediate result; ``202`` -> task id -> poll until it settles.
        """

        query = dict(params or {})
        if use_wait and self.task_wait_seconds > 0:
            query.setdefault("wait", int(self.task_wait_seconds))
        status, body = await self.request(method, path, params=query, json_body=json_body)
        data = body.get("data") or {}
        meta = body.get("meta") or {}
        request_id = str(meta.get("request_id") or "")

        if status == 200:
            return TaskResult(state="done", data=data, request_id=request_id, meta=meta)
        if status == 202:
            task_id = str(data.get("task_id") or "")
            if not task_id:
                raise DouyinBackendSchemaError(
                    f"backend answered 202 without a task id for {method} {path}"
                )
            return await self.wait_for_task(
                task_id, timeout=poll_timeout, request_id=request_id
            )
        raise DouyinBackendError(
            f"unexpected status {status} for {method} {path}", status=status
        )

    async def wait_for_task(
        self,
        task_id: str,
        *,
        timeout: float | None = None,
        interval: float | None = None,
        request_id: str = "",
    ) -> TaskResult:
        """Poll ``GET /api/v1/tasks/{task_id}`` until it settles (section 7).

        Bounded in both time and attempts; a failed task carries an explicit
        ``retryable`` flag which is surfaced to the caller.
        """

        deadline = asyncio.get_event_loop().time() + (
            timeout if timeout is not None else self.max_task_wait_seconds
        )
        delay = interval or self.task_poll_interval_seconds
        while True:
            status, body = await self.request("GET", f"{UPSTREAM_API_PREFIX}/tasks/{task_id}")
            payload = body.get("data") or {}
            state = str(payload.get("state") or "unknown")
            meta = body.get("meta") or {}
            if state == "done":
                return TaskResult(
                    state="done",
                    data=payload.get("data"),
                    task_id=task_id,
                    request_id=str(meta.get("request_id") or request_id),
                    meta=payload.get("result_meta") or {},
                )
            if state == "failed":
                error = payload.get("error") or {}
                code = str(error.get("code") or "")
                message = self._sanitize(error.get("message") or "")
                raise DouyinBackendTaskError(
                    f"backend task {task_id} failed: {code} {message}".strip(),
                    code=code,
                    retryable=bool(error.get("retryable", False)),
                    retry_after=float(error["retry_after"]) if error.get("retry_after") else None,
                )
            if state not in ("queued", "running", "pending", "unknown"):
                raise DouyinBackendSchemaError(
                    f"backend task {task_id} reported an unknown state: {state!r}"
                )
            if asyncio.get_event_loop().time() >= deadline:
                raise DouyinBackendTaskError(
                    f"backend task {task_id} did not finish within the wait budget",
                    retryable=True,
                )
            await asyncio.sleep(delay)

    # -- capabilities / health ---------------------------------------------
    async def probe_capabilities(self, *, refresh: bool = False) -> BackendCapabilityState:
        """Probe ``/openapi.json`` **once per client** and cache the outcome.

        Success *and* failure are cached, so a backend that answered HTTP 502
        is not probed again for every search keyword (the reliability bug this
        fixes).  A transient failure still gets the normal bounded retry
        sequence inside :meth:`request`.
        """

        if self._capabilities.probed and not refresh:
            return self._capabilities

        self._capabilities.probed = True
        try:
            status, body = await self.request("GET", "/openapi.json", raw=True, force=True)
        except DouyinBackendError as exc:
            self.mark_unavailable(
                status=getattr(exc, "status", None) or self._capabilities.http_status,
                path="/openapi.json",
            )
            self._capabilities.detail = self._sanitize(exc)
            return self._capabilities

        self._capabilities.http_status = status
        if status != 200 or not isinstance(body, dict):
            self.mark_unavailable(status=status, path="/openapi.json")
            return self._capabilities

        self._openapi_cache = body
        paths = body.get("paths") if isinstance(body.get("paths"), dict) else {}
        self._capabilities.available = True
        self._capabilities.status = "ok"
        self._capabilities.content_supported = any(
            "platform" in path and path.endswith("/video") for path in paths
        )
        self._capabilities.archive_supported = f"{UPSTREAM_API_PREFIX}/archive" in paths
        self._capabilities.task_supported = any(
            path.startswith(f"{UPSTREAM_API_PREFIX}/tasks") for path in paths
        )
        self._capabilities.keyword_search_endpoint = self._match_search_endpoint(paths)
        self._capabilities.keyword_search = bool(self._capabilities.keyword_search_endpoint)
        self._capabilities.detail = (
            f"openapi v{body.get('info', {}).get('version', '?')} "
            f"keyword_search={self._capabilities.keyword_search} "
            f"archive={self._capabilities.archive_supported}"
        )
        LOGGER.info("douyin backend capabilities: %s", self._capabilities.detail)
        return self._capabilities

    def _match_search_endpoint(self, paths: Mapping[str, Any]) -> str | None:
        """Find a keyword-search route in an OpenAPI ``paths`` mapping."""

        for candidate in self.search_endpoint_candidates:
            if candidate in paths:
                methods = {method.lower() for method in paths[candidate]}
                if "get" in methods or "post" in methods:
                    return candidate
        for path in paths:
            lowered = path.lower()
            if "search" in lowered and "douyin" in lowered and "{" not in path:
                return path
        return None

    async def openapi_document(self, *, refresh: bool = False) -> dict[str, Any]:
        """Cached OpenAPI document (probing once); never raises."""

        await self.probe_capabilities(refresh=refresh)
        return self._openapi_cache or {}

    async def discover_search_endpoint(self, *, refresh: bool = False) -> str | None:
        """Keyword-search route, or ``None`` (uses the cached probe)."""

        state = await self.probe_capabilities(refresh=refresh)
        return state.keyword_search_endpoint

    async def health(self, *, deep: bool = False) -> BackendCapabilities:
        """Connectivity + auth + capability check (sections 6 and 41)."""

        capabilities = BackendCapabilities(base_url=self.base_url)

        # 1. process probe: /healthz is unauthenticated and outside the envelope
        try:
            status, _ = await self.request("GET", "/healthz", raw=True, force=True)
            capabilities.reachable = status == 200
        except DouyinBackendError as exc:
            capabilities.notes.append(self._sanitize(exc))
        if not capabilities.reachable:
            try:
                status, _ = await self.request("GET", "/readyz", raw=True, force=True)
                capabilities.reachable = status == 200
            except DouyinBackendError as exc:
                capabilities.notes.append(self._sanitize(exc))

        # 2. authentication: /auth/me is the cheapest key check
        try:
            status, body = await self.request("GET", f"{UPSTREAM_API_PREFIX}/auth/me", force=True)
            payload = body.get("data") or {}
            capabilities.authorized = status == 200
            capabilities.account = str(
                payload.get("username") or payload.get("account") or ""
            )
            scopes = payload.get("scopes") or []
            capabilities.scopes = [str(item) for item in scopes]
        except DouyinBackendAuthError:
            capabilities.authorized = False
            capabilities.notes.append(
                "backend answered 401/403: set the API key (see .env.example)"
            )
        except DouyinBackendError as exc:
            capabilities.notes.append(self._sanitize(exc))

        # 3. version, when the key may read it
        if capabilities.authorized:
            try:
                status, body = await self.request(
                    "GET", f"{UPSTREAM_API_PREFIX}/system/status", force=True
                )
                payload = body.get("data") or {}
                capabilities.version = str(payload.get("version") or "")
                capabilities.commit = str(payload.get("commit") or "")
            except DouyinBackendError as exc:
                capabilities.notes.append(self._sanitize(exc))

        # 4. capability map from the backend's own OpenAPI document
        document = await self.openapi_document(refresh=deep)
        paths = document.get("paths") if isinstance(document, dict) else {}
        if not paths:
            capabilities.notes.append(
                "could not read /openapi.json, so capabilities could not be verified"
            )
        else:
            capabilities.content_read = f"{UPSTREAM_API_PREFIX}/{{platform}}/video" in paths or any(
                "platform" in path and path.endswith("/video") for path in paths
            )
            capabilities.archive_search = f"{UPSTREAM_API_PREFIX}/archive" in paths
            capabilities.media_read = f"{UPSTREAM_API_PREFIX}/downloads" in paths
            capabilities.task_support = any(
                path.startswith(f"{UPSTREAM_API_PREFIX}/tasks") for path in paths
            )
            capabilities.search_endpoint = await self.discover_search_endpoint()

        if not capabilities.keyword_search:
            capabilities.notes.append(
                f"{UPSTREAM_PROJECT} {UPSTREAM_API_VERSION} exposes no keyword search "
                "endpoint; discovery uses archive search, author/mix seeds or manual URLs"
            )
        # the same probe failure can be recorded several times (healthz, readyz,
        # auth): keep the report readable
        capabilities.notes = list(dict.fromkeys(capabilities.notes))
        return capabilities

    # -- documented endpoints ----------------------------------------------
    async def content_detail(
        self,
        *,
        aweme_id: str | None = None,
        url: str | None = None,
        platform: str = "douyin",
        include_raw: bool = False,
    ) -> TaskResult:
        """``GET /api/v1/{platform}/video`` -- one post, video or image album."""

        if not aweme_id and not url:
            raise ValueError("content_detail needs an aweme_id or a url")
        return await self.submit(
            "GET",
            f"{UPSTREAM_API_PREFIX}/{platform}/video",
            params={"aweme_id": aweme_id, "url": url, "include_raw": include_raw},
        )

    async def user_posts(
        self,
        *,
        sec_user_id: str | None = None,
        url: str | None = None,
        cursor: str | None = None,
        count: int = 20,
        platform: str = "douyin",
    ) -> TaskResult:
        """``GET /api/v1/{platform}/user/posts`` -- paged author post list."""

        if not sec_user_id and not url:
            raise ValueError("user_posts needs a sec_user_id or a url")
        return await self.submit(
            "GET",
            f"{UPSTREAM_API_PREFIX}/{platform}/user/posts",
            params={"sec_user_id": sec_user_id, "url": url, "cursor": cursor, "count": count},
        )

    async def mix_posts(
        self,
        *,
        mix_id: str,
        cursor: str | None = None,
        count: int = 20,
        platform: str = "douyin",
    ) -> TaskResult:
        """``GET /api/v1/{platform}/mix/posts`` -- posts inside a mix/playlist."""

        return await self.submit(
            "GET",
            f"{UPSTREAM_API_PREFIX}/{platform}/mix/posts",
            params={"mix_id": mix_id, "cursor": cursor, "count": count},
        )

    async def archive_search(
        self,
        *,
        q: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
        platform: str = "douyin",
    ) -> TaskResult:
        """``GET /api/v1/archive`` -- search what the instance already collected."""

        return await self.submit(
            "GET",
            f"{UPSTREAM_API_PREFIX}/archive",
            params={"q": q, "cursor": cursor, "limit": limit, "platform": platform},
        )

    async def keyword_search(
        self,
        *,
        query: str,
        cursor: str | None = None,
        limit: int = 20,
        platform: str = "douyin",
    ) -> TaskResult:
        """Call the keyword-search route **only if the backend exposes one**."""

        endpoint = await self.discover_search_endpoint()
        if not endpoint:
            raise DouyinBackendSchemaError(
                "backend exposes no keyword search endpoint "
                f"({UPSTREAM_PROJECT} {UPSTREAM_API_VERSION})"
            )
        return await self.submit(
            "GET",
            endpoint,
            params={"keyword": query, "q": query, "cursor": cursor, "count": limit},
        )

    async def parse_url(self, url: str) -> TaskResult:
        """``POST /api/v1/parse`` -- resolve any supported share link."""

        return await self.submit(
            "POST", f"{UPSTREAM_API_PREFIX}/parse", json_body={"url": url}
        )
