# M9.8 Acceptance — Volcano Engine Refined Subtitle Erasure

Date: 2026-09-16
Project: `F:\Codex\fohe-dy`

No secrets, signed URLs or authorization headers are recorded.

## Clip #28 rejection

Clip #28 (`D:\素材库2\红薯干\clips\material_03ed7333_01188243.mp4`) is now:

```text
clip review_status = rejected
clip review_note   = 原视频含移动文字水印；v1 清理不充分且存在模糊块
cleanup v1 review  = rejected
cleanup v1 failure = visible_blur_patch
cleanup v1 note    = 明显模糊块；仍残留字幕；移动文字水印
```

Original media, thumbnail, provenance, source record, AI audit, cleanup
records and the v1 derivative remain untouched.  Production-ready now excludes
the rejected clip (`10/11` ready; rejected clip counts separately).

## Version separation

```text
subtitle_cleanup_v1
  engine = ffmpeg_delogo

subtitle_cleanup_v2_volcengine
  engine = volcengine_refined_subtitle_erase
  provider = volcengine
```

The v1 algorithm was not modified.  v2 uses its own deterministic derivative:

```text
<stem>__subtitle_cleanup_v2_volcengine.mp4
```

Schema v15 adds cloud audit columns for provider/input/run/output state; no
signed playback query is persisted.

## Official API/SDK

The implementation uses the official `volcengine-python-sdk` (5.0.49)
`UniversalApi` signer, not hand-written request signatures:

```text
POST https://vod.volcengineapi.com
service = vod
version = 2025-01-01
Action  = StartExecution
```

Erase request shape:

```text
Task.Type = Erase
Erase.Mode = Auto
Erase.Auto.Type = Subtitle
Erase.WithEraseInfo = true
Erase.NewVid = true
Erase.Auto.Locations = local RatioLocation boxes
```

`Type=Text` is never selected automatically.

## Credentials / readiness

Credentials are environment-only:

```text
VOLCENGINE_ACCESS_KEY_ID
VOLCENGINE_SECRET_ACCESS_KEY
VOLCENGINE_VOD_SPACE_NAME
VOLCENGINE_REGION
```

`.env.example` contains placeholders only.  Real readiness result:

```text
configured=false
official SDK available=true
authorized=false
VOD accessible=false
subtitle erase capability=false
space=(unset) region=(unset)
ready=false
```

This was the initial pre-credential readiness result. Credentials were later
configured locally through `.env`; real secret values remain intentionally
absent from this document.

## Safety behavior

* Clip-level human rejection is checked before upload; #28 was not uploaded.
* `none` / `watermark_only` -> `not_needed`.
* complex / multi-region / colored block / promotional overlay / dense text ->
  `ineligible`.
* Moving/floating watermark heuristic can mark a clip
  `watermark_disqualified` and prevents upload.
* Local OCR regions are converted to bounded `RatioLocation` boxes with a
  conservative margin; no full-frame erase.
* A full local preflight runs before the paid request: OCR geometry, stable
  subtitle tracks, watermark rejection, bounded masks and persistent solid
  caption-backing detection. `--subtitle-cleanup-cloud-preflight CLIP_ID`
  performs the same content gate without upload or paid API calls.
* After a successful cloud task, the signed output is downloaded, checked for
  basic media readability, exported directly and auto-approved. Per the final
  product policy, no second OCR/visual scoring or human review is performed.
* Approved healthy derivative -> preferred media; rejected/failed -> original.
* Cloud cleanup is opt-in (`--engine volcengine`), paid, and batch requires an
  explicit small `--limit`.

## Mocked tests

The current cloud test suite covers credentials/readiness, upload flow,
StartExecution shape, Subtitle-only policy, RatioLocation mapping, watermark
and solid-backing rejection, zero-cost preflight, RunId recovery, spending
limits, polling, signed download, direct export/auto-approval, idempotency,
version separation, original immutability and secret redaction.

## Regression

```text
824 tests collected / 820 passed / 4 skipped / 0 failures
--check: passed
--check-library: 11 clips, 0 missing, 0 orphans
--production-ready-report: 10/11 ready, 0 missing preferred media
--check-ai-provider: qwen ready=true, model=qwen-vl-max
cache: 0 files
```

## Real cloud acceptance

**COMPLETED on 2026-09-19.** Real upload, StartExecution, polling, signed URL
download, recovery and export were exercised. Clip #34 is the accepted
production result. Clip #31 exposed a solid caption backing plate; the paid
preflight now rejects that pattern before submission. Spending limits remain
configurable (`max_paid_tasks_per_run` / `max_paid_tasks_per_day`).

```bash
python app.py --check-volcengine-cleanup
python app.py --subtitle-cleanup-cloud <clip_id> --engine volcengine
```

Use a production-acceptable simple-subtitle clip with no moving watermark;
clip #28 is explicitly not eligible for salvage.
