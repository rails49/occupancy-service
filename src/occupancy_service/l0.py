"""L0, and what its boxes say about each sensor. The README's L0 section documents the rules."""

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt
from r49.data import STANDARD_GAUGE

from occupancy_service.capture import Frame, decode

_log = logging.getLogger(__name__)

_POLL = 0.01  # Seconds between looks for a new frame.
_RATE_WINDOW = 10.0  # Seconds of runs the rate is measured over: a run takes about a second.
_JOIN_WAIT = 2.0  # Seconds stop() waits for the thread, which may be stuck in the detector.

# The prototype's car width in mm, for the box's width: car width = DPT × this / standard gauge.
STANDARD_WIDTH = 3150.0


@dataclass(frozen=True)
class Detection:
    """One box the detector found, in ``camera.resolution`` pixels. ``angle`` is in radians, as a
    track's heading is, and runs along ``length``. ``width`` is the model's own prediction."""

    x: float
    y: float
    length: float
    width: float
    angle: float
    cls: str
    confidence: float


@dataclass(frozen=True)
class L0:
    """The detections in one frame. ``run`` counts the detector's runs, from 1."""

    frame: Frame
    detections: tuple[Detection, ...]
    run: int


@dataclass(frozen=True)
class L0Stats:
    """The newest run's milliseconds, the rate of runs, and the age of the newest L0 from when its
    frame reached the service. Each is None before the first run, and the rate is 0 then."""

    detect_ms: float | None
    rate_fps: float
    age_ms: float | None


class Detector(Protocol):
    """Finds the cars in one frame: the detector's model, or a test's fake."""

    def detect(self, frame: npt.NDArray[np.uint8], dpt: float) -> Sequence["Detection"]: ...


class FrameSource(Protocol):
    def latest(self) -> Frame | None: ...


class L0Loop:
    """Runs the detector on its own thread: it takes the newest frame, runs the detector, and
    waits ``every`` seconds. ``view`` gives the DPT and ``camera.resolution`` to use, and with no
    DPT, or a frame of another size, there is no run. A run that raises leaves the newest L0 as it
    was, and one that hangs holds up only this thread."""

    def __init__(
        self,
        detector: Detector,
        source: FrameSource,
        view: Callable[[], tuple[float | None, tuple[int, int]]],
        every: float,
    ) -> None:
        self._detector = detector
        self._source = source
        self._view = view
        self._every = every
        self._lock = threading.Lock()
        self._l0: L0 | None = None
        self._detect_ms: float | None = None
        self._runs_at: deque[float] = deque()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="l0", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(_JOIN_WAIT)
            self._thread = None

    def newest(self) -> tuple[L0 | None, L0Stats]:
        """The newest L0, however old, and the stats at this moment."""
        with self._lock:
            now = time.monotonic()
            self._forget_runs(now)
            count = len(self._runs_at)
            rate = (count - 1) / (now - self._runs_at[0]) if count > 1 else 0.0
            l0 = self._l0
            age_ms = None if l0 is None else _age_ms(l0.frame)
            return l0, L0Stats(self._detect_ms, rate, age_ms)

    def _run(self) -> None:
        seen: int | None = None
        while not self._stopping.is_set():
            frame = self._source.latest()
            if frame is None or frame.id == seen:
                self._stopping.wait(_POLL)
                continue
            seen = frame.id
            self._detect(frame)
            self._stopping.wait(self._every)

    def _detect(self, frame: Frame) -> None:
        dpt, size = self._view()
        if dpt is None:
            return
        image = decode(frame.jpeg)
        if image is None or (image.shape[1], image.shape[0]) != size:
            return
        start = time.perf_counter()
        try:
            detections = tuple(self._detector.detect(image, dpt))
        except Exception:  # As with a hang, L0 goes stale and the classifier goes on.
            _log.exception("the detector failed on a frame")
            return
        detect_ms = (time.perf_counter() - start) * 1000
        with self._lock:
            run = 1 if self._l0 is None else self._l0.run + 1
            self._l0 = L0(frame, detections, run)
            self._detect_ms = detect_ms
            now = time.monotonic()
            self._runs_at.append(now)
            self._forget_runs(now)

    def _forget_runs(self, now: float) -> None:
        while self._runs_at and self._runs_at[0] < now - _RATE_WINDOW:
            self._runs_at.popleft()


def _age_ms(frame: Frame) -> float:
    return max((datetime.now(UTC) - frame.arrived_at).total_seconds() * 1000, 0.0)


def is_fresh(l0: L0, limit: float) -> bool:
    """Whether ``l0``'s frame reached the service at most ``limit`` seconds ago."""
    return _age_ms(l0.frame) <= limit * 1000


@dataclass(frozen=True)
class DetectorSays:
    """What the detector says about one sensor: ``occupied`` with ``confidence``, or ``clear``."""

    occupied: bool
    confidence: float | None = None


def car_width(dpt: float) -> float:
    """The width in pixels of a car, at ``dpt`` pixels to the track gauge."""
    return dpt * STANDARD_WIDTH / STANDARD_GAUGE


def detector_says(
    detections: Sequence[Detection],
    sensors: Mapping[str, tuple[float, float]],
    size: tuple[int, int],
    width: float,
    margin: float,
) -> dict[str, DetectorSays]:
    """What the detections say about each sensor in the frame of ``size``. A sensor outside the
    frame has no entry. A box keeps a detection's centre and angle, takes ``width`` in place of
    the detection's own, and is ``margin`` pixels longer at both ends. A sensor strictly inside a
    box is occupied, with the highest confidence of the boxes it is in."""
    says: dict[str, DetectorSays] = {}
    for id, (x, y) in sensors.items():
        if not (0 <= x < size[0] and 0 <= y < size[1]):
            continue
        confidences = [d.confidence for d in detections if _inside(d, x, y, width, margin)]
        says[id] = DetectorSays(True, max(confidences)) if confidences else DetectorSays(False)
    return says


def _inside(detection: Detection, x: float, y: float, width: float, margin: float) -> bool:
    dx, dy = x - detection.x, y - detection.y
    cos, sin = math.cos(detection.angle), math.sin(detection.angle)
    along = dx * cos + dy * sin
    across = -dx * sin + dy * cos
    return abs(along) < detection.length / 2 + margin and abs(across) < width / 2


def detection_json(detection: Detection) -> dict[str, Any]:
    """A detection as LIVE.md has it."""
    return {
        "centre": {"x": detection.x, "y": detection.y},
        "length": detection.length,
        "width": detection.width,
        "angle": detection.angle,
        "class": detection.cls,
        "confidence": detection.confidence,
    }


def detection_from_json(value: Mapping[str, Any]) -> Detection:
    """A detection from LIVE.md's form. Raises ``KeyError`` or ``TypeError`` for another form."""
    centre = value["centre"]
    return Detection(
        x=float(centre["x"]),
        y=float(centre["y"]),
        length=float(value["length"]),
        width=float(value["width"]),
        angle=float(value["angle"]),
        cls=str(value["class"]),
        confidence=float(value["confidence"]),
    )
