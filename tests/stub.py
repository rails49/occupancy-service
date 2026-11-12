"""A small implementation of LIVE.md to run the checker against. A test changes
what it serves through the fields of ``Stub``, to make it conform or break."""

import hashlib
import io
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import r49
from PIL import Image

WIDTH, HEIGHT = 64, 48
SENSORS = ["C3.A", 'say "hi"']
BOUNDARY = "stub-boundary"


def jpeg(width: int = WIDTH, height: int = HEIGHT) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), "gray").save(out, "JPEG")
    return out.getvalue()


def manifest(
    sensors: list[str] = SENSORS, width: int = WIDTH, height: int = HEIGHT
) -> dict[str, Any]:
    return {
        "version": r49.MANIFEST_VERSION,
        "id": "stub",
        "layout": {
            "scale": "N",
            "sensors": [{"id": sensor, "x": 1, "y": 2} for sensor in sensors],
        },
        "camera": {"resolution": {"width": width, "height": height}},
    }


def answer() -> dict[str, Any]:
    """The JSON part of a conforming answer with a frame, and no optional field."""
    return {
        "version": 1,
        "frame_id": "f-41",
        "captured_at": "2026-11-05T10:15:30.125Z",
        "l1": {
            "C3.A": {"state": "occupied", "confidence": 0.93},
            'say "hi"': {"state": "clear"},
        },
    }


def no_frame_answer() -> dict[str, Any]:
    """The JSON-only answer: no frame in about 1 s."""
    return {
        "version": 1,
        "frame_id": None,
        "captured_at": None,
        "l1": {sensor: {"state": "unknown", "reason": "no frames"} for sensor in SENSORS},
    }


def events() -> list[dict[str, Any]]:
    return [
        {"sensor": "C3.A", "state": "occupied", "confidence": 0.93},
        {"sensor": 'say "hi"', "state": "clear"},
    ]


@dataclass
class Part:
    name: str
    body: bytes
    filename: str | None = None
    content_type: str = "image/jpeg"


@dataclass
class Stub:
    latest: Any = field(default_factory=answer)
    raw_latest: bytes | None = None
    raw_json: bytes | None = None
    json_name: str = "json"
    json_filename: str | None = None
    latest_type: str = f"multipart/form-data; boundary={BOUNDARY}"
    truncate_latest: bool = False
    frame: bytes | None = field(default_factory=jpeg)
    frame_filename: str | None = "frame.jpg"
    patch_parts: list[Part] = field(default_factory=list)
    patches_with_after: bool = True
    manifest: bytes = field(default_factory=lambda: json.dumps(manifest()).encode())
    manifest_type: str = "application/json"
    manifest_status: int = 200
    events: list[dict[str, Any]] = field(default_factory=events)
    events_type: str = "text/event-stream"
    raw_events: bytes | None = None
    hold_events: float = 0
    repeat_events: bool = False
    not_found: set[str] = field(default_factory=set)
    requests: list[str] = field(default_factory=list)
    offers_saving: bool = False
    checks_if_match: bool = True
    checks_origin: bool = True
    checks_manifest: bool = True
    wrong_etag: str | None = None
    no_error_field: bool = False
    rewrite_fails: bool = False
    not_offered_status: int = 405
    not_offered_allow: str | None = "GET"
    writes: list[bytes] = field(default_factory=list)

    @property
    def etag(self) -> str:
        return self.wrong_etag or f'"{hashlib.sha256(self.manifest).hexdigest()}"'

    def multipart(self, patches: bool) -> bytes:
        if self.raw_latest is not None:
            return self.raw_latest
        fields = self.latest
        if isinstance(fields, dict) and not patches:
            fields = {key: value for key, value in fields.items() if key != "patches"}
        body = self.raw_json or json.dumps(fields).encode()
        parts = [Part(self.json_name, body, self.json_filename, "application/json")]
        if self.frame is not None:
            parts.append(Part("frame", self.frame, self.frame_filename))
        if patches:
            parts.extend(self.patch_parts)
        out = b""
        for part in parts:
            disposition = f'form-data; name="{part.name}"'
            if part.filename is not None:
                disposition += f'; filename="{part.filename}"'
            head = f"--{BOUNDARY}\r\nContent-Disposition: {disposition}\r\n"
            head += f"Content-Type: {part.content_type}\r\n\r\n"
            out += head.encode() + part.body + b"\r\n"
        return out if self.truncate_latest else out + f"--{BOUNDARY}--\r\n".encode()


@contextmanager
def serving(stub: Stub) -> Iterator[str]:
    """Serve ``stub`` on a free local port, and yield its base URL."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            stub.requests.append(self.path)
            path, _, query = self.path.partition("?")
            if path in stub.not_found:
                self.send_error(404)
            elif path == "/base/latest":
                patches = "patches=1" in query
                body = stub.multipart(
                    patches and (stub.patches_with_after or "after=" not in query)
                )
                self.reply(body, stub.latest_type)
            elif path == "/base/events":
                self.reply_events()
            elif path == "/base/archive/manifest.json":
                self.reply_manifest()
            else:
                self.send_error(404)

        def do_PUT(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if not stub.offers_saving:
                self.not_offered()
                return
            if_match = self.headers.get("If-Match")
            if stub.checks_origin and self.headers.get("Origin") == "http://live-check.invalid":
                self.status(403)
            elif stub.checks_if_match and if_match is None:
                self.status(428)
            elif stub.checks_if_match and if_match != stub.etag:
                self.status(412)
            elif stub.checks_manifest and self.refuse_manifest(body):
                pass
            elif stub.rewrite_fails:
                self.status(500)
            else:
                stub.manifest = body
                stub.writes.append(body)
                self.send_response(204)
                self.send_header("ETag", stub.etag)
                self.end_headers()

        def do_DELETE(self) -> None:
            self.not_offered()

        def not_offered(self) -> None:
            self.send_response(stub.not_offered_status)
            if stub.not_offered_allow is not None:
                self.send_header("Allow", stub.not_offered_allow)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def refuse_manifest(self, body: bytes) -> bool:
            try:
                r49.parse_manifest(body)
            except r49.R49Error as error:
                answer = (
                    b"{}" if stub.no_error_field else json.dumps({"error": error.rule}).encode()
                )
                self.send_response(422)
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)
                return True
            return False

        def status(self, code: int) -> None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def reply_manifest(self) -> None:
            self.send_response(stub.manifest_status)
            self.send_header("Content-Type", stub.manifest_type)
            self.send_header("Content-Length", str(len(stub.manifest)))
            if stub.offers_saving:
                self.send_header("ETag", stub.etag)
            self.end_headers()
            self.wfile.write(stub.manifest)

        def reply_events(self) -> None:
            lines = [f"data: {json.dumps(event)}\n\n" for event in stub.events]
            body = stub.raw_events or "".join(lines).encode()
            if not stub.hold_events and not stub.repeat_events:
                self.reply(body, stub.events_type)
                return
            self.send_response(200)
            self.send_header("Content-Type", stub.events_type)
            self.end_headers()
            self.wfile.write(body)
            time.sleep(stub.hold_events)
            try:
                while stub.repeat_events and not stopping.is_set():
                    self.wfile.write(body)
                    time.sleep(0.02)
            except OSError:
                pass  # The checker has hung up.

        def reply(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    stopping = threading.Event()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/base"
    finally:
        stopping.set()
        server.shutdown()
        server.server_close()
        thread.join()
