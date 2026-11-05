"""Capture MJPEG from a USB camera through an ``ffmpeg`` child process, keeping the latest frame."""

import logging
import os
import select
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import r49

_log = logging.getLogger(__name__)

_RETRY_WAIT = 1.0  # Seconds between the end of one ffmpeg and the start of the next.
_KILL_WAIT = 1.0  # Seconds to wait for a killed ffmpeg, which may be stuck in a USB call.
_RATE_WINDOW = 2.0  # Seconds of arrivals the capture rate is measured over.
_POLL = 0.05  # Seconds between looks at the pipe, the hang limit and stop().
_READ_SIZE = 1 << 16
_SOI, _EOI, _SOS = 0xD8, 0xD9, 0xDA


@dataclass(frozen=True)
class CaptureSettings:
    """``resolution`` is the manifest's ``camera.resolution``. The camera is ``lost`` after
    ``lost_after`` seconds with no new frame."""

    resolution: r49.Resolution
    device: str = "/dev/video0"
    frame_rate: float = 20
    hang_limit: float = 3
    ffmpeg: str = "ffmpeg"
    lost_after: float = 1.0


@dataclass(frozen=True)
class Frame:
    """A complete JPEG. ``id`` grows with each frame; ``arrived_at`` is when it reached Python."""

    jpeg: bytes
    id: int
    arrived_at: datetime


@dataclass(frozen=True)
class CaptureStatus:
    """``frame_age_ms`` is None before the first frame."""

    state: Literal["ok", "lost"]
    frame_age_ms: float | None
    rate_fps: float


def _ffmpeg_command(settings: CaptureSettings) -> list[str]:
    """Copy MJPEG from the camera to stdout, with no decode and no re-encode.

    ``mjpeg2jpeg`` adds the Huffman tables, because some UVC cameras leave them out.
    """
    size = f"{settings.resolution.width}x{settings.resolution.height}"
    # fmt: off
    return [
        settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "v4l2", "-input_format", "mjpeg",
        "-framerate", str(settings.frame_rate), "-video_size", size,
        "-i", settings.device,
        "-c:v", "copy", "-bsf:v", "mjpeg2jpeg", "-flush_packets", "1", "-f", "mjpeg", "pipe:1",
    ]
    # fmt: on


class Capture:
    """Runs ``ffmpeg`` on its own thread, and starts it again whenever it ends, until stop().

    One slot holds the latest frame, so a slow reader only misses frames and never holds up
    capture. status() works from the clock when it is called, so a blocked read of the camera
    cannot hold up ``lost``.
    """

    def __init__(self, settings: CaptureSettings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._latest: Frame | None = None
        self._next_id = 1
        self._arrivals: deque[float] = deque()
        self._last_arrival: float | None = None
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def latest(self) -> Frame | None:
        """The newest frame, however old. None before the first one."""
        with self._lock:
            return self._latest

    def status(self) -> CaptureStatus:
        now = time.monotonic()
        with self._lock:
            self._forget_arrivals(now)
            count = len(self._arrivals)
            rate = (count - 1) / (now - self._arrivals[0]) if count > 1 else 0.0
            last = self._last_arrival
        if last is None:
            return CaptureStatus("lost", None, rate)
        age = now - last
        return CaptureStatus("ok" if age <= self._settings.lost_after else "lost", age * 1000, rate)

    def _run(self) -> None:
        while not self._stopping.is_set():
            self._run_child()
            self._stopping.wait(_RETRY_WAIT)

    def _run_child(self) -> None:
        """One ffmpeg, from its start until it ends, hangs or stop() is called."""
        try:
            child = subprocess.Popen(
                _ffmpeg_command(self._settings), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE
            )
        except OSError as error:
            _log.warning("cannot start ffmpeg: %s", error)
            return
        assert child.stdout is not None
        try:
            self._read_frames(child.stdout.fileno())
        finally:
            child.kill()
            child.stdout.close()
            try:
                child.wait(_KILL_WAIT)
            except subprocess.TimeoutExpired:
                _log.warning("ffmpeg %d has not ended after a kill", child.pid)

    def _read_frames(self, pipe: int) -> None:
        """Until the pipe ends, no frame arrives within the hang limit, or stop() is called."""
        splitter = _JpegSplitter()
        deadline = time.monotonic() + self._settings.hang_limit
        while not self._stopping.is_set():
            if select.select([pipe], [], [], _POLL)[0]:
                data = os.read(pipe, _READ_SIZE)
                if not data:
                    return
                for jpeg in splitter.feed(data):
                    self._publish(jpeg)
                    deadline = time.monotonic() + self._settings.hang_limit
            if time.monotonic() >= deadline:
                _log.warning("no frame within %s s: killing ffmpeg", self._settings.hang_limit)
                return

    def _publish(self, jpeg: bytes) -> None:
        now = time.monotonic()
        with self._lock:
            self._latest = Frame(jpeg, self._next_id, datetime.now(UTC))
            self._next_id += 1
            self._last_arrival = now
            self._arrivals.append(now)
            self._forget_arrivals(now)

    def _forget_arrivals(self, now: float) -> None:
        while self._arrivals and self._arrivals[0] < now - _RATE_WINDOW:
            self._arrivals.popleft()


class _JpegSplitter:
    """Finds the complete JPEGs in a stream of them, by walking their markers.

    A JPEG runs from SOI through its scan to EOI. An SOI where a JPEG should go on means that
    JPEG was truncated, so it is dropped, and so is anything else that is not a JPEG.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._in_jpeg = False
        self._in_scan = False
        self._position = 0

    def feed(self, data: bytes) -> list[bytes]:
        """The JPEGs that ``data`` completes."""
        self._buffer += data
        jpegs: list[bytes] = []
        while self._find_start():
            end = self._find_end()
            if end is not None:
                jpegs.append(bytes(self._buffer[:end]))
                self._forget(end)
            elif self._in_jpeg:
                break  # It needs more data.
        return jpegs

    def _find_start(self) -> bool:
        if self._in_jpeg:
            return True
        start = self._buffer.find(bytes((0xFF, _SOI)))
        if start < 0:
            del self._buffer[:-1]  # The last byte may be half of an SOI.
            return False
        del self._buffer[:start]
        self._in_jpeg, self._in_scan, self._position = True, False, 2
        return True

    def _find_end(self) -> int | None:
        """Where the JPEG ends. None when it needs more data, or when it was dropped."""
        buffer = self._buffer
        while self._position < len(buffer):
            if self._in_scan:
                found = buffer.find(0xFF, self._position)
                if found < 0:
                    self._position = len(buffer)
                    return None
                self._position = found
            if self._position + 2 > len(buffer):
                return None
            mark, marker = buffer[self._position], buffer[self._position + 1]
            if mark != 0xFF:
                return self._forget(2)
            if marker == 0xFF:  # A fill byte.
                self._position += 1
            elif self._in_scan and (marker == 0 or 0xD0 <= marker <= 0xD7):
                self._position += 2  # A stuffed FF, or a restart marker: still the scan.
            elif marker == _EOI and self._in_scan:
                return self._position + 2
            elif marker == _SOI:
                return self._forget(self._position)
            elif marker == _EOI or marker < 0xC0 or 0xD0 <= marker <= 0xD7:
                return self._forget(2)  # These cannot start a segment.
            else:
                if self._position + 4 > len(buffer):
                    return None
                length = int.from_bytes(buffer[self._position + 2 : self._position + 4])
                if length < 2:
                    return self._forget(2)
                self._position += 2 + length
                self._in_scan = marker == _SOS
        return None

    def _forget(self, count: int) -> None:
        """Finish with the JPEG being walked. The next may start anywhere after ``count``."""
        del self._buffer[:count]
        self._in_jpeg = False
