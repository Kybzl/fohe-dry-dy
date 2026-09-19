"""Milestone 9.8 mocked tests: Volcano Engine refined subtitle erase."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from analyzers.subtitle_analysis import SubtitleAnalysisSettings, SubtitleAnalyzer
from core.cloud_cleanup import (
    CLOUD_CLEANUP_ENGINE,
    CLOUD_CLEANUP_VERSION,
    CloudCleanupService,
    detect_solid_caption_backing,
)
from core.dependencies import build_library
from core.models import ReviewStatus
from core.production_ready import ProductionReadyService
from core.subtitle_cleanup import SubtitleCleanupService
from core.subtitle_cleanup_models import CleanupStatus
from core.subtitle_cleanup_models import CleanupMask
from media.volcengine_vod import (
    SdkVolcengineVodClient,
    VolcengineApiError,
    VolcengineExecution,
)
from tests.test_m9_2_subtitle_cleanup import (
    FakeEngine,
    FakeToolkit,
    ScriptedDetector,
    _add_clip,
    _region,
)
from tests.test_m9_4_production_integration import _set_analysis


def run(coro):
    return asyncio.run(coro)


class CloudFakeToolkit(FakeToolkit):
    async def probe(self, path: Path):
        if path.name == "cloud_result.mp4":
            if self.probe_candidate_error:
                raise RuntimeError("corrupt cloud output")
            from media.ffmpeg import MediaInfo

            return MediaInfo(
                duration=self.after_duration,
                width=self.after_width,
                height=self.after_height,
                fps=self.fps,
                codec="h264",
                audio_codec="aac",
                has_audio=True,
            )
        return await super().probe(path)


class FakeCloudClient:
    name = "volcengine"
    provider = "volcengine"

    def __init__(
        self,
        *,
        ready: bool = True,
        source: Path | None = None,
        execution_status: str = "Success",
    ) -> None:
        self.ready = ready
        self.source = source
        self.execution_status = execution_status
        self.uploads: list[Path] = []
        self.starts: list[tuple[str, list[dict[str, float]]]] = []
        self.polls = 0
        self.downloads = 0

    async def readiness(self):
        return SimpleNamespace(ready=self.ready, detail="" if self.ready else "not ready")

    async def upload_local(self, path: Path, *, file_name: str | None = None):
        self.uploads.append(Path(path))
        return {"vid": "vid-input-1", "file_name": file_name or Path(path).name, "input_kind": "upload"}

    async def start_subtitle_erase(self, vid: str, *, locations=()):
        self.starts.append((vid, [dict(item) for item in locations]))
        return "run-0001"

    async def poll_execution(self, run_id: str):
        self.polls += 1
        return VolcengineExecution(
            run_id=run_id,
            status=self.execution_status,
            output_vid="vid-output-1" if self.execution_status == "Success" else "",
            output_file_name="cloud_output.mp4",
            error_class="" if self.execution_status == "Success" else "execution_failed",
        )

    async def download_output(self, output_vid: str, dest: Path, *, output_file_name=""):
        self.downloads += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.source is not None:
            shutil.copyfile(self.source, dest)
        else:
            dest.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 64)
        return dest


class FakeApiError(Exception):
    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _configured_client(
    settings,
    *,
    payload: dict | None = None,
    error: Exception | None = None,
) -> SdkVolcengineVodClient:
    settings.secrets = {
        "VOLCENGINE_ACCESS_KEY_ID": "fake-ak",
        "VOLCENGINE_SECRET_ACCESS_KEY": "fake-sk",
        "VOLCENGINE_VOD_SPACE_NAME": "test-vod-space",
        "VOLCENGINE_REGION": "cn-beijing",
    }
    client = SdkVolcengineVodClient(settings)
    calls: list[dict] = []

    def fake_call(action: str, body, *, version: str | None = None):
        calls.append({"action": action, "body": body, "version": version})
        if error is not None:
            raise error
        return dict(payload or {})

    client._call = fake_call  # type: ignore[assignment]
    client.fake_calls = calls  # type: ignore[attr-defined]
    return client


def _cloud_service(
    settings,
    *,
    detector: ScriptedDetector | None = None,
    toolkit: CloudFakeToolkit | None = None,
    client: FakeCloudClient | None = None,
) -> tuple[CloudCleanupService, ScriptedDetector, CloudFakeToolkit, FakeCloudClient]:
    settings.cloud_cleanup.enabled = True
    library = build_library(settings)
    detector = detector or ScriptedDetector(before=[_region()], after=[])
    toolkit = toolkit or CloudFakeToolkit()
    client = client or FakeCloudClient()
    local = SubtitleCleanupService(
        library,
        settings,
        analyzer=SubtitleAnalyzer(
            SubtitleAnalysisSettings(detector=detector, max_frames=6)
        ),
        toolkit=toolkit,
        engine=FakeEngine(),
    )
    return (
        CloudCleanupService(library, settings, client=client, local_service=local),
        detector,
        toolkit,
        client,
    )


def _clip(service, settings, *, index: int = 1, classification: str = "top_simple"):
    clip_id = _add_clip(service.library, settings.paths.library_root, index=index)
    _set_analysis(service.library, clip_id, classification)
    return clip_id


# ---------------------------------------------------------------------------
# Readiness / credentials
# ---------------------------------------------------------------------------
def test_volcengine_credentials_missing(settings) -> None:
    settings.secrets = {}
    client = SdkVolcengineVodClient(settings)
    readiness = run(client.readiness())
    assert readiness.configured is False
    assert readiness.ready is False
    assert "credential" in readiness.detail or "configured" in readiness.detail


def test_volcengine_readiness_success(settings) -> None:
    settings.secrets = {
        "VOLCENGINE_ACCESS_KEY_ID": "fake-ak",
        "VOLCENGINE_SECRET_ACCESS_KEY": "fake-sk",
        "VOLCENGINE_VOD_SPACE_NAME": "space-a",
        "VOLCENGINE_REGION": "cn-north-1",
    }
    client = SdkVolcengineVodClient(settings)
    assert client.configured is True
    assert client.space == "space-a" and client.region == "cn-north-1"


def test_apply_upload_info_current_response_envelope() -> None:
    upload, session_key = SdkVolcengineVodClient._parse_upload_info(
        {
            "Result": {
                "Data": {
                    "UploadAddress": {
                        "UploadHosts": ["upload.example.com"],
                        "StoreInfos": [{"StoreUri": "store/path", "Auth": "token"}],
                        "SessionKey": "session-current",
                    }
                }
            }
        }
    )

    assert upload["UploadHosts"] == ["upload.example.com"]
    assert session_key == "session-current"


def test_apply_upload_info_legacy_response_envelope() -> None:
    upload, session_key = SdkVolcengineVodClient._parse_upload_info(
        {
            "Result": {
                "UploadAddress": {
                    "UploadHosts": ["legacy.example.com"],
                    "StoreInfos": [{"StoreUri": "store/path", "Auth": "token"}],
                },
                "SessionKey": "session-legacy",
            }
        }
    )

    assert upload["UploadHosts"] == ["legacy.example.com"]
    assert session_key == "session-legacy"


def test_list_space_uses_root_endpoint_and_2021_version(settings) -> None:
    client = _configured_client(
        settings,
        payload={
            "Result": {
                "SpaceList": [
                    {
                        "SpaceName": "test-vod-space",
                        "Region": "cn-beijing",
                        "ProjectName": "default",
                    }
                ]
            }
        },
    )
    readiness = run(client.readiness())
    assert readiness.ready is True
    assert readiness.authorized is True
    assert readiness.vod_accessible is True
    assert readiness.space_found is True
    assert readiness.space_region == "cn-beijing"
    assert readiness.erase_api_configured is True
    assert readiness.erase_api_real_call_tested is False
    spec = SdkVolcengineVodClient.request_spec("ListSpace")
    assert spec == {
        "host": "vod.volcengineapi.com",
        "path": "/",
        "method": "GET",
        "action": "ListSpace",
        "version": "2021-01-01",
    }


def test_list_space_space_missing(settings) -> None:
    settings.cloud_cleanup.volcengine.discover_space_region = False
    client = _configured_client(settings, payload={"Result": {"SpaceList": []}})
    readiness = run(client.readiness())
    assert readiness.authorized is True
    assert readiness.vod_accessible is True
    assert readiness.space_found is False
    assert readiness.ready is False
    assert "space_not_found" in readiness.detail


def test_list_space_region_mismatch(settings) -> None:
    client = _configured_client(
        settings,
        payload={
            "Result": {
                "SpaceList": [
                    {
                        "SpaceName": "test-vod-space",
                        "Region": "cn-shanghai",
                    }
                ]
            }
        },
    )
    readiness = run(client.readiness())
    assert readiness.ready is False
    assert readiness.space_found is True
    assert readiness.space_region == "cn-shanghai"
    assert "region_mismatch" in readiness.detail


def test_transport_404_is_not_authentication_failure(settings) -> None:
    client = _configured_client(
        settings,
        error=VolcengineApiError(
            "transport_404: host=vod.volcengineapi.com path=/ Action=ListSpace "
            "Version=2021-01-01 method=GET",
            error_class="transport_404",
        ),
    )
    readiness = run(client.readiness())
    assert readiness.ready is False
    assert "transport_404" in readiness.detail
    assert "authentication_failed" not in readiness.detail


def test_start_and_get_execution_use_2025_version(settings) -> None:
    client = _configured_client(settings)

    def fake_call(action, body, *, version=None):
        if action == "StartExecution":
            return {"Result": {"RunId": "run-9"}}
        return {
            "Result": {
                "RunId": "run-9",
                "Status": "Success",
                "Output": {"Task": {"Erase": {"Vid": "vid-out"}}},
            }
        }

    client._call = fake_call  # type: ignore[assignment]
    run_id = run(client.start_subtitle_erase("vid-1"))
    assert run_id == "run-9"
    start_spec = SdkVolcengineVodClient.request_spec("StartExecution")
    assert start_spec["method"] == "POST"
    assert start_spec["version"] == "2025-01-01"
    assert start_spec["path"] == "/"
    execution = run(client.get_execution("run-9"))
    assert execution.status == "Success"
    get_spec = SdkVolcengineVodClient.request_spec("GetExecution")
    assert get_spec["method"] == "GET"
    assert get_spec["version"] == "2025-01-01"
    assert get_spec["path"] == "/"


def test_parse_execution_reads_current_nested_output_file() -> None:
    execution = SdkVolcengineVodClient.parse_execution(
        {
            "Result": {
                "RunId": "hb:run-current",
                "Status": "Success",
                "Output": {
                    "Task": {
                        "Erase": {
                            "File": {
                                "Vid": "vid-current",
                                "FileName": "cleaned.mp4",
                            }
                        }
                    }
                },
            }
        }
    )

    assert execution.output_vid == "vid-current"
    assert execution.output_file_name == "cleaned.mp4"


def test_type_a_url_signature_matches_protocol() -> None:
    url = SdkVolcengineVodClient.build_type_a_url(
        "https://storage.example.com/",
        "/path/video.mp4",
        "test-secret",
        expires_at=1700000000,
        rand="abc123",
    )
    expected = hashlib.md5(
        b"/path/video.mp4-1700000000-abc123-0-test-secret"
    ).hexdigest()
    assert url == (
        "https://storage.example.com/path/video.mp4?auth_key="
        f"1700000000-abc123-0-{expected}"
    )


def test_readiness_diagnostics_never_show_secret(settings) -> None:
    settings.cloud_cleanup.volcengine.discover_space_region = False
    settings.secrets = {
        "VOLCENGINE_ACCESS_KEY_ID": "AK-SECRET-VALUE",
        "VOLCENGINE_SECRET_ACCESS_KEY": "SK-SECRET-VALUE",
        "VOLCENGINE_VOD_SPACE_NAME": "space-a",
        "VOLCENGINE_REGION": "cn-north-1",
    }
    client = SdkVolcengineVodClient(settings)
    client._call = lambda *args, **kwargs: {}  # type: ignore[assignment]
    readiness = run(client.readiness())
    text = "\n".join(readiness.summary_lines())
    assert "AK-SECRET-VALUE" not in text
    assert "SK-SECRET-VALUE" not in text


# ---------------------------------------------------------------------------
# Request shape / locations / policy
# ---------------------------------------------------------------------------
def test_start_execution_body_is_subtitle_only() -> None:
    body = SdkVolcengineVodClient.build_start_execution_body(
        space="space-a",
        vid="vid-1",
        locations=[
            {
                "TopLeftX": 0.1,
                "TopLeftY": 0.8,
                "BottomRightX": 0.9,
                "BottomRightY": 0.9,
            }
        ],
    )
    assert body["Operation"]["Type"] == "Task"
    task = body["Operation"]["Task"]
    erase = task["Erase"]
    assert task["Type"] == "Erase"
    assert erase["Mode"] == "Auto"
    assert erase["Auto"]["Type"] == "Subtitle"
    assert erase["Auto"]["SubtitleFilter"] == {}
    assert erase["WithEraseInfo"] is True and erase["NewVid"] is True
    assert erase["Auto"]["Locations"][0]["RatioLocation"]["BottomRightX"] == 0.9
    assert "Text" not in str(body)


def test_ratio_location_mapping_keeps_conservative_margin() -> None:
    from core.subtitle_cleanup_models import CleanupMask

    locations = CloudCleanupService._ratio_locations(
        [CleanupMask(x1=0.2, y1=0.8, x2=0.8, y2=0.9)], margin=0.01
    )
    assert locations == [
        {
            "TopLeftX": 0.19,
            "TopLeftY": 0.79,
            "BottomRightX": 0.81,
            "BottomRightY": 0.91,
        }
    ]


def test_cloud_cleanup_disabled_never_uploads(settings) -> None:
    service, _detector, _toolkit, client = _cloud_service(settings)
    service.settings.cloud_cleanup.enabled = False
    clip_id = _clip(service, settings)
    outcome = run(service.cleanup_clip(clip_id))
    assert outcome.status is CleanupStatus.INELIGIBLE
    assert client.uploads == []


def test_human_rejected_clip_is_never_submitted(settings) -> None:
    service, _detector, _toolkit, client = _cloud_service(settings)
    clip_id = _clip(service, settings)
    service.library.set_review(
        [clip_id], status=ReviewStatus.REJECTED, note="moving watermark"
    )
    outcome = run(service.cleanup_clip(clip_id))
    assert outcome.status is CleanupStatus.INELIGIBLE
    assert "clip_human_rejected" in outcome.reason
    assert client.uploads == []


def test_watermark_class_is_never_submitted(settings) -> None:
    detector = ScriptedDetector(before=[_region(text="特价优惠")], after=[])
    service, _detector, _toolkit, client = _cloud_service(settings, detector=detector)
    clip_id = _clip(service, settings, classification="promotional_overlay")
    outcome = run(service.cleanup_clip(clip_id))
    assert outcome.status is CleanupStatus.INELIGIBLE
    assert "classification_not_eligible" in outcome.reason
    assert client.uploads == []


# ---------------------------------------------------------------------------
# Successful cloud flow
# ---------------------------------------------------------------------------
def test_cloud_cleanup_success_persists_run_and_auto_approval(settings) -> None:
    service, _detector, _toolkit, client = _cloud_service(settings)
    clip_id = _clip(service, settings)
    outcome = run(service.cleanup_clip(clip_id))
    assert outcome.status is CleanupStatus.SUCCEEDED, outcome.lines()
    assert outcome.output_path is not None and Path(outcome.output_path).exists()
    assert len(client.uploads) == 1
    assert client.starts[0][0] == "vid-input-1"
    assert client.starts[0][1]  # bounded Locations were submitted
    assert client.downloads == 1
    record = service.library.subtitle_cleanup(clip_id, CLOUD_CLEANUP_VERSION)
    assert record is not None
    assert record["version"] == CLOUD_CLEANUP_VERSION
    assert record["engine"] == CLOUD_CLEANUP_ENGINE
    assert record["provider"] == "volcengine"
    assert record["run_id"] == "run-0001"
    assert record["input_vid"] == "vid-input-1"
    assert record["cloud_output_vid"] == "vid-output-1"
    assert record["review_status"] == "approved"
    assert record["before_metrics"]
    assert record["after_metrics"] is None


def test_cloud_cleanup_is_idempotent(settings) -> None:
    service, _detector, _toolkit, client = _cloud_service(settings)
    clip_id = _clip(service, settings)
    first = run(service.cleanup_clip(clip_id))
    second = run(service.cleanup_clip(clip_id))
    assert first.status is CleanupStatus.SUCCEEDED
    assert second.status is CleanupStatus.SUCCEEDED and second.reused is True
    assert len(client.uploads) == 1


def test_original_clip_is_immutable(settings) -> None:
    service, _detector, _toolkit, _client = _cloud_service(settings)
    clip_id = _clip(service, settings)
    original = Path(service.library.get_clip(clip_id).file_path)
    before = hashlib.sha256(original.read_bytes()).hexdigest()
    run(service.cleanup_clip(clip_id))
    after = hashlib.sha256(original.read_bytes()).hexdigest()
    assert before == after


def test_cloud_version_is_separate_from_v1(settings) -> None:
    service, _detector, _toolkit, _client = _cloud_service(settings)
    clip_id = _clip(service, settings)
    # create a v1 record as well
    v1 = run(service.local_service.cleanup_clip(clip_id))
    assert v1.status is CleanupStatus.SUCCEEDED
    v2 = run(service.cleanup_clip(clip_id))
    assert v2.status is CleanupStatus.SUCCEEDED
    v1_record = service.library.subtitle_cleanup(clip_id)
    v2_record = service.library.subtitle_cleanup(clip_id, CLOUD_CLEANUP_VERSION)
    assert v1_record is not None and v2_record is not None
    assert v1_record["version"] != v2_record["version"]
    assert v1_record["engine"] != v2_record["engine"]


def test_approved_v2_derivative_is_preferred(settings) -> None:
    service, _detector, _toolkit, _client = _cloud_service(settings)
    clip_id = _clip(service, settings)
    outcome = run(service.cleanup_clip(clip_id))
    ok, message = service.local_service.review_cleanup(
        clip_id, status="approved", version=CLOUD_CLEANUP_VERSION
    )
    assert ok, message
    clip = service.library.get_clip(clip_id)
    assert service.library.preferred_media_path(clip) == Path(outcome.output_path)


def test_rejected_v2_derivative_falls_back_to_original(settings) -> None:
    service, _detector, _toolkit, _client = _cloud_service(settings)
    clip_id = _clip(service, settings)
    run(service.cleanup_clip(clip_id))
    ok, _message = service.local_service.review_cleanup(
        clip_id,
        status="rejected",
        failure_class="residual_subtitle",
        version=CLOUD_CLEANUP_VERSION,
    )
    assert ok
    clip = service.library.get_clip(clip_id)
    assert service.library.preferred_media_path(clip) == Path(clip.file_path)


# ---------------------------------------------------------------------------
# Failure / residual / polling
# ---------------------------------------------------------------------------
def test_cloud_execution_failure_is_failed_processing(settings) -> None:
    client = FakeCloudClient(execution_status="Failed")
    service, _detector, _toolkit, _client = _cloud_service(settings, client=client)
    clip_id = _clip(service, settings)
    outcome = run(service.cleanup_clip(clip_id))
    assert outcome.status is CleanupStatus.FAILED_PROCESSING
    record = service.library.subtitle_cleanup(clip_id, CLOUD_CLEANUP_VERSION)
    assert record is not None and record["cloud_status"] == "Failed"


def test_download_failure_preserves_cloud_recovery_identifiers(settings) -> None:
    class DownloadFailingClient(FakeCloudClient):
        async def download_output(
            self, output_vid: str, dest: Path, *, output_file_name=""
        ):
            self.downloads += 1
            raise RuntimeError("temporary download failure")

    client = DownloadFailingClient()
    service, _detector, _toolkit, _client = _cloud_service(settings, client=client)
    clip_id = _clip(service, settings)

    outcome = run(service.cleanup_clip(clip_id))

    assert outcome.status is CleanupStatus.FAILED_PROCESSING
    assert outcome.run_id == "run-0001"
    record = service.library.subtitle_cleanup(clip_id, CLOUD_CLEANUP_VERSION)
    assert record is not None
    assert record["run_id"] == "run-0001"
    assert record["input_vid"] == "vid-input-1"
    assert record["cloud_output_vid"] == "vid-output-1"
    assert record["cloud_output_file_name"] == "cloud_output.mp4"
    assert record["cloud_status"] == "Success"
    assert record["cloud_error_class"] == "RuntimeError"


def test_download_failure_resumes_without_second_paid_task(settings) -> None:
    class FailOnceClient(FakeCloudClient):
        def __init__(self) -> None:
            super().__init__()
            self.fail_download = True

        async def download_output(
            self, output_vid: str, dest: Path, *, output_file_name=""
        ):
            if self.fail_download:
                self.downloads += 1
                self.fail_download = False
                raise RuntimeError("temporary download failure")
            return await super().download_output(
                output_vid, dest, output_file_name=output_file_name
            )

    client = FailOnceClient()
    service, _detector, _toolkit, _client = _cloud_service(settings, client=client)
    clip_id = _clip(service, settings)

    first = run(service.cleanup_clip(clip_id))
    second = run(service.cleanup_clip(clip_id))

    assert first.status is CleanupStatus.FAILED_PROCESSING
    assert second.status is CleanupStatus.SUCCEEDED
    assert second.reason == "resumed_completed_cloud_output"
    assert len(client.uploads) == 1
    assert len(client.starts) == 1
    assert client.polls == 1
    assert client.downloads == 2


def test_adopt_downloaded_output_validates_without_paid_task(settings) -> None:
    service, _detector, _toolkit, client = _cloud_service(settings)
    clip_id = _clip(service, settings)
    candidate = settings.paths.cache_dir / "adopt" / "cloud_result.mp4"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 64)

    outcome = run(
        service.adopt_downloaded_output(
            clip_id,
            candidate,
            run_id="run-existing",
            input_vid="vid-existing-input",
            output_vid="vid-existing-output",
            output_file_name="existing-output.mp4",
        )
    )

    assert outcome.status is CleanupStatus.SUCCEEDED
    assert client.uploads == []
    assert client.starts == []
    assert client.polls == 0
    assert client.downloads == 0
    record = service.library.subtitle_cleanup(clip_id, CLOUD_CLEANUP_VERSION)
    assert record is not None
    assert record["review_status"] == "approved"
    assert record["run_id"] == "run-existing"
    assert record["input_vid"] == "vid-existing-input"
    assert record["cloud_output_vid"] == "vid-existing-output"
    assert Path(record["output_path"]).exists()


def test_cloud_preflight_can_pass_without_upload_or_paid_task(settings) -> None:
    service, _detector, _toolkit, client = _cloud_service(settings)
    clip_id = _clip(service, settings)

    outcome = run(service.preflight_clip(clip_id))

    assert outcome.status is CleanupStatus.PENDING
    assert outcome.reason == "eligible_for_cloud_submission"
    assert outcome.masks
    assert client.uploads == []
    assert client.starts == []
    assert service.library.subtitle_cleanup(clip_id, CLOUD_CLEANUP_VERSION) is None


def test_solid_caption_backing_is_detected_before_cloud_submission(settings, tmp_path: Path) -> None:
    from PIL import Image, ImageDraw

    frames = []
    for index in range(4):
        path = tmp_path / f"solid-{index}.png"
        image = Image.new("RGB", (320, 240), (35, 90, 45))
        draw = ImageDraw.Draw(image)
        draw.rectangle((80, 160, 240, 210), fill=(250, 250, 250))
        draw.rectangle((125, 178, 195, 190), fill=(220, 40, 40))
        image.save(path)
        frames.append(path)
    blocked, metrics = detect_solid_caption_backing(
        frames,
        [CleanupMask(x1=0.25, y1=0.665, x2=0.75, y2=0.88)],
        config=settings.subtitle_cleanup,
    )
    assert blocked is True
    assert metrics["persistence"] == 1.0


def test_natural_flat_area_continuing_through_ring_is_not_caption_block(
    settings, tmp_path: Path
) -> None:
    from PIL import Image

    path = tmp_path / "sky.png"
    Image.new("RGB", (320, 240), (60, 150, 235)).save(path)
    blocked, metrics = detect_solid_caption_backing(
        [path],
        [CleanupMask(x1=0.25, y1=0.665, x2=0.75, y2=0.88)],
        config=settings.subtitle_cleanup,
    )
    assert blocked is False
    assert metrics["ring_ratio_min"] > settings.subtitle_cleanup.backing_block_ring_ratio_max


def test_daily_paid_task_limit_blocks_new_submission(settings) -> None:
    service, _detector, _toolkit, client = _cloud_service(settings)
    settings.cloud_cleanup.max_paid_tasks_per_day = 1
    service.library.log_maintenance(
        "cloud_cleanup_submitted",
        target_type="clip",
        target_id=999,
        details={"run_id": "existing-paid-run"},
    )
    clip_id = _clip(service, settings)

    outcome = run(service.cleanup_clip(clip_id))

    assert outcome.status is CleanupStatus.INELIGIBLE
    assert outcome.reason == "daily_paid_task_limit_reached:1/1"
    assert client.uploads == []
    assert client.starts == []


def test_cloud_result_skips_post_ocr_and_exports_directly(settings) -> None:
    detector = ScriptedDetector(before=[_region()], after=[_region()])
    service, _detector, _toolkit, _client = _cloud_service(settings, detector=detector)
    clip_id = _clip(service, settings)
    outcome = run(service.cleanup_clip(clip_id))
    assert outcome.status is CleanupStatus.SUCCEEDED
    record = service.library.subtitle_cleanup(clip_id, CLOUD_CLEANUP_VERSION)
    assert record is not None and record["status"] == "succeeded"
    assert record["after_metrics"] is None
    assert record["review_status"] == "approved"


def test_cloud_result_skips_post_visual_quality_scoring(settings) -> None:
    toolkit = CloudFakeToolkit(after_duration=12.0)
    service, _detector, _toolkit, _client = _cloud_service(settings, toolkit=toolkit)
    clip_id = _clip(service, settings)
    outcome = run(service.cleanup_clip(clip_id))
    assert outcome.status is CleanupStatus.SUCCEEDED
    record = service.library.subtitle_cleanup(clip_id, CLOUD_CLEANUP_VERSION)
    assert record is not None and record["quality"] is None


def test_bounded_polling_stops_at_timeout(settings) -> None:
    settings.secrets = {}
    client = SdkVolcengineVodClient(settings)
    settings.cloud_cleanup.volcengine.max_poll_seconds = 0.2
    settings.cloud_cleanup.volcengine.poll_interval_seconds = 0.05

    async def never_terminal(run_id: str):
        return VolcengineExecution(run_id=run_id, status="Processing")

    async def no_sleep(_seconds: float) -> None:
        return None

    client.get_execution = never_terminal  # type: ignore[assignment]
    execution = run(client.poll_execution("run-1", sleep=no_sleep))
    assert execution.status == "Processing"
    assert execution.error_class == "timeout"


def test_rejected_original_is_not_production_ready(settings) -> None:
    service, _detector, _toolkit, _client = _cloud_service(settings)
    clip_id = _clip(service, settings)
    run(service.cleanup_clip(clip_id))
    service.library.set_review([clip_id], status=ReviewStatus.REJECTED, note="watermark")
    report = ProductionReadyService(service.library, settings).report()
    row = next(item for item in report.rows if item.clip_id == clip_id)
    assert row.clip_review_status == "rejected"
    assert row.production_ready is False
    assert report.aggregates["rejected_clips"] >= 1
