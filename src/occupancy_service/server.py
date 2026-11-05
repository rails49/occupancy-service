"""Serve the live contract over HTTP under ``/live``. LIVE.md owns what each route answers."""

import io
import json
import logging
import mimetypes
import os
import secrets
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote

import numpy as np
import numpy.typing as npt
import r49
from PIL import Image

from occupancy_service.capture import CaptureStatus, Frame
from occupancy_service.l1 import L1Entry, L1Loop, L1Result, L1Stats

_log = logging.getLogger(__name__)

_BASE = "/live"
_LATEST = f"{_BASE}/latest"
_ARCHIVE = f"{_BASE}/archive/"
_MANIFEST = "manifest.json"
_IDLE_TIMEOUT = 30.0  # Seconds an idle connection stays open, and any one write may take.


@dataclass(frozen=True)
class ServerSettings:
    """Where to listen. Port 0 takes a free port."""

    address: str = "127.0.0.1"
    port: int = 8000


class LiveServer:
    """A thread per request, reading from ``loop`` and ``archive``. The caller owns the loop."""

    def __init__(
        self, settings: ServerSettings, archive: str | os.PathLike[str], loop: L1Loop
    ) -> None:
        """Binds the address, and raises the ``OSError`` when it cannot."""
        self.settings = settings
        self.archive = archive
        self.loop = loop
        self.stopping = threading.Event()
        # A frame's id starts again at 1 on a restart, and a reader's id from before one must not
        # read as a frame still to come.
        self.run_id = secrets.token_hex(4)
        self._http = ThreadingHTTPServer((settings.address, settings.port), partial(_Handler, self))
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._http.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._http.serve_forever, args=(0.05,), name="http", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.stopping.set()
        if self._thread is not None:
            self._http.shutdown()
            self._thread.join()
            self._thread = None
        self._http.server_close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = _IDLE_TIMEOUT
    disable_nagle_algorithm = True

    def __init__(self, live: LiveServer, *args: Any) -> None:
        self.live = live  # Before super(), which handles the request.
        super().__init__(*args)

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        try:
            if path == _LATEST:
                self._latest(parse_qs(query))
            elif path.startswith(_ARCHIVE):
                self._archive_file(unquote(path.removeprefix(_ARCHIVE)))
            else:
                self.send_error(404)
        except OSError:
            self.close_connection = True  # The client has gone, or is not reading.

    def log_message(self, format: str, *args: Any) -> None:
        _log.debug(format, *args)

    def _latest(self, query: Mapping[str, list[str]]) -> None:
        result, stats = self.live.loop.read()
        # The contract has a frame part at camera.resolution, and no frame part with no frame.
        frame = None if result.wrong_frame_size else result.frame
        frame_id = None if frame is None else f"{self.live.run_id}-{frame.id}"
        fields = _fields(result, stats, frame, frame_id)
        parts = []
        if query.get("patches") == ["1"]:
            patches = {} if frame is None else result.patches
            fields["patches"] = {id: f"patch-{n}" for n, id in enumerate(patches)}
            parts = [
                _part(f"patch-{n}", f"patch-{n}.png", "image/png", _png(patch))
                for n, patch in enumerate(patches.values())
            ]
        if frame is not None:
            parts.insert(0, _part("frame", "frame.jpg", "image/jpeg", frame.jpeg))
        parts.insert(0, _part("json", None, "application/json", _json(fields)))
        boundary = secrets.token_hex(16)
        delimiter = f"--{boundary}\r\n".encode()
        body = b"".join(delimiter + part for part in parts) + f"--{boundary}--\r\n".encode()
        self._reply(body, f"multipart/form-data; boundary={boundary}", no_store=True)

    def _archive_file(self, name: str) -> None:
        # A folder or a zip would not refuse every dot path by itself, and this is a direct
        # request: the server refuses them for every archive.
        if any(segment.startswith(".") for segment in name.split("/")):
            self.send_error(404)
            return
        try:
            body = r49.load(self.live.archive).image(name)
        except (r49.R49Error, OSError):
            _log.exception("reading %r from the archive failed", name)
            self.send_error(500)
            return
        if body is None:
            self.send_error(404)
        elif name == _MANIFEST:
            self._reply(body, "application/json", no_store=True)
        else:
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self._reply(body, content_type)

    def _reply(self, body: bytes, content_type: str, no_store: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _fields(
    result: L1Result, stats: L1Stats, frame: Frame | None, frame_id: str | None
) -> dict[str, Any]:
    """The JSON part, with what the service offers, without ``patches``."""
    fields: dict[str, Any] = {
        "version": 1,
        "frame_id": frame_id,
        "captured_at": None if frame is None else frame.arrived_at.isoformat(),
        "l1": {id: _entry(entry) for id, entry in result.l1.items()},
        "scores": {} if frame is None else {id: float(p) for id, p in result.scores.items()},
        "changes": {id: stats.changes.get(id, 0) for id in result.l1},
        "timings": _timings(stats, frame is not None),
        "camera": _camera(stats.camera),
    }
    if result.threshold is not None:
        fields["threshold"] = float(result.threshold)
    return fields


def _entry(entry: L1Entry) -> dict[str, Any]:
    """An L1 entry as LIVE.md has it: only ``occupied`` has a number, only ``unknown`` a reason."""
    if entry.state == "occupied":
        return {"state": "occupied", "confidence": float(entry.confidence or 0)}
    if entry.state == "unknown":
        return {"state": "unknown", "reason": entry.reason}
    return {"state": "clear"}


def _timings(stats: L1Stats, has_frame: bool) -> dict[str, float]:
    """The rate, and with a frame served, that frame's stage times where it has them."""
    timings = {"rate_fps": stats.rate_fps}
    if has_frame:
        stages = {
            "capture_ms": stats.capture_ms,
            "decode_ms": stats.decode_ms,
            "cut_ms": stats.cut_ms,
            "classify_ms": stats.classify_ms,
        }
        timings |= {name: ms for name, ms in stages.items() if ms is not None}
    return timings


def _camera(status: CaptureStatus) -> dict[str, Any]:
    camera: dict[str, Any] = {"state": status.state, "rate_fps": status.rate_fps}
    if status.frame_age_ms is not None:
        camera["frame_age_ms"] = status.frame_age_ms
    return camera


def _json(value: Any) -> bytes:
    return json.dumps(value, allow_nan=False).encode()


def _png(patch: npt.NDArray[np.uint8]) -> bytes:
    out = io.BytesIO()
    Image.fromarray(patch).save(out, "PNG")
    return out.getvalue()


def _part(name: str, filename: str | None, content_type: str, body: bytes) -> bytes:
    """One part of a multipart body, with no delimiter. A part with no ``filename`` reads as a
    string in ``response.formData()``. ``name`` holds no character that needs quoting."""
    disposition = f'form-data; name="{name}"'
    if filename is not None:
        disposition += f'; filename="{filename}"'
    head = f"Content-Disposition: {disposition}\r\nContent-Type: {content_type}\r\n\r\n"
    return head.encode() + body + b"\r\n"
