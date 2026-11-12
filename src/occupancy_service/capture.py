"""Capture MJPEG from a V4L2 device or an MJPEG stream through an ``ffmpeg`` child process,
keeping the latest frame."""

import io
import logging
import os
import re
import select
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

import numpy as np
import numpy.typing as npt
import r49
from PIL import Image

_log = logging.getLogger(__name__)

_RETRY_WAIT = 1.0  # Seconds between the end of one ffmpeg and the start of the next.
_KILL_WAIT = 1.0  # Seconds to wait for a killed ffmpeg, which may be stuck in a USB call.
_RATE_WINDOW = 2.0  # Seconds of arrivals the capture rate is measured over.
_POLL = 0.05  # Seconds between looks at the pipe, the hang limit and stop().
_FRAME_POLL = 0.01  # Seconds between looks for a new frame.
_READ_SIZE = 1 << 16
_SOI, _EOI, _SOS = 0xD8, 0xD9, 0xDA
_UNDECODABLE = (OSError, ValueError, SyntaxError, Image.DecompressionBombError)
_MJPEG_SIZES = re.compile(r"Compressed:\s*mjpeg\s*:[^:]*:(.*)")
_SIZE = re.compile(r"\b(\d+)x(\d+)\b")


@dataclass(frozen=True)
class CaptureSettings:
    """``device`` is a V4L2 device path, or the URL of an MJPEG stream when it contains ``://``.

    ``resolution`` is the open archive's ``camera.resolution``. None, with no archive, is the
    largest size the camera lists for MJPEG, asked again at each start of ``ffmpeg``.
    ``Capture.use`` changes it. A stream is not asked for a size or ``frame_rate``: its source
    sets both. The camera is ``lost`` after ``lost_after`` seconds with no new
    frame."""

    resolution: r49.Resolution | None
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

    def age_ms(self) -> float:
        """Milliseconds since the frame reached Python."""
        return max((datetime.now(UTC) - self.arrived_at).total_seconds() * 1000, 0.0)


class FrameSource(Protocol):
    """Where frames come from: a ``Capture``, or a test's fake."""

    def latest(self) -> Frame | None: ...


def each_new_frame(
    source: FrameSource, stopping: threading.Event, handle: Callable[[Frame], None], every: float
) -> None:
    """Calls ``handle`` with each newest frame it has not had, and waits ``every`` seconds after
    each call, until ``stopping`` is set."""
    seen: int | None = None
    while not stopping.is_set():
        frame = source.latest()
        if frame is None or frame.id == seen:
            stopping.wait(_FRAME_POLL)
            continue
        seen = frame.id
        handle(frame)
        stopping.wait(every)


class Rate:
    """Events per second over the last ``window`` seconds of ``time.monotonic()``. Its owner holds
    the lock."""

    def __init__(self, window: float) -> None:
        self._window = window
        self._at: deque[float] = deque()

    def add(self, now: float) -> None:
        self._at.append(now)
        self._forget(now)

    def per_second(self, now: float) -> float:
        self._forget(now)
        count = len(self._at)
        return (count - 1) / (now - self._at[0]) if count > 1 else 0.0

    def _forget(self, now: float) -> None:
        while self._at and self._at[0] < now - self._window:
            self._at.popleft()


def decode(jpeg: bytes) -> npt.NDArray[np.uint8] | None:
    """The frame as height × width × RGB, 8 bits. None when it does not decode."""
    try:
        with Image.open(io.BytesIO(jpeg)) as image:
            return np.asarray(image.convert("RGB"))
    except _UNDECODABLE as error:
        _log.warning("dropping a frame that does not decode: %s", error)
        return None


@dataclass(frozen=True)
class CaptureStatus:
    """``frame_age_ms`` is None before the first frame."""

    state: Literal["ok", "lost"]
    frame_age_ms: float | None
    rate_fps: float


def largest_size(text: str) -> tuple[int, int] | None:
    """The largest of the MJPEG sizes in what ``ffmpeg -list_formats compressed`` prints. None
    when it lists none."""
    found = _MJPEG_SIZES.search(text)
    sizes = [] if found is None else _SIZE.findall(found[1])
    return max(((int(w), int(h)) for w, h in sizes), key=lambda s: s[0] * s[1], default=None)


def _listed_size(settings: CaptureSettings) -> tuple[int, int] | None:
    """The camera's largest MJPEG size, as ``ffmpeg`` lists it. None when it lists none, as with
    no camera."""
    # fmt: off
    command = [
        settings.ffmpeg, "-hide_banner", "-nostdin",
        "-f", "v4l2", "-list_formats", "compressed", "-i", settings.device,
    ]
    # fmt: on
    try:
        listed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=settings.hang_limit,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        _log.warning("cannot list the camera's sizes: %s", error)
        return None
    return largest_size(listed.stderr.decode(errors="replace"))


def _is_stream(device: str) -> bool:
    """Whether ``device`` is the URL of a stream rather than a device path."""
    return "://" in device


def _ffmpeg_command(settings: CaptureSettings, size: tuple[int, int] | None) -> list[str]:
    """Copy MJPEG from the camera to stdout, with no decode and no re-encode. ``size`` is used
    only for a device.

    ``mjpeg2jpeg`` adds the Huffman tables, because some UVC cameras leave them out.
    """
    if _is_stream(settings.device):
        transport = ["-rtsp_transport", "tcp"] if settings.device.startswith("rtsp://") else []
        source = [*transport, "-i", settings.device]
    else:
        assert size is not None
        # fmt: off
        source = [
            "-f", "v4l2", "-input_format", "mjpeg",
            "-framerate", str(settings.frame_rate), "-video_size", f"{size[0]}x{size[1]}",
            "-i", settings.device,
        ]
        # fmt: on
    # fmt: off
    return [
        settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        *source,
        "-c:v", "copy", "-bsf:v", "mjpeg2jpeg", "-flush_packets", "1", "-f", "mjpeg", "pipe:1",
    ]
    # fmt: on


class Capture:
    """Reads a V4L2 device or an MJPEG stream. Runs ``ffmpeg`` on its own thread, and starts it
    again whenever it ends, until stop().

    One slot holds the latest frame, so a slow reader only misses frames and never holds up
    capture. status() works from the clock when it is called, so a blocked read of the camera
    cannot hold up ``lost``.
    """

    def __init__(self, settings: CaptureSettings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._latest: Frame | None = None
        self._next_id = 1
        self._arrivals = Rate(_RATE_WINDOW)
        self._last_arrival: float | None = None
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._resolution = settings.resolution
        self._new_size = threading.Event()

    def start(self) -> None:
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def use(self, resolution: r49.Resolution | None) -> None:
        """Capture at ``resolution`` from now on, as ``CaptureSettings.resolution`` says. A new
        one ends the running ``ffmpeg``, and the next starts at it."""
        with self._lock:
            if resolution != self._resolution:
                self._resolution = resolution
                self._new_size.set()

    def latest(self) -> Frame | None:
        """The newest frame, however old. None before the first one."""
        with self._lock:
            return self._latest

    def status(self) -> CaptureStatus:
        now = time.monotonic()
        with self._lock:
            rate = self._arrivals.per_second(now)
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
        """One ffmpeg, from its start until it ends, hangs or stop() is called. For a device with
        no resolution set and no size listed, none."""
        with self._lock:
            resolution = self._resolution
            self._new_size.clear()
        size: tuple[int, int] | None
        if _is_stream(self._settings.device):
            size = None
        elif resolution is None:
            size = _listed_size(self._settings)
            if size is None:
                return
        else:
            size = (resolution.width, resolution.height)
        try:
            child = subprocess.Popen(
                _ffmpeg_command(self._settings, size),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
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
        """Until the pipe ends, no frame arrives within the hang limit, a new size is set, or
        stop() is called."""
        splitter = _JpegSplitter()
        deadline = time.monotonic() + self._settings.hang_limit
        while not self._stopping.is_set() and not self._new_size.is_set():
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
            self._arrivals.add(now)


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
