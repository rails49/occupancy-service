"""How far the camera has moved since the archive's first image, sampled on its own thread.

The measuring is a port of ``@occupancy/drift`` in rails49/occupancy, which
``conformance/drift.json`` keeps in step with this one. Each block's lag is the peak of the whitened
cross-power spectrum, so the lighting drops out with the magnitude. A moved camera shifts every
block and a moved car only some, so the median over blocks reads the camera.
"""

import io
import logging
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Protocol

import numpy as np
import numpy.typing as npt
import r49
from PIL import Image

from occupancy_service.capture import Frame

_log = logging.getLogger(__name__)

type Gray = npt.NDArray[np.float32]
"""Height × width, row-major, from 0 to 1."""

_WORKING_WIDTH = 960
_BLOCK_SIZE = 128
_MIN_BLOCK_SIZE = 32
_NO_PHASE = 1e-12  # A bin weaker than this holds no phase to read.
_POLL = 0.05  # Seconds between looks for a first frame, or for a reload to give a reference.
_JOIN_WAIT = 2.0  # Seconds stop() waits for the thread, which may be in the middle of a reading.

_NO_READING_YET = "no reading yet"
_NO_FIRST_IMAGE = "no first image"
_DPT_UNRESOLVED = "DPT unresolved"
_CHECK_FAILED = "check failed"


def gray_of(image: bytes) -> Gray:
    """An encoded image in Rec. 601 luma. Raises what Pillow raises when it does not decode."""
    with Image.open(io.BytesIO(image)) as opened:
        rgb = np.asarray(opened.convert("RGB"), np.float64)
    luma = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    return (luma / 255).astype(np.float32)


def _resampled(plane: Gray, width: int, height: int) -> Gray:
    """``plane`` stretched to exactly this size: bilinear, sampling at pixel centres."""
    if plane.shape == (height, width):
        return plane
    rows, columns = plane.shape

    def sampled(
        count: int, source: int
    ) -> tuple[npt.NDArray[np.intp], npt.NDArray[np.intp], npt.NDArray[np.floating]]:
        at = np.clip((np.arange(count) + 0.5) * (source / count) - 0.5, 0, source - 1)
        low = np.floor(at).astype(np.intp)
        return low, np.minimum(low + 1, source - 1), at - low

    y0, y1, fy = sampled(height, rows)
    x0, x1, fx = sampled(width, columns)
    top = plane[y0][:, x0] * (1 - fx) + plane[y0][:, x1] * fx
    bottom = plane[y1][:, x0] * (1 - fx) + plane[y1][:, x1] * fx
    return (top * (1 - fy)[:, None] + bottom * fy[:, None]).astype(np.float32)


def _block_size(width: int, height: int) -> int:
    """128 px, stepping down for a plane too small for it."""
    size = _BLOCK_SIZE
    while size > min(width, height):
        size >>= 1
    if size < _MIN_BLOCK_SIZE:
        raise ValueError(f"a {width}x{height} image is too small to hold a block")
    return size


class DriftCheck:
    """Readings against one reference image. The reference's half of the work is done once."""

    def __init__(self, reference: Gray) -> None:
        """Raises ``ValueError`` for a reference too small to hold a block."""
        rows, columns = reference.shape
        self._width = min(_WORKING_WIDTH, columns)
        self._height = max(1, math.floor(rows * self._width / columns + 0.5))
        self._px_per_lag = columns / self._width
        self._size = _block_size(self._width, self._height)
        hann = (0.5 * (1 - np.cos(2 * np.pi * np.arange(self._size) / (self._size - 1)))).astype(
            np.float32
        )
        self._window = np.outer(hann, hann).astype(np.float32).astype(np.float64)
        self._reference = self._spectra(reference)

    def read(self, frame: Gray) -> float:
        """How far ``frame``, at any size, has moved, in pixels of the reference."""
        cross = self._spectra(frame).astype(np.complex128) * np.conj(
            self._reference.astype(np.complex128)
        )
        magnitude = np.abs(cross)
        whitened = np.divide(
            cross, magnitude, out=np.zeros_like(cross), where=magnitude >= _NO_PHASE
        )
        correlation = np.real(np.fft.ifft2(whitened)).reshape(len(cross), -1)
        # Whole-pixel lags, so a frame that did not move reads exactly 0. The transform wraps, so
        # a lag past halfway is a negative one.
        lag_y, lag_x = np.divmod(np.argmax(correlation, axis=1), self._size)
        half = self._size / 2
        dy = np.where(lag_y > half, lag_y - self._size, lag_y)
        dx = np.where(lag_x > half, lag_x - self._size, lag_x)
        return float(np.median(np.hypot(dx, dy))) * self._px_per_lag

    def _spectra(self, plane: Gray) -> npt.NDArray[np.complex64]:
        """The FFT of each whole block, windowed, with the leftover split between the edges."""
        size = self._size
        rows, columns = self._height // size, self._width // size
        top, left = (self._height - rows * size) // 2, (self._width - columns * size) // 2
        resampled = _resampled(plane, self._width, self._height)
        tiled = resampled[top : top + rows * size, left : left + columns * size]
        blocks = tiled.reshape(rows, size, columns, size).swapaxes(1, 2).reshape(-1, size, size)
        return np.fft.fft2(blocks.astype(np.float64) * self._window).astype(np.complex64)


class Check(Protocol):
    """Readings against one reference: a ``DriftCheck``, or a test's fake."""

    def read(self, frame: Gray) -> float: ...


@dataclass(frozen=True)
class DriftReading:
    """A reading in pixels of the first image. Exactly at the tolerance is not past it."""

    px: float
    tolerance_px: float

    @property
    def past(self) -> bool:
        return self.px > self.tolerance_px


@dataclass(frozen=True)
class DriftUnmeasured:
    """No reading to go by, and why. L1 goes on as if the camera had not moved."""

    reason: str
    past: ClassVar[bool] = False


@dataclass(frozen=True)
class _Reference:
    """What a reload says to measure against. It is compared by identity, so that each reload
    builds the check anew, as the file may hold other bytes under the same name."""

    archive: r49.R49Archive
    filename: str
    tolerance_px: float


class DriftMonitor:
    """Reads the newest frame every ``every`` seconds on its own thread, and keeps the last answer.

    ``reading()`` only takes that answer, so a slow check holds up nothing that asks for it. A
    refusal lasts through a reload until the next reading, unless the reload leaves nothing to
    measure against.
    """

    def __init__(
        self,
        every: float,
        tolerance_g: float,
        latest: Callable[[], Frame | None],
        make_check: Callable[[Gray], Check] = DriftCheck,
    ) -> None:
        self._every = every
        self._tolerance_g = tolerance_g
        self._latest = latest
        self._make_check = make_check
        self._lock = threading.Lock()
        self._reference: _Reference | None = None
        self._reading: DriftReading | DriftUnmeasured = DriftUnmeasured(_NO_READING_YET)
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def use(self, archive: r49.R49Archive, dpt: float | None) -> None:
        """Measure against this archive's first image from now on, with its DPT. The thread builds
        the check, so this does not wait for the image to decode."""
        images = archive.manifest.images
        if not images or dpt is None:
            reason = _DPT_UNRESOLVED if images else _NO_FIRST_IMAGE
            with self._lock:
                self._reference, self._reading = None, DriftUnmeasured(reason)
            return
        reference = _Reference(archive, images[0].filename, self._tolerance_g * dpt)
        with self._lock:
            self._reference = reference
            if isinstance(self._reading, DriftUnmeasured):
                self._reading = DriftUnmeasured(_NO_READING_YET)

    def reading(self) -> DriftReading | DriftUnmeasured:
        with self._lock:
            return self._reading

    def start(self) -> None:
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="drift", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(_JOIN_WAIT)
            self._thread = None

    def _run(self) -> None:
        built_for: _Reference | None = None
        check: Check | None = None
        while not self._stopping.is_set():
            with self._lock:
                reference = self._reference
            if reference is not built_for:
                built_for, check = reference, None if reference is None else self._build(reference)
            frame = self._latest()
            if reference is None or check is None or frame is None:
                self._stopping.wait(_POLL)
                continue
            self._settle(reference, self._read(check, frame, reference))
            self._stopping.wait(self._every)

    def _build(self, reference: _Reference) -> Check | None:
        """The check against ``reference``. None, with the reading saying why, when there is none.
        It is not tried again before the next reload, which is the one way the archive changes."""
        try:
            image = reference.archive.image(reference.filename)
            if image is not None:
                return self._make_check(gray_of(image))
            self._settle(reference, DriftUnmeasured(_NO_FIRST_IMAGE))
        except Exception:  # A failed check is reported, whatever an image or an archive raises.
            _log.exception("building the drift check on %r failed", reference.filename)
            self._settle(reference, DriftUnmeasured(_CHECK_FAILED))
        return None

    def _read(
        self, check: Check, frame: Frame, reference: _Reference
    ) -> DriftReading | DriftUnmeasured:
        try:
            return DriftReading(check.read(gray_of(frame.jpeg)), reference.tolerance_px)
        except Exception:  # A failed check is reported, and the next sample tries again.
            _log.exception("reading drift failed")
            return DriftUnmeasured(_CHECK_FAILED)

    def _settle(self, reference: _Reference, reading: DriftReading | DriftUnmeasured) -> None:
        """Take ``reading``, unless a reload has replaced what it was measured against."""
        with self._lock:
            if self._reference is reference:
                self._reading = reading
