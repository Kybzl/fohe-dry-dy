"""Build a local review pack for a failed Volcano cleanup result.

The command only downloads an already completed cloud output when the local
candidate is missing.  It never submits a new paid cleanup task.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.cloud_cleanup import CLOUD_CLEANUP_VERSION, CloudCleanupService
from core.config import DEFAULT_CONFIG_PATH, load_settings
from core.dependencies import build_library
from core.subtitle_cleanup import sample_timestamps
from core.subtitle_cleanup_models import CleanupMask


async def build_pack(clip_id: int, config_path: str) -> Path:
    settings = load_settings(config_path)
    library = build_library(settings)
    service = CloudCleanupService(library, settings)
    clip = library.get_clip(clip_id)
    record = library.subtitle_cleanup(clip_id, CLOUD_CLEANUP_VERSION)
    if clip is None or record is None:
        raise RuntimeError(f"clip #{clip_id} or its cleanup record was not found")
    if str(record.get("cloud_status") or "").lower() != "success":
        raise RuntimeError("the cloud task has not completed successfully")
    output_vid = str(record.get("cloud_output_vid") or "")
    if not output_vid:
        raise RuntimeError("the cleanup record has no cloud output Vid")

    pack_dir = (
        settings.subtitle_cleanup.reports_dir
        / "subtitle_cleanup"
        / f"clip_{clip_id}_failed_quality"
    )
    frames_dir = pack_dir / "frames"
    candidate = pack_dir / "cloud_candidate.mp4"
    pack_dir.mkdir(parents=True, exist_ok=True)
    if not candidate.is_file() or candidate.stat().st_size <= 0:
        await service.client.download_output(
            output_vid,
            candidate,
            output_file_name=str(record.get("cloud_output_file_name") or ""),
        )

    original = library.safe_media_path(clip.file_path)
    if original is None or not original.is_file():
        raise RuntimeError("the original clip is missing")
    duration = float(clip.duration or (await service.local_service._toolkit().probe(original)).duration)
    stamps = sample_timestamps(duration, config=service.local_service.config)
    before_frames = await service.local_service._extract_frames(
        original, stamps, frames_dir / "before", "before"
    )
    after_frames = await service.local_service._extract_frames(
        candidate, stamps, frames_dir / "after", "after"
    )
    masks = [
        CleanupMask.model_validate(item)
        for item in (record.get("regions") or [])
        if isinstance(item, dict)
    ]

    comparisons: list[Path] = []
    for index, (before_path, after_path) in enumerate(zip(before_frames, after_frames)):
        with Image.open(before_path) as before_image, Image.open(after_path) as after_image:
            before = before_image.convert("RGB")
            after = after_image.convert("RGB")
            width, height = before.size
            marked_before = before.copy()
            marked_after = after.copy()
            draw_before = ImageDraw.Draw(marked_before)
            draw_after = ImageDraw.Draw(marked_after)
            crop_boxes: list[tuple[int, int, int, int]] = []
            for mask in masks:
                x, y, w, h = mask.pixel_box(width, height)
                box = (x, y, min(width, x + w), min(height, y + h))
                crop_boxes.append(box)
                draw_before.rectangle(box, outline=(255, 48, 48), width=4)
                draw_after.rectangle(box, outline=(255, 48, 48), width=4)
            panels = [marked_before, marked_after]
            for box in crop_boxes:
                crop_before = before.crop(box)
                crop_after = after.crop(box)
                target_width = max(320, crop_before.width * 2)
                scale = target_width / max(1, crop_before.width)
                target_size = (target_width, max(1, round(crop_before.height * scale)))
                panels.extend(
                    [
                        crop_before.resize(target_size),
                        crop_after.resize(target_size),
                    ]
                )
            canvas_width = max(panel.width for panel in panels)
            canvas_height = sum(panel.height for panel in panels)
            canvas = Image.new("RGB", (canvas_width, canvas_height), "white")
            top = 0
            for panel in panels:
                canvas.paste(panel, (0, top))
                top += panel.height
            destination = frames_dir / f"compare_{index:03d}.jpg"
            destination.parent.mkdir(parents=True, exist_ok=True)
            canvas.save(destination, quality=92)
            comparisons.append(destination)

    quality = record.get("quality") or {}
    rows = []
    for index, path in enumerate(comparisons):
        stamp = stamps[index] if index < len(stamps) else 0.0
        rows.append(
            f"<h3>{stamp:.3f}s</h3><img src='{html.escape(path.relative_to(pack_dir).as_posix())}' "
            "alt='before and cloud output comparison'>"
        )
    document = "\n".join(
        [
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
            f"<title>clip #{clip_id} 云端去字幕失败质检</title>",
            "<style>body{font-family:sans-serif;margin:24px;max-width:1100px}"
            "img{max-width:100%;border:1px solid #ccc}video{width:48%;max-height:720px}"
            "code{white-space:pre-wrap}</style></head><body>",
            f"<h1>clip #{clip_id} 云端去字幕失败质检</h1>",
            "<p>每组依次为：原片标框、云端结果标框、原字幕区域放大、处理后区域放大。</p>",
            f"<p>状态：{html.escape(str(record.get('status')))}；原因："
            f"{html.escape(str(record.get('error') or record.get('skip_reason') or '-'))}</p>",
            f"<code>{html.escape(json.dumps(quality, ensure_ascii=False, indent=2))}</code>",
            "<h2>视频</h2>",
            f"<video controls src='{html.escape(original.as_uri())}'></video>",
            "<video controls src='cloud_candidate.mp4'></video>",
            "<h2>全部采样点</h2>",
            *rows,
            "</body></html>",
        ]
    )
    index_path = pack_dir / "index.html"
    index_path.write_text(document, encoding="utf-8")
    return index_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clip_id", type=int)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    args = parser.parse_args()
    path = asyncio.run(build_pack(args.clip_id, args.config))
    print(path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
