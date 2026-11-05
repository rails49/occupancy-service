"""Turn the newest frame into L1: read the manifest, decode, cut, score, apply the threshold."""

import io
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import Literal, Protocol

import classifier
import numpy as np
import numpy.typing as npt
import r49
from classifier.runtime import Engine
from PIL import Image

from occupancy_service.capture import CaptureStatus, Frame

_log = logging.getLogger(__name__)

_STALLED_AFTER = 1.0  # Seconds with the camera fine and no new result until scoring has stalled.
_POLL = 0.01  # Seconds between looks for a new frame.
_WATCH_EVERY = 0.05  # Seconds between looks for a change that no new result brings.
_RATE_WINDOW = 2.0  # Seconds of results the overall rate is measured over.
_JOIN_WAIT = 2.0  # Seconds stop() waits for a thread, which may be stuck in a scorer.
_UNDECODABLE = (OSError, ValueError, SyntaxError, Image.DecompressionBombError)

_NO_FRAMES = "no frames"
_SCORING_STALLED = "scoring stalled"
_WRONG_FRAME_SIZE = "wrong frame size"
_DPT_UNRESOLVED = "DPT unresolved"
_NO_TRACK = "no track"
_OUTSIDE_THE_FRAME = "outside the frame"
_NO_MODEL = "no model"
_NO_ARCHIVE_ID = "archive has no id"


@dataclass(frozen=True)
class L1Settings:
    """``archive`` is what ``r49.load`` takes as a path. A sensor farther than ``no_track_limit_g``
    gauges from every track is ``unknown``. ``device`` is as ``classifier.Classifier`` takes it.
    """

    archive: str | os.PathLike[str]
    no_track_limit_g: float = 0.3
    engine: Engine = "onnxruntime"
    device: str = "CPU"
    model: Path = classifier.MODEL


@dataclass(frozen=True)
class L1Entry:
    """One sensor's L1. Only ``occupied`` has a ``confidence``; only ``unknown`` a ``reason``."""

    state: Literal["occupied", "clear", "unknown"]
    confidence: float | None = None
    reason: str | None = None


@dataclass(frozen=True)
class L1Result:
    """Everything from one frame, with its L1 entries from one manifest.

    ``frame`` is None with no frame to serve, and then ``scores`` and ``patches`` are empty. A
    sensor with no score or no patch has no entry in them. A patch is height × width × RGB, 8 bits.
    ``wrong_frame_size`` says ``frame`` is not at ``camera.resolution``.
    """

    frame: Frame | None
    l1: Mapping[str, L1Entry]
    scores: Mapping[str, float]
    threshold: float | None
    patches: Mapping[str, npt.NDArray[np.uint8]]
    archive_id: str | None
    wrong_frame_size: bool = False


@dataclass(frozen=True)
class L1Change:
    """A sensor's new state or new reason, or the first state of a sensor a reload brought."""

    sensor: str
    entry: L1Entry


class L1Subscription:
    """The L1 changes since ``state``, each sensor's L1 when this subscription began.

    Changes wait here until they are read, so close it when done.
    """

    def __init__(self, state: Mapping[str, L1Entry], close: Callable[[], None]) -> None:
        self.state = state
        self.queue: SimpleQueue[L1Change] = SimpleQueue()
        self._close = close

    def get(self, timeout: float | None = None) -> L1Change | None:
        """The next change. None when there is none within ``timeout`` seconds."""
        try:
            return self.queue.get(timeout=timeout)
        except Empty:
            return None

    def close(self) -> None:
        self._close()


@dataclass(frozen=True)
class L1Stats:
    """The newest result's stage times in milliseconds, with the rate of results, the camera and
    the count of L1 changes per sensor.

    ``capture_ms`` runs from the frame reaching the service to the loop taking it. A time is None
    before any result, and ``cut_ms`` and ``classify_ms`` also when that result was not scored.
    """

    capture_ms: float | None
    decode_ms: float | None
    cut_ms: float | None
    classify_ms: float | None
    rate_fps: float
    camera: CaptureStatus
    changes: Mapping[str, int]


@dataclass(frozen=True)
class Scoring:
    """What a scorer gives for one frame, and the milliseconds the two stages of that took."""

    results: Mapping[str, classifier.Result]
    cut_ms: float
    classify_ms: float


class Scorer(Protocol):
    """Cuts and scores one frame's sensors. A sensor outside the frame scores None."""

    @property
    def threshold(self) -> float: ...

    def score(
        self, frame: npt.NDArray[np.uint8], sensors: Sequence[classifier.Sensor], dpt: float
    ) -> Scoring: ...


class CaptureSource(Protocol):
    """Where frames come from: a ``Capture``, or a test's fake."""

    def latest(self) -> Frame | None: ...

    def status(self) -> CaptureStatus: ...


class _ClassifierScorer:
    """``classifier.Classifier``, timing the cut and the model apart."""

    def __init__(self, settings: L1Settings) -> None:
        self._classifier = classifier.Classifier(settings.model, settings.engine, settings.device)
        self._classify_ms = 0.0
        # score() is one call with no public way to time its two stages, so this wraps the model
        # call inside it. It is why rails49-classifier is pinned.
        run = self._classifier._run

        def timed_run(patches: npt.NDArray[np.floating]) -> object:
            start = time.perf_counter()
            scores = run(patches)
            self._classify_ms = (time.perf_counter() - start) * 1000
            return scores

        self._classifier._run = timed_run

    @property
    def threshold(self) -> float:
        return self._classifier.threshold

    def score(
        self, frame: npt.NDArray[np.uint8], sensors: Sequence[classifier.Sensor], dpt: float
    ) -> Scoring:
        self._classify_ms = 0.0
        start = time.perf_counter()
        results = self._classifier.score(frame, sensors, dpt)
        total_ms = (time.perf_counter() - start) * 1000
        return Scoring(results, total_ms - self._classify_ms, self._classify_ms)


@dataclass(frozen=True)
class _Geometry:
    """What one manifest says about scoring. ``unknown`` holds each sensor that is ``unknown``
    whatever the frame shows, with its reason, and ``to_score`` the others."""

    sensor_ids: tuple[str, ...]
    archive_id: str | None
    size: tuple[int, int]
    dpt: float | None
    to_score: tuple[classifier.Sensor, ...]
    unknown: Mapping[str, str]


def _geometry_of(manifest: r49.Manifest, no_track_limit_g: float, has_model: bool) -> _Geometry:
    sensors = manifest.layout.sensors
    resolution = manifest.camera.resolution
    dpt = r49.calibration_fit(manifest).dpt
    to_score: list[classifier.Sensor] = []
    unknown: dict[str, str] = {}
    for sensor in sensors:
        if manifest.id is None:
            unknown[sensor.id] = _NO_ARCHIVE_ID
        elif not has_model:
            unknown[sensor.id] = _NO_MODEL
        elif dpt is None:
            unknown[sensor.id] = _DPT_UNRESOLVED
        else:
            at = r49.Point(x=sensor.x, y=sensor.y)
            nearest = r49.nearest_on_track(manifest.layout.tracks, at)
            if nearest is None or nearest.distance > no_track_limit_g * dpt:
                unknown[sensor.id] = _NO_TRACK
            else:
                to_score.append(classifier.Sensor(sensor.id, sensor.x, sensor.y, nearest.heading))
    return _Geometry(
        sensor_ids=tuple(sensor.id for sensor in sensors),
        archive_id=manifest.id,
        size=(resolution.width, resolution.height),
        dpt=dpt,
        to_score=tuple(to_score),
        unknown=unknown,
    )


@dataclass(frozen=True)
class _Scored:
    """The result of one frame, as the scoring thread leaves it."""

    frame: Frame
    geometry: _Geometry
    l1: Mapping[str, L1Entry]
    capture_ms: float
    decode_ms: float
    cut_ms: float | None = None
    classify_ms: float | None = None
    scores: Mapping[str, float] = field(default_factory=dict)
    patches: Mapping[str, npt.NDArray[np.uint8]] = field(default_factory=dict)
    wrong_frame_size: bool = False


def _decode(jpeg: bytes) -> npt.NDArray[np.uint8] | None:
    """The frame as height × width × RGB, 8 bits. None when it does not decode."""
    try:
        with Image.open(io.BytesIO(jpeg)) as image:
            return np.asarray(image.convert("RGB"))
    except _UNDECODABLE as error:
        _log.warning("dropping a frame that does not decode: %s", error)
        return None


def _entry(p_occupied: float | None, threshold: float) -> L1Entry:
    if p_occupied is None:
        return L1Entry("unknown", reason=_OUTSIDE_THE_FRAME)
    if p_occupied >= threshold:
        return L1Entry("occupied", confidence=p_occupied)
    return L1Entry("clear")


class L1Loop:
    """Scores the newest frame on its own thread, skipping one already scored.

    A read works from the clock when it is called, and a second thread looks as often for the
    changes that no new result brings, so a scorer that hangs cannot hold up ``scoring stalled``
    or ``no frames``. No lock is held while a frame is scored.
    """

    def __init__(
        self, settings: L1Settings, source: CaptureSource, scorer: Scorer | None = None
    ) -> None:
        """Reads the manifest, and raises what ``r49.load`` raises when it cannot be read.

        ``scorer`` stands in for the model the settings name.
        """
        self._settings = settings
        self._source = source
        self._scorer = scorer if scorer is not None else self._load_scorer()
        self._lock = threading.Lock()
        self._geometry = self._read_geometry()
        self._scored: _Scored | None = None
        self._scored_at = 0.0
        self._camera_ok_since: float | None = None
        self._newest_times: _Scored | None = None  # Outlasts ``_scored`` when the camera is lost.
        self._results_at: deque[float] = deque()
        self._states: dict[str, L1Entry] = {}
        self._changes: dict[str, int] = {}
        self._subscriptions: set[L1Subscription] = set()
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []
        with self._lock:
            self._advance()

    def start(self) -> None:
        self._stopping.clear()
        self._threads = [
            threading.Thread(target=self._run, name="l1", daemon=True),
            threading.Thread(target=self._watch, name="l1-watch", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stopping.set()
        for thread in self._threads:
            thread.join(_JOIN_WAIT)
        self._threads = []

    def latest(self) -> L1Result:
        """The newest result, with the state of the camera and of scoring at this moment."""
        with self._lock:
            return self._advance()

    def subscribe(self) -> L1Subscription:
        """Every L1 change from now on. The subscription's ``state`` leaves no gap before them."""
        with self._lock:
            self._advance()

            def close() -> None:
                with self._lock:
                    self._subscriptions.discard(subscription)

            subscription = L1Subscription(dict(self._states), close)
            self._subscriptions.add(subscription)
            return subscription

    def reload(self) -> None:
        """Read the manifest again, for the next frame to use. The frame in progress finishes
        under the old one. When this raises, as ``r49.load`` does, nothing has changed."""
        geometry = self._read_geometry()
        with self._lock:
            self._geometry = geometry
            self._advance()

    def stats(self) -> L1Stats:
        return self.read()[1]

    def read(self) -> tuple[L1Result, L1Stats]:
        """``latest()`` and ``stats()`` at one moment. A result that has a frame comes with that
        frame's stage times."""
        with self._lock:
            result = self._advance()
            times = self._newest_times
            now = time.monotonic()
            self._forget_results(now)
            count = len(self._results_at)
            rate = (count - 1) / (now - self._results_at[0]) if count > 1 else 0.0
            return result, L1Stats(
                capture_ms=times.capture_ms if times is not None else None,
                decode_ms=times.decode_ms if times is not None else None,
                cut_ms=times.cut_ms if times is not None else None,
                classify_ms=times.classify_ms if times is not None else None,
                rate_fps=rate,
                camera=self._source.status(),
                changes=dict(self._changes),
            )

    def _load_scorer(self) -> Scorer | None:
        try:
            return _ClassifierScorer(self._settings)
        except Exception:  # What a model or an engine raises is theirs to choose.
            _log.exception("the model did not load")
            return None

    def _read_geometry(self) -> _Geometry:
        manifest = r49.load(self._settings.archive).manifest
        return _geometry_of(manifest, self._settings.no_track_limit_g, self._scorer is not None)

    def _run(self) -> None:
        scored_id: int | None = None
        while not self._stopping.is_set():
            frame = self._source.latest()
            if frame is None or frame.id == scored_id:
                self._stopping.wait(_POLL)
                continue
            scored_id = frame.id
            with self._lock:
                geometry = self._geometry
            scored = self._score(frame, geometry)
            if scored is not None:
                now = time.monotonic()
                with self._lock:
                    self._scored, self._scored_at, self._newest_times = scored, now, scored
                    self._results_at.append(now)
                    self._forget_results(now)
                    self._advance()

    def _watch(self) -> None:
        while not self._stopping.wait(_WATCH_EVERY):
            with self._lock:
                self._advance()

    def _forget_results(self, now: float) -> None:
        while self._results_at and self._results_at[0] < now - _RATE_WINDOW:
            self._results_at.popleft()

    def _score(self, frame: Frame, geometry: _Geometry) -> _Scored | None:
        """The result of ``frame``. None when it is dropped."""
        capture_ms = max((datetime.now(UTC) - frame.arrived_at).total_seconds() * 1000, 0.0)
        start = time.perf_counter()
        image = _decode(frame.jpeg)
        decode_ms = (time.perf_counter() - start) * 1000
        if image is None:
            return None
        if (image.shape[1], image.shape[0]) != geometry.size:
            l1 = {
                id: L1Entry("unknown", reason=geometry.unknown.get(id, _WRONG_FRAME_SIZE))
                for id in geometry.sensor_ids
            }
            return _Scored(frame, geometry, l1, capture_ms, decode_ms, wrong_frame_size=True)
        l1 = {id: L1Entry("unknown", reason=reason) for id, reason in geometry.unknown.items()}
        if self._scorer is None or geometry.dpt is None or not geometry.to_score:
            return _Scored(frame, geometry, l1, capture_ms, decode_ms)
        try:
            scoring = self._scorer.score(image, geometry.to_score, geometry.dpt)
        except Exception:  # As with a hang, the camera is fine and the results stop.
            _log.exception("scoring a frame failed")
            return None
        results = scoring.results
        for sensor in geometry.to_score:
            l1[sensor.id] = _entry(results[sensor.id].p_occupied, self._scorer.threshold)
        return _Scored(
            frame,
            geometry,
            l1,
            capture_ms,
            decode_ms,
            scoring.cut_ms,
            scoring.classify_ms,
            scores={
                id: result.p_occupied
                for id, result in results.items()
                if result.p_occupied is not None
            },
            patches={
                id: result.patch for id, result in results.items() if result.patch is not None
            },
        )

    def _advance(self) -> L1Result:
        """The newest result as it stands now, telling subscribers what that changes. The caller
        holds the lock."""
        result = self._view()
        for id, entry in result.l1.items():
            before = self._states.get(id)
            if before is None or (before.state, before.reason) != (entry.state, entry.reason):
                if before is not None:
                    self._changes[id] = self._changes.get(id, 0) + 1
                for subscription in self._subscriptions:
                    subscription.queue.put(L1Change(id, entry))
        self._states = dict(result.l1)
        self._changes = {id: self._changes.get(id, 0) for id in result.l1}
        return result

    def _view(self) -> L1Result:
        """The newest result as it stands now. The caller holds the lock."""
        now = time.monotonic()
        if self._source.status().state == "lost":
            # Nothing from before the loss is served once the camera is back.
            self._camera_ok_since, self._scored = None, None
        elif self._camera_ok_since is None:
            self._camera_ok_since = now
        scored = self._scored
        threshold = self._scorer.threshold if self._scorer is not None else None
        if self._camera_ok_since is None:
            live_reason = _NO_FRAMES
        elif now - max(self._camera_ok_since, self._scored_at) > _STALLED_AFTER:
            live_reason = _SCORING_STALLED
        elif scored is None:
            live_reason = _NO_FRAMES  # The camera has just come back: there is no result yet.
        else:
            live_reason = None
        geometry = scored.geometry if scored is not None else self._geometry
        if live_reason is None and scored is not None:
            l1 = scored.l1
        else:
            l1 = {
                id: L1Entry("unknown", reason=geometry.unknown.get(id, live_reason))
                for id in geometry.sensor_ids
            }
        if scored is None:
            return L1Result(None, l1, {}, threshold, {}, geometry.archive_id)
        return L1Result(
            scored.frame,
            l1,
            scored.scores,
            threshold,
            scored.patches,
            geometry.archive_id,
            scored.wrong_frame_size,
        )
