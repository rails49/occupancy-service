"""Serve the live contract over HTTP under ``/live``. LIVE.md owns what each route answers."""

import io
import json
import logging
import mimetypes
import os
import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from email.message import Message
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote

import numpy as np
import numpy.typing as npt
import r49
from PIL import Image

from occupancy_service.active_profile import LastMessage
from occupancy_service.archive_writer import MANIFEST as _MANIFEST
from occupancy_service.archive_writer import ArchiveWriter, Refused, can_name, etag_of
from occupancy_service.capture import CaptureStatus, Frame
from occupancy_service.drift import DriftReading, DriftUnmeasured
from occupancy_service.l0 import L0, l0_json
from occupancy_service.l1 import DetectorState, L1Entry, L1Loop, L1Result, L1Stats
from occupancy_service.origin import is_own_page

_log = logging.getLogger(__name__)

_BASE = "/live"
_LATEST = f"{_BASE}/latest"
_EVENTS = f"{_BASE}/events"
_ARCHIVE = f"{_BASE}/archive/"
_AFTER_WAIT = 1.0  # Seconds ``after`` waits for a newer frame.
_AFTER_POLL = 0.01  # Seconds between looks for one, as often as the loop looks for a new frame.
_EVENTS_BUFFER = 1 << 16  # Bytes the system holds for an ``events`` client that is not reading.
_EVENTS_WAKE = 0.1  # Seconds an ``events`` stream waits before it looks at stop() again.
_IDLE_TIMEOUT = 30.0  # Seconds an idle connection stays open, and any one write may take.
_BODY_CHUNK = 1 << 16  # Bytes of a request's body read at a time.


@dataclass(frozen=True)
class ServerSettings:
    """Where to listen, and how an ``events`` stream behaves. Port 0 takes a free port.

    A comment line goes out every ``comment_every`` seconds. A client that takes more than
    ``write_timeout`` seconds to take one event, or has more than ``max_backlog`` changes waiting
    for it, has fallen behind, and is dropped.
    """

    address: str = "127.0.0.1"
    port: int = 8000
    comment_every: float = 15
    write_timeout: float = 2
    max_backlog: int = 1000


class LiveServer:
    """A thread per request, reading from ``loop`` and ``archive``. The caller owns the loop."""

    def __init__(
        self,
        settings: ServerSettings,
        archive: str | os.PathLike[str],
        loop: L1Loop,
        last_messages: Callable[[], Mapping[str, Mapping[str, LastMessage]]] | None = None,
    ) -> None:
        """Binds the address, and raises the ``OSError`` when it cannot.

        ``last_messages`` is a ``Publisher``'s, and without it ``latest`` has no such field.
        """
        self.settings = settings
        self.archive = archive
        self.loop = loop
        self.last_messages = last_messages
        self.writer = ArchiveWriter(archive, loop.reload)
        self.stopping = threading.Event()
        # A frame's id starts again at 1 on a restart, and a reader's id from before one must not
        # read as a frame still to come.
        self.run_id = secrets.token_hex(4)
        self._http = ThreadingHTTPServer((settings.address, settings.port), partial(_Handler, self))
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._http.server_address[1]

    def seen_frame(self, frame_id: str) -> int:
        """The ``Frame.id`` of a ``frame_id`` this run gave, and otherwise 0: any frame is newer
        than one from before a restart, or than an id that is no frame's."""
        run_id, _, number = frame_id.rpartition("-")
        if run_id != self.run_id:
            return 0
        try:
            return int(number)
        except ValueError:
            return 0

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
        self._unread: int | None = 0  # Bytes left of a write's body. None when that is not known.
        super().__init__(*args)

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except OSError:
            self.close_connection = True  # The client has gone.

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        try:
            if path == _LATEST:
                self._latest(parse_qs(query))
            elif path == _EVENTS:
                self._events()
            elif path.startswith(_ARCHIVE):
                self._archive_file(unquote(path.removeprefix(_ARCHIVE)))
            else:
                self.send_error(404)
        except OSError:
            self.close_connection = True  # The client has gone, or is not reading.

    def do_PUT(self) -> None:
        self._write(self._put)

    def do_DELETE(self) -> None:
        self._write(self._delete)

    def log_message(self, format: str, *args: Any) -> None:
        _log.debug(format, *args)

    def _latest(self, query: Mapping[str, list[str]]) -> None:
        result, stats, frame = self._read(query.get("after", [None])[0])
        fields = _fields(result, stats, frame, self.live.run_id)
        if self.live.last_messages is not None:
            fields["last_messages"] = _last_messages(self.live.last_messages(), result)
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

    def _read(self, after: str | None) -> tuple[L1Result, L1Stats, Frame | None]:
        """The newest result and the frame to serve with it. With ``after``, the first with a newer
        frame within about 1 s, or else the newest at the end of that."""
        seen = 0 if after is None else self.live.seen_frame(after)
        deadline = time.monotonic() + _AFTER_WAIT
        while True:
            result, stats = self.live.loop.read()
            # The contract has a frame part at camera.resolution, and no frame part with no frame.
            frame = None if result.wrong_frame_size else result.frame
            newer = frame is not None and frame.id > seen
            if after is None or newer or time.monotonic() >= deadline:
                return result, stats, frame
            self.live.stopping.wait(_AFTER_POLL)

    def _events(self) -> None:
        """Every sensor's state, then each change. Only this client's thread ever waits on it. An
        ``OSError`` from a write drops the client, which reconnects to the full state. So does a
        new manifest, as no event says that a sensor has gone."""
        settings = self.live.settings
        self.close_connection = True
        # A small buffer, so that a client that stops reading is found soon.
        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _EVENTS_BUFFER)
        self.connection.settimeout(settings.write_timeout)
        subscription = self.live.loop.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            for id, entry in subscription.state.items():
                self.wfile.write(_event(id, entry))
            comment_at = time.monotonic() + settings.comment_every
            while not self.live.stopping.is_set():
                if subscription.outdated or subscription.backlog() > settings.max_backlog:
                    return
                wait = min(_EVENTS_WAKE, max(comment_at - time.monotonic(), 0))
                change = subscription.get(wait)
                if change is not None:
                    self.wfile.write(_event(change.sensor, change.entry))
                if time.monotonic() >= comment_at:
                    self.wfile.write(b": still here\n\n")
                    comment_at += settings.comment_every
        finally:
            subscription.close()

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
            etag = etag_of(body) if self.live.writer.offered() else None
            self._reply(body, "application/json", no_store=True, etag=etag)
        else:
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self._reply(body, content_type)

    def _write(self, route: Callable[[str, str], None]) -> None:
        """Make the checks of every write, in the order LIVE.md lists them, and then ``route``,
        which takes the file's name and the ``If-Match`` value."""
        self._unread = _body_length(self.headers)
        path = self.path.partition("?")[0]
        try:
            try:
                if not path.startswith(_ARCHIVE):
                    raise Refused(404)
                if not self.live.writer.offered():
                    self._refuse(405, allow="GET")
                    return
                if not _own_page(self.headers.get("Origin"), self.headers.get("Host")):
                    raise Refused(403)
                if_match = self.headers.get("If-Match")
                if if_match is None:
                    raise Refused(428)
                self.live.writer.check_copy(if_match)
                name = unquote(path.removeprefix(_ARCHIVE))
                if not can_name(name):
                    raise Refused(404)
                route(name, if_match)
            except Refused as refused:
                self._refuse(refused.status, refused.rule)
            except (r49.R49Error, OSError):
                _log.exception("%s %r failed", self.command, path)
                self._refuse(500)
        except OSError:
            self.close_connection = True  # The client has gone, or is not reading.

    def _put(self, name: str, if_match: str) -> None:
        if self._unread is None or "Content-Length" not in self.headers:
            raise Refused(411)
        if name == _MANIFEST:
            etag = self.live.writer.put_manifest(if_match, b"".join(self._body()))
        else:
            self.live.writer.put_image(if_match, name, self._body())
            etag = None
        self._no_content(etag)

    def _delete(self, name: str, if_match: str) -> None:
        if self._unread is None:
            raise Refused(411)  # A body of no known length; with no body, none is needed.
        self.live.writer.delete(if_match, name)
        self._no_content()

    def _body(self) -> Iterator[bytes]:
        """What is left of the body. A client that sends less than it said raises an ``OSError``."""
        while self._unread:
            chunk = self.rfile.read(min(self._unread, _BODY_CHUNK))
            if not chunk:
                raise ConnectionError("the body ended before its Content-Length")
            self._unread -= len(chunk)
            yield chunk

    def _end_of_body(self) -> None:
        """Take the rest of the body before answering. A client still sending when the connection
        closes may get a reset in place of the answer. With a body of no known length, the
        connection cannot be used again."""
        for _ in self._body():
            pass
        if self._unread is None:
            self.send_header("Connection", "close")

    def _refuse(self, status: int, rule: str | None = None, allow: str | None = None) -> None:
        body = b"" if rule is None else _json({"error": rule})
        self.send_response(status)
        if allow is not None:
            self.send_header("Allow", allow)
        if rule is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._end_of_body()
        self.end_headers()
        self.wfile.write(body)

    def _no_content(self, etag: str | None = None) -> None:
        self.send_response(204)
        if etag is not None:
            self.send_header("ETag", etag)
        self._end_of_body()
        self.end_headers()

    def _reply(
        self, body: bytes, content_type: str, no_store: bool = False, etag: str | None = None
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if no_store:
            self.send_header("Cache-Control", "no-store")
        if etag is not None:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)


def _own_page(origin: str | None, host: str | None) -> bool:
    """``is_own_page``, with an ``Origin`` too malformed to read counting as a foreign one."""
    try:
        return is_own_page(origin, host)
    except ValueError:
        return False


def _body_length(headers: Message) -> int | None:
    """The length of a request's body, 0 with no sign of one, and None when it cannot be told."""
    length = headers.get("Content-Length", "0")
    if "Transfer-Encoding" in headers or not (length.isascii() and length.isdigit()):
        return None
    return int(length)


def _fields(result: L1Result, stats: L1Stats, frame: Frame | None, run_id: str) -> dict[str, Any]:
    """The JSON part, with what the service offers, without ``patches``. A frame's id is
    ``<run_id>-<Frame.id>``."""
    fields: dict[str, Any] = {
        "version": 1,
        "frame_id": None if frame is None else f"{run_id}-{frame.id}",
        "captured_at": None if frame is None else frame.arrived_at.isoformat(),
        "l1": {id: _entry(entry) for id, entry in result.l1.items()},
        "scores": {} if frame is None else {id: float(p) for id, p in result.scores.items()},
        "changes": {id: stats.changes.get(id, 0) for id in result.l1},
        "timings": _timings(stats, frame is not None),
        "camera": _camera(stats.camera),
    }
    if result.threshold is not None:
        fields["threshold"] = float(result.threshold)
    if result.drift is not None:
        fields["drift"] = _drift(result.drift)
    if result.l0 is not None:
        fields["l0"] = _l0(result.l0, run_id)
    if stats.detector is not None:
        fields["detector"] = _detector(stats.detector)
    return fields


def _l0(l0: L0, run_id: str) -> dict[str, Any]:
    return {"frame_id": f"{run_id}-{l0.frame.id}", **l0_json(l0)}


def _detector(detector: DetectorState) -> dict[str, Any]:
    if detector.state == "failed":
        return {"state": "failed", "reason": detector.reason}
    return {"state": "ok"}


def _last_messages(
    sent: Mapping[str, Mapping[str, LastMessage]], result: L1Result
) -> dict[str, Any]:
    """What went out for each sensor in ``result``, which may be fewer than the publisher has."""
    return {
        id: {
            profile: {"message": last.message, "time": last.time.isoformat()}
            for profile, last in by_profile.items()
        }
        for id, by_profile in sent.items()
        if id in result.l1
    }


def _entry(entry: L1Entry) -> dict[str, Any]:
    """An L1 entry as LIVE.md has it: only ``occupied`` has a number, only ``unknown`` a reason."""
    if entry.state == "occupied":
        return {"state": "occupied", "confidence": float(entry.confidence or 0)}
    if entry.state == "unknown":
        return {"state": "unknown", "reason": entry.reason}
    return {"state": "clear"}


def _drift(drift: DriftReading | DriftUnmeasured) -> dict[str, Any]:
    if isinstance(drift, DriftUnmeasured):
        return {"unmeasured": drift.reason}
    return {"px": drift.px, "tolerance_px": drift.tolerance_px, "past": drift.past}


def _timings(stats: L1Stats, has_frame: bool) -> dict[str, float]:
    """The rate, the detector's times where it has them, and with a frame served, that frame's
    stage times where it has them."""
    timings = {"rate_fps": stats.rate_fps}
    if stats.l0 is not None:
        l0 = {
            "detect_ms": stats.l0.detect_ms,
            "l0_rate_fps": stats.l0.rate_fps,
            "l0_age_ms": stats.l0.age_ms,
        }
        timings |= {name: value for name, value in l0.items() if value is not None}
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


def _event(sensor: str, entry: L1Entry) -> bytes:
    return b"data: " + _json({"sensor": sensor, **_entry(entry)}) + b"\n\n"


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
