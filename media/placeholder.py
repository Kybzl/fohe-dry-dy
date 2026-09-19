"""Tiny placeholder media files for mock mode.

The mock source, mock downloader and mock cutters all need files on disk so
the directory layout, hashing and deduplication logic run for real.  These
helpers write minimal stand-ins (a valid 1x1 JPEG, and a clearly fake MP4
container) instead of shipping binary fixtures.
"""

from __future__ import annotations

import base64
from pathlib import Path

# A 1x1 pixel baseline JPEG - valid enough for any image loader or thumbnail grid.
_TINY_JPEG_BASE64 = (
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)

TINY_JPEG_BYTES = base64.b64decode(_TINY_JPEG_BASE64)

FAKE_MP4_HEADER = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"


def write_placeholder_jpeg(path: Path, payload: bytes | None = None) -> Path:
    """Write a valid 1x1 JPEG placeholder (optionally with extra bytes)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    data = TINY_JPEG_BYTES
    if payload:
        # Trailing bytes keep the file readable as an image while making the
        # file content (and therefore its hash) depend on the signature.
        data = data + b"\n#sig:" + payload[:96]
    path.write_bytes(data)
    return path


def write_placeholder_mp4(path: Path, signature: bytes = b"") -> Path:
    """Write a fake MP4 file whose content is derived from ``signature``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    body = FAKE_MP4_HEADER + signature[:512] + b"\x00" * 8
    path.write_bytes(body)
    return path
