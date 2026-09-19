"""Volcano Engine VOD refined subtitle-erase adapter (Milestone 9.8).

Uses the official ``volcengine-python-sdk`` core signer through ``UniversalApi``
(no hand-written request signature logic).  The client is deliberately lazy:
local cleanup and all normal tests work without the SDK or credentials.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import json
import secrets
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import httpx

from core.config import AppSettings, VolcengineVodSettings

LOGGER = logging.getLogger(__name__)


class VolcengineCleanupError(RuntimeError):
    """Base cloud-cleanup error."""


class VolcengineNotConfiguredError(VolcengineCleanupError):
    """Credentials or space/region are missing."""


class VolcengineSdkUnavailableError(VolcengineCleanupError):
    """The official SDK is not installed."""


class VolcengineApiError(VolcengineCleanupError):
    """The official VOD API returned an error."""

    def __init__(self, message: str, *, error_class: str = "api_error") -> None:
        super().__init__(message)
        self.error_class = error_class


@dataclass
class VolcengineExecution:
    """Compact terminal state of one StartExecution/GetExecution run."""

    run_id: str
    status: str
    output_vid: str = ""
    output_file_name: str = ""
    error_class: str = ""
    detail: str = ""
    raw_status: str = ""


@dataclass
class VolcengineReadiness:
    configured: bool = False
    sdk_available: bool = False
    authorized: bool = False
    vod_accessible: bool = False
    subtitle_erase_capability: bool = False
    space: str = ""
    region: str = ""
    space_found: bool = False
    space_region: str = ""
    space_project: str = ""
    erase_api_configured: bool = False
    erase_api_real_call_tested: bool = False
    host: str = ""
    path: str = "/"
    action: str = ""
    version: str = ""
    method: str = ""
    ready: bool = False
    detail: str = ""
    latency_ms: int = 0

    def summary_lines(self) -> list[str]:
        lines = [
            f"[info] volcengine configured: {self.configured}",
            f"[info] official SDK available: {self.sdk_available}",
            f"[info] authorized: {self.authorized}",
            f"[info] VOD accessible: {self.vod_accessible}",
            f"[info] subtitle erase capability: {self.subtitle_erase_capability}",
            f"[info] space: {self.space or '(unset)'} | region: {self.region or '(unset)'}",
            f"[info] space found: {self.space_found}"
            + (f" | returned region: {self.space_region}" if self.space_region else "")
            + (f" | project: {self.space_project}" if self.space_project else ""),
            f"[info] erase_api_configured: {self.erase_api_configured} | "
            f"erase_api_real_call_tested: {self.erase_api_real_call_tested}",
            f"[info] readiness request: {self.method or '-'} "
            f"https://{self.host or 'vod.volcengineapi.com'}{self.path or '/'} "
            f"Action={self.action or '-'} Version={self.version or '-'}",
            f"[info] latency_ms: {self.latency_ms}",
            f"[{'ok' if self.ready else 'warn'}] volcengine cleanup readiness: "
            f"{'ready' if self.ready else self.detail or 'not ready'}",
        ]
        return lines


def sdk_available() -> bool:
    try:
        from volcengine.vod.VodService import VodService  # noqa: F401

        return True
    except Exception:
        return False


class SdkVolcengineVodClient:
    """Official SDK-backed VOD client (subtitle erase only)."""

    name = "volcengine"
    provider = "volcengine"

    #: authoritative Action -> (method, version) mapping for the VOD OpenAPI
    VOD_API_SPECS: dict[str, tuple[str, str]] = {
        "ListSpace": ("GET", "2021-01-01"),
        "ApplyUploadInfo": ("GET", "2022-01-01"),
        "CommitUploadInfo": ("GET", "2022-01-01"),
        "GetPlayInfo": ("GET", "2020-08-01"),
        "StartExecution": ("POST", "2025-01-01"),
        "GetExecution": ("GET", "2025-01-01"),
    }

    @classmethod
    def request_spec(
        cls, action: str, version: str | None = None
    ) -> dict[str, str]:
        """Root-endpoint request contract: ``/`` + Action/Version query."""

        default_method, default_version = cls.VOD_API_SPECS.get(
            action, ("POST", "2025-01-01")
        )
        return {
            "host": "vod.volcengineapi.com",
            "path": "/",
            "method": default_method,
            "action": action,
            "version": version or default_version,
        }

    def __init__(
        self,
        settings: AppSettings,
        *,
        config: VolcengineVodSettings | None = None,
        secrets: Mapping[str, str] | None = None,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        vod_service: Any | None = None,
    ) -> None:
        self.settings = settings
        self.config = config or settings.cloud_cleanup.volcengine
        self.secrets = dict(secrets or settings.secrets or {})
        self._http_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=self.config.request_timeout_seconds)
        )
        self._vod_service = vod_service

    # -- credential resolution (never logged/persisted) --------------------
    def _secret(self, env_name: str) -> str:
        return str(os.environ.get(env_name) or self.secrets.get(env_name) or "")

    @property
    def access_key(self) -> str:
        return self._secret(self.config.access_key_env)

    @property
    def secret_key(self) -> str:
        return self._secret(self.config.secret_key_env)

    @property
    def space(self) -> str:
        return (
            self._secret(self.config.space_env)
            or self.config.space_name
            or ""
        )

    @property
    def region(self) -> str:
        return (
            self._secret(self.config.region_env)
            or self.config.region
            or ""
        )

    @property
    def storage_domain(self) -> str:
        return self._secret(self.config.storage_domain_env).rstrip("/")

    @property
    def url_auth_key(self) -> str:
        return self._secret(self.config.url_auth_key_env)

    @property
    def configured(self) -> bool:
        return bool(
            self.access_key
            and self.secret_key
            and self.space
            and self.region
            and self.config.endpoint
        )

    # -- official VodService / SignerV4 ------------------------------------
    def _vod(self) -> Any:
        if self._vod_service is not None:
            return self._vod_service
        try:
            from volcengine.vod.VodService import VodService
        except Exception as exc:  # pragma: no cover - depends on the environment
            raise VolcengineSdkUnavailableError(
                "official volcengine SDK is not installed; install the optional "
                "dependency or continue using --engine local"
            ) from exc
        service = VodService(self.region or "cn-north-1")
        # the VOD OpenAPI is served from the configured global endpoint
        service.service_info.host = (
            self.config.endpoint.replace("https://", "").replace("http://", "")
            or "vod.volcengineapi.com"
        )
        service.service_info.scheme = (
            "http" if str(self.config.endpoint).startswith("http://") else "https"
        )
        service.service_info.credentials.set_ak(self.access_key)
        service.service_info.credentials.set_sk(self.secret_key)
        self._vod_service = service
        return service

    def _call(self, action: str, body: Mapping[str, Any], *, version: str | None = None) -> dict[str, Any]:
        """Official-signer call on the root VOD endpoint.

        The VOD OpenAPI uses ``/`` plus query parameters ``Action`` and
        ``Version``.  GET actions keep all parameters in the query string;
        POST actions use a JSON body.  No path such as ``/StartExecution`` is
        ever built.
        """

        if not self.configured:
            raise VolcengineNotConfiguredError(
                "Volcano Engine VOD is not configured (AK/SK/space/region missing)"
            )
        service = self._vod()
        from volcengine.ApiInfo import ApiInfo
        from volcengine.auth.SignerV4 import SignerV4

        builtin = service.api_info.get(action)
        spec = self.request_spec(action, version)
        effective_version = spec["version"]
        params = {str(key): value for key, value in body.items() if value is not None}
        if builtin is not None:
            api_info = builtin
            # keep the SDK's authoritative version for built-in actions
            effective_version = str(dict(api_info.query).get("Version") or effective_version)
        else:
            method = spec["method"]
            api_info = ApiInfo(
                method,
                "/",
                {"Action": action, "Version": effective_version},
                {},
                {},
            )
        is_post = str(api_info.method).upper() == "POST"
        # Service.prepare_request always merges its ``params`` argument into
        # the query string, even for POST.  Nested execution JSON must live
        # only in the body or SignerV4 tries to URL-quote dictionaries.
        request = service.prepare_request(api_info, {} if is_post else params)
        if is_post:
            request.headers["Content-Type"] = "application/json"
            request.body = json.dumps(dict(body), ensure_ascii=False)
        SignerV4.sign(request, service.service_info.credentials)
        url = request.build()
        try:
            if is_post:
                response = service.session.post(
                    url,
                    headers=request.headers,
                    data=request.body,
                    timeout=(
                        service.service_info.connection_timeout,
                        service.service_info.socket_timeout,
                    ),
                )
            else:
                response = service.session.get(
                    url,
                    headers=request.headers,
                    timeout=(
                        service.service_info.connection_timeout,
                        service.service_info.socket_timeout,
                    ),
                )
        except Exception as exc:
            raise VolcengineApiError(str(exc)[:300], error_class="api_error") from exc
        status = int(getattr(response, "status_code", 0) or 0)
        text = str(getattr(response, "text", "") or "")
        if status == 404:
            raise VolcengineApiError(
                "transport_404: "
                f"host={service.service_info.host} path=/ "
                f"Action={action} Version={effective_version} method={api_info.method}",
                error_class="transport_404",
            )
        if status in (401, 403):
            raise VolcengineApiError(
                text[:300] or f"HTTP {status}",
                error_class="authentication_failed",
            )
        if status != 200:
            raise VolcengineApiError(
                text[:300] or f"HTTP {status}", error_class="api_error"
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise VolcengineApiError(
                f"invalid JSON response: {text[:160]}", error_class="api_error"
            ) from exc
        if isinstance(payload, Mapping) and payload.get("ResponseMetadata", {}).get("Error"):
            error = payload["ResponseMetadata"]["Error"]
            raise VolcengineApiError(
                str(error.get("Message") or error),
                error_class=str(error.get("Code") or "api_error"),
            )
        return dict(payload or {})

    # -- readiness ---------------------------------------------------------
    @staticmethod
    def _parse_spaces(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        result = payload.get("Result")
        # ListSpace returns the list directly; older/other responses wrap it.
        if isinstance(result, list):
            return [dict(item) for item in result if isinstance(item, Mapping)]
        if isinstance(result, Mapping):
            for key in ("SpaceList", "Spaces", "SpaceInfoList", "SpaceInfos"):
                items = result.get(key)
                if isinstance(items, list):
                    return [dict(item) for item in items if isinstance(item, Mapping)]
        return []

    async def readiness(self) -> VolcengineReadiness:
        import time

        started = time.perf_counter()
        host = self.config.endpoint.replace("https://", "").replace("http://", "")
        readiness = VolcengineReadiness(
            configured=self.configured,
            sdk_available=sdk_available(),
            space=self.space,
            region=self.region,
            host=host,
            path="/",
            action="ListSpace",
            version="2021-01-01",
            method="GET",
            erase_api_configured=bool(self.configured),
        )
        if not readiness.sdk_available:
            readiness.detail = "official volcengine SDK is not installed"
            return readiness
        if not readiness.configured:
            readiness.detail = "credentials/space/region are not configured"
            return readiness
        try:
            candidate_regions = [self.region]
            if self.config.discover_space_region:
                for region in ("cn-north-1", "ap-southeast-1"):
                    if region and region not in candidate_regions:
                        candidate_regions.append(region)
            match: dict[str, Any] | None = None
            matched_region = ""
            configured_probe_error: VolcengineApiError | None = None
            for index, region in enumerate(candidate_regions):
                probe = self
                if region != self.region:
                    probe = SdkVolcengineVodClient(
                        self.settings,
                        config=self.config,
                        secrets={**self.secrets, self.config.region_env: region},
                        http_client_factory=self._http_factory,
                    )
                try:
                    payload = await _to_thread(
                        probe._call,
                        "ListSpace",
                        {"Offset": 0, "Limit": 100},
                        version="2021-01-01",
                    )
                except VolcengineApiError as exc:
                    if index == 0:
                        configured_probe_error = exc
                    continue
                readiness.authorized = True
                readiness.vod_accessible = True
                spaces = self._parse_spaces(payload)
                found = next(
                    (
                        item
                        for item in spaces
                        if str(
                            item.get("SpaceName")
                            or item.get("Name")
                            or item.get("SpaceId")
                            or ""
                        )
                        == self.space
                    ),
                    None,
                )
                if found is not None:
                    match = found
                    matched_region = region
                    break
            if configured_probe_error is not None and match is None:
                raise configured_probe_error
            if not readiness.authorized:
                readiness.detail = "authentication_failed: ListSpace did not succeed"
                readiness.latency_ms = int((time.perf_counter() - started) * 1000)
                return readiness
            if match is None:
                readiness.detail = (
                    f"space_not_found: {self.space!r} not present in ListSpace response"
                )
                readiness.latency_ms = int((time.perf_counter() - started) * 1000)
                return readiness
            readiness.space_found = True
            readiness.space_region = str(
                match.get("Region") or match.get("RegionName") or ""
            )
            readiness.space_project = str(
                match.get("ProjectName") or match.get("Project") or ""
            )
            if (
                readiness.space_region
                and self.region
                and readiness.space_region != self.region
            ):
                readiness.detail = (
                    f"region_mismatch: configured={self.region} "
                    f"returned={readiness.space_region}"
                )
                readiness.latency_ms = int((time.perf_counter() - started) * 1000)
                return readiness
            readiness.subtitle_erase_capability = True
            readiness.ready = True
        except VolcengineApiError as exc:
            readiness.detail = f"{exc.error_class}: {str(exc)[:180]}"
        except Exception as exc:
            readiness.detail = str(exc)[:180]
        readiness.latency_ms = int((time.perf_counter() - started) * 1000)
        return readiness

    # -- upload ------------------------------------------------------------
    @staticmethod
    def _parse_upload_info(payload: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
        """Normalize both legacy and current ApplyUploadInfo envelopes."""

        result = dict(payload.get("Result") or {})
        data = dict(result.get("Data") or {})
        upload = dict(
            data.get("UploadAddress")
            or result.get("UploadAddress")
            or {}
        )
        session_key = str(
            upload.get("SessionKey")
            or data.get("SessionKey")
            or result.get("SessionKey")
            or ""
        )
        return upload, session_key

    async def upload_local(self, path: Path, *, file_name: str | None = None) -> dict[str, str]:
        """Upload one local clip through the official Apply/Commit flow."""

        path = Path(path)
        if not path.exists() or path.stat().st_size <= 0:
            raise VolcengineCleanupError(f"local clip is missing or empty: {path}")
        name = file_name or path.name
        applied = await _to_thread(
            self._call,
            "ApplyUploadInfo",
            {
                "SpaceName": self.space,
                "FileType": "video",
                "FileName": name,
                "FileSize": path.stat().st_size,
            },
        )
        upload, session_key = self._parse_upload_info(applied)
        hosts = list(upload.get("UploadHosts") or [])
        stores = list(upload.get("StoreInfos") or [])
        if not hosts or not stores or not session_key:
            raise VolcengineApiError("ApplyUploadInfo returned no upload address")
        store = dict(stores[0])
        upload_url = f"https://{hosts[0]}/{store.get('StoreUri')}"

        def file_crc32() -> str:
            checksum = 0
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    checksum = zlib.crc32(chunk, checksum)
            return f"{checksum & 0xFFFFFFFF:08x}"

        headers = {
            "Authorization": str(store.get("Auth") or ""),
            "Content-Type": "application/octet-stream",
            # VOD upload hosts require a fixed-length body; without this,
            # httpx uses chunked transfer and the signed upload returns 499.
            "Content-Length": str(path.stat().st_size),
            # Required by the official VOD direct-upload protocol.
            "Content-CRC32": await asyncio.to_thread(file_crc32),
        }

        async def file_chunks():
            with path.open("rb") as handle:
                while True:
                    chunk = await asyncio.to_thread(handle.read, 1024 * 1024)
                    if not chunk:
                        break
                    yield chunk

        async with self._http_factory() as client:
            response = await client.put(
                upload_url,
                headers=headers,
                content=file_chunks(),
            )
            response.raise_for_status()
        committed = await _to_thread(
            self._call,
            "CommitUploadInfo",
            {
                "SpaceName": self.space,
                "SessionKey": session_key,
            },
        )
        committed_result = dict(committed.get("Result") or {})
        vid = str(
            committed_result.get("Vid")
            or (committed_result.get("Data") or {}).get("Vid")
            or ""
        )
        if not vid:
            raise VolcengineApiError("CommitUploadInfo returned no Vid")
        return {"vid": vid, "file_name": name, "input_kind": "upload"}

    # -- subtitle erase execution -----------------------------------------
    @staticmethod
    def build_start_execution_body(
        *,
        space: str,
        vid: str,
        locations: Sequence[Mapping[str, float]] = (),
    ) -> dict[str, Any]:
        """Official StartExecution body; Type is always Subtitle by policy."""

        auto: dict[str, Any] = {
            "Type": "Subtitle",
            "SubtitleFilter": {},
            "Locations": [
                {"RatioLocation": dict(location)} for location in locations
            ],
        }
        return {
            "Input": {"Type": "Vid", "Vid": vid},
            "Operation": {
                "Type": "Task",
                "Task": {
                    "Type": "Erase",
                    "Erase": {
                        "Mode": "Auto",
                        "Auto": auto,
                        "WithEraseInfo": True,
                        "NewVid": True,
                    },
                }
            },
        }

    async def start_subtitle_erase(
        self,
        vid: str,
        *,
        locations: Sequence[Mapping[str, float]] = (),
    ) -> str:
        body = self.build_start_execution_body(
            space=self.space, vid=vid, locations=locations
        )
        result = await _to_thread(self._call, "StartExecution", body)
        run_id = str(dict(result.get("Result") or {}).get("RunId") or "")
        if not run_id:
            raise VolcengineApiError("StartExecution returned no RunId")
        return run_id

    @staticmethod
    def parse_execution(payload: Mapping[str, Any]) -> VolcengineExecution:
        result = dict(payload.get("Result") or {})
        output = dict(result.get("Output") or {})
        task = dict(output.get("Task") or {})
        erase = dict(task.get("Erase") or {})
        output_file = dict(erase.get("File") or {})
        status = str(result.get("Status") or "")
        return VolcengineExecution(
            run_id=str(result.get("RunId") or ""),
            status=status,
            raw_status=status,
            output_vid=str(
                output_file.get("Vid")
                or erase.get("Vid")
                or erase.get("OutputVid")
                or ""
            ),
            output_file_name=str(
                output_file.get("FileName")
                or erase.get("FileName")
                or erase.get("OutputFileName")
                or ""
            ),
            error_class=str(result.get("ErrorCode") or erase.get("Code") or ""),
            detail=str(result.get("Message") or erase.get("Message") or "")[:300],
        )

    async def get_execution(self, run_id: str) -> VolcengineExecution:
        payload = await _to_thread(self._call, "GetExecution", {"RunId": run_id})
        return self.parse_execution(payload)

    async def poll_execution(
        self,
        run_id: str,
        *,
        sleep: Callable[[float], Any] | None = None,
    ) -> VolcengineExecution:
        import asyncio
        import time

        sleeper = sleep or asyncio.sleep
        deadline = time.monotonic() + max(1.0, float(self.config.max_poll_seconds))
        terminal = {"Success", "Failed", "Failure", "Error", "Cancelled"}
        while True:
            execution = await self.get_execution(run_id)
            if execution.status in terminal:
                return execution
            if time.monotonic() >= deadline:
                execution.error_class = execution.error_class or "timeout"
                execution.detail = execution.detail or "bounded polling timeout"
                return execution
            await sleeper(max(0.1, float(self.config.poll_interval_seconds)))

    # -- result download ---------------------------------------------------
    @staticmethod
    def build_type_a_url(
        domain: str,
        file_path: str,
        key: str,
        *,
        expires_at: int,
        rand: str,
        uid: str = "0",
    ) -> str:
        """Build a Volcano VOD type-A URL without logging its secret."""

        path = "/" + str(file_path).lstrip("/")
        source = f"{path}-{int(expires_at)}-{rand}-{uid}-{key}"
        digest = hashlib.md5(
            source.encode("utf-8"), usedforsecurity=False
        ).hexdigest()
        auth_key = f"{int(expires_at)}-{rand}-{uid}-{digest}"
        return f"{str(domain).rstrip('/')}{path}?auth_key={auth_key}"

    def _signed_storage_url(self, output_file_name: str) -> str:
        if not self.storage_domain or not self.url_auth_key or not output_file_name:
            return ""
        return self.build_type_a_url(
            self.storage_domain,
            output_file_name,
            self.url_auth_key,
            expires_at=int(time.time()) + int(self.config.url_auth_ttl_seconds),
            rand=secrets.token_hex(8),
        )

    def _play_url(self, output_vid: str) -> str:
        payload = self._call("GetPlayInfo", {"Vid": output_vid})
        result = dict(payload.get("Result") or {})
        items = list(result.get("PlayInfoList") or [])
        if not items:
            raise VolcengineApiError("GetPlayInfo returned no playable output")
        item = dict(items[0])
        url = str(item.get("MainPlayUrl") or item.get("PlayUrl") or "")
        if not url:
            raise VolcengineApiError("GetPlayInfo returned no playable URL")
        return url

    async def download_output(
        self,
        output_vid: str,
        dest: Path,
        *,
        output_file_name: str = "",
    ) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if output_file_name.startswith("http"):
            url = output_file_name
        else:
            url = self._signed_storage_url(output_file_name)
            if not url:
                url = await _to_thread(self._play_url, output_vid)
        async with self._http_factory() as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                with dest.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        handle.write(chunk)
        if not dest.exists() or dest.stat().st_size <= 0:
            raise VolcengineCleanupError("downloaded cloud output is empty")
        return dest


async def _to_thread(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    import asyncio

    return await asyncio.to_thread(function, *args, **kwargs)
