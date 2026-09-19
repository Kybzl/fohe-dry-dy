"""Local development stand-in for the Douyin backend (dtk v5 contract).

It is **not** a Douyin client: it serves the documented HTTP contract with
fixture data so the acquisition layer (envelope, API-key auth, 202 + task
polling, cursor paging, media streaming) can be exercised end to end without a
real deployment and without touching Douyin.

    python scripts/douyin_backend_stub.py --port 8899 \
        --media "D:/test/apple_drying.mp4" --api-key devkey

    set DOUYIN_BACKEND_BASE_URL=http://127.0.0.1:8899
    set DOUYIN_BACKEND_API_KEY=devkey
    python app.py --check-douyin
    python app.py --douyin-search "苹果干烘干" --target 2
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

OPENAPI_PATHS = {
    "/api/v1/{platform}/video": {"get": {}},
    "/api/v1/{platform}/user/posts": {"get": {}},
    "/api/v1/{platform}/mix/posts": {"get": {}},
    "/api/v1/archive": {"get": {}},
    "/api/v1/parse": {"post": {}},
    "/api/v1/tasks/{task_id}": {"get": {}},
    "/api/v1/downloads": {"post": {}},
}

FIXTURE_TITLES = (
    "{material}热泵烘干全过程实拍",
    "{material}切片铺盘细节",
    "{material}烘干房出料现场",
    "{material}价格表与联系方式（多字幕）",
    "厂区航拍，与本物料无关",
)


def build_contents(material: str, count: int, media_url: str) -> list[dict]:
    """Fixture posts: several usable ones plus one subtitle-heavy and one unrelated."""

    contents: list[dict] = []
    for index in range(max(1, count)):
        title = FIXTURE_TITLES[index % len(FIXTURE_TITLES)].format(material=material)
        content_id = f"stub{700000 + index}"
        contents.append(
            {
                "platform": "douyin",
                "content_id": content_id,
                "kind": "video",
                "web_url": f"https://www.douyin.com/video/{content_id}",
                "title": title,
                "description": f"{title} #烘干 #{material}",
                "created_at": "2026-09-10T08:00:00+00:00",
                "duration_ms": 30_000,
                "is_deleted": False,
                "is_private": False,
                "author": {
                    "nickname": "烘干设备老张",
                    "sec_uid": "MS4wLjABAAAAstub",
                    "uid": "1001",
                },
                "stats": {"digg_count": 1234, "play_count": 56789},
                "media": {
                    "covers": [{"url": f"{media_url}?cover=1"}],
                    "video": {
                        "url": media_url,
                        "width": 640,
                        "height": 360,
                        "bitrate": 1_500_000,
                        "watermark": False,
                    },
                    "streams": [],
                },
                "tags": ["烘干", material],
            }
        )
    return contents


class StubHandler(BaseHTTPRequestHandler):
    server_version = "dtk-stub/5.0.3"
    api_key = "devkey"
    media_path: Path | None = None
    material = "苹果干"
    content_count = 5
    tasks: dict[str, dict] = {}

    # -- helpers -----------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"[stub] {self.command} {self.path} :: {fmt % args}\n")

    def _authorized(self) -> bool:
        return self.headers.get("X-API-Key") == self.api_key or self.headers.get(
            "Authorization"
        ) == f"Bearer {self.api_key}"

    def _send(self, status: int, body: dict, headers: dict | None = None) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Request-ID", str(uuid.uuid4()))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _envelope(self, data, *, status: int = 200, error: dict | None = None) -> None:
        body = {
            "success": error is None,
            "data": None if error is not None else data,
            "error": error,
            "meta": {"request_id": str(uuid.uuid4()), "cached": False, "duration_ms": 1},
        }
        self._send(status, body)

    def _media_url(self) -> str:
        host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_port}"
        return f"http://{host}/media/sample.mp4"

    def _contents(self) -> list[dict]:
        return build_contents(self.material, self.content_count, self._media_url())

    def _requires_key(self) -> bool:
        if self._authorized():
            return False
        self._envelope(
            None,
            status=401,
            error={"code": "UNAUTHENTICATED", "message": "missing or invalid API key"},
        )
        return True

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path == "/healthz":
            self._send(200, {"status": "ok"})
            return
        if path == "/readyz":
            self._send(200, {"status": "ready"})
            return
        if path == "/openapi.json":
            self._send(
                200,
                {
                    "openapi": "3.1.0",
                    "info": {"title": "dtk-stub", "version": "5.0.3"},
                    "paths": OPENAPI_PATHS,
                },
            )
            return
        if path == "/media/sample.mp4":
            self._serve_media()
            return
        if self._requires_key():
            return

        if path == "/api/v1/auth/me":
            self._envelope(
                {"username": "admin", "role": "admin", "scopes": ["douyin:read", "admin"]}
            )
            return
        if path == "/api/v1/system/status":
            self._envelope(
                {"version": "5.0.3", "commit": "stub", "uptime_seconds": int(time.time())}
            )
            return
        if path == "/api/v1/archive":
            limit = int((query.get("limit") or ["20"])[0])
            self._envelope({"items": self._contents()[:limit], "cursor": None, "has_more": False})
            return
        if path == "/api/v1/douyin/video":
            aweme_id = (query.get("aweme_id") or [""])[0]
            match = next(
                (item for item in self._contents() if item["content_id"] == aweme_id), None
            )
            if match is None:
                self._envelope(
                    None,
                    status=404,
                    error={"code": "CONTENT_NOT_FOUND", "message": "no such post"},
                )
                return
            self._envelope(match)
            return
        if path.startswith("/api/v1/tasks/"):
            task_id = path.rsplit("/", 1)[-1]
            task = self.tasks.get(task_id)
            if task is None:
                self._envelope(
                    None,
                    status=404,
                    error={"code": "TASK_NOT_FOUND", "message": "unknown task"},
                )
                return
            self._envelope(task)
            return
        if path.startswith("/api/v1/douyin/user/posts"):
            self._envelope({"items": self._contents(), "cursor": None, "has_more": False})
            return
        if path.startswith("/api/v1/douyin/mix/posts"):
            self._envelope({"items": self._contents(), "cursor": None, "has_more": False})
            return
        self._envelope(
            None, status=404, error={"code": "NOT_FOUND", "message": f"no route {path}"}
        )

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        path = urlparse(self.path).path
        if self._requires_key():
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if path == "/api/v1/parse":
            url = str(body.get("url") or "")
            content_id = re.sub(r"[^0-9A-Za-z]", "", url.rsplit("/", 1)[-1]) or "stub0"
            match = next(
                (item for item in self._contents() if item["content_id"] == content_id),
                self._contents()[0],
            )
            self._envelope(match)
            return
        self._envelope(
            None, status=404, error={"code": "NOT_FOUND", "message": f"no route {path}"}
        )

    def _serve_media(self) -> None:
        """Serve the sample MP4 with HTTP Range support.

        Real CDNs answer ranged requests and ffprobe/ffmpeg rely on them: an
        MP4 written without ``+faststart`` keeps its ``moov`` atom at the end,
        so a seek to the tail is required just to read the metadata.
        """

        if self.media_path is None or not self.media_path.exists():
            self._send(404, {"error": "no media configured"})
            return
        data = self.media_path.read_bytes()
        total = len(data)
        start, end = 0, total - 1
        status = HTTPStatus.OK

        range_header = self.headers.get("Range")
        if range_header:
            match = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
            if match:
                raw_start, raw_end = match.groups()
                if raw_start:
                    start = int(raw_start)
                    end = int(raw_end) if raw_end else total - 1
                elif raw_end:  # suffix range: last N bytes
                    start = max(0, total - int(raw_end))
                end = min(end, total - 1)
                if start > end or start >= total:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT

        chunk = data[start : end + 1]
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        self.wfile.write(chunk)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="dtk v5 contract stub for fohe-dy")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--api-key", default="devkey")
    parser.add_argument("--media", default=None, help="mp4 served as the media stream")
    parser.add_argument("--material", default="苹果干")
    parser.add_argument("--content-count", type=int, default=5)
    args = parser.parse_args(argv)

    StubHandler.api_key = args.api_key
    StubHandler.material = args.material
    StubHandler.content_count = max(1, args.content_count)
    StubHandler.media_path = Path(args.media) if args.media else None

    server = ThreadingHTTPServer((args.host, args.port), StubHandler)
    print(
        f"dtk stub listening on http://{args.host}:{args.port} "
        f"(api key set, media={StubHandler.media_path})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
