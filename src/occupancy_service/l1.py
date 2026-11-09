"""Turn the newest frame into L1: read the manifest, decode, cut, score, apply the threshold."""

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

import classifier
import numpy as np
import numpy.typing as npt
import r49
from classifier.runtime import Engine

from occupancy_service import detector_model
from occupancy_service.capture import (
    CaptureStatus,
    Frame,
    FrameSource,
    Rate,
    decode,
    each_new_frame,
)
from occupancy_service.drift import (
    Check,
    DriftCheck,
    DriftMonitor,
    DriftReading,
    DriftUnmeasured,
    Gray,
)
from occupancy_service.l0 import (
    L0,
    Detector,
    DetectorSays,
    L0Loop,
    L0Stats,
    car_width,
    detector_says,
    is_fresh,
)
from occupancy_service.l1_changes import L1Changes, L1Subscription

_log = logging.getLogger(__name__)

_WATCH_EVERY = 0.05  # Seconds between looks for a change that no new result brings.
_RATE_WINDOW = 2.0  # Seconds of results the overall rate is measured over.
_JOIN_WAIT = 2.0  # Seconds stop() waits for a thread, which may be stuck in a scorer.

_NO_FRAMES = "no frames"
_SCORING_STALLED = "scoring stalled"
_WRONG_FRAME_SIZE = "wrong frame size"
_DPT_UNRESOLVED = "DPT unresolved"
_NO_TRACK = "no track"
_OUTSIDE_THE_FRAME = "outside the frame"
_NO_MODEL = "no model"
_NO_ARCHIVE_ID = "archive has no id"
_DRIFT = "drift"
# The classifier's reasons for ``unknown`` that the detector may answer. Every other one stands.
_DETECTOR_MAY_ANSWER = (_NO_TRACK, _NO_MODEL)


@dataclass(frozen=True)
class L1Settings:
    """``archive`` is what ``r49.load`` takes as a path. A sensor farther than ``no_track_limit_g``
    gauges from every track is ``unknown``. ``device`` is as ``classifier.Classifier`` takes it.
    Scoring has stalled after ``stalled_after`` seconds with the camera fine and no new result.
    The drift check samples every ``drift_every`` seconds. Every sensor is ``unknown`` past
    ``drift_tolerance_g`` gauges, and None turns the check off.

    With ``detector_model`` set, the detector loads from that path, runs on ``detector_engine``
    and ``detector_device`` (None for ``onnxruntime``, which takes no device), and waits
    ``detect_every`` seconds after each run. A result is combined with the newest L0 no older
    than ``l0_limit`` seconds, and a box is ``margin_g`` gauges longer than its detection at both
    ends. None, the default, imports no detector.
    """

    archive: str | os.PathLike[str]
    no_track_limit_g: float = 0.3
    engine: Engine = "onnxruntime"
    device: str = "CPU"
    model: Path = classifier.MODEL
    stalled_after: float = 1.0
    drift_every: float = 3.0
    drift_tolerance_g: float | None = 0.5
    detector_model: Path | None = None
    detector_engine: str = "onnxruntime"
    detector_device: str | None = None
    detect_every: float = 1.0
    l0_limit: float = 5.0
    margin_g: float = 2.0


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
    ``wrong_frame_size`` says ``frame`` is not at ``camera.resolution``. ``drift`` is the newest
    reading, from a frame of its own, and None with the check turned off. ``l0`` is the newest L0
    no older than the limit, from a frame of its own, and None with none.
    """

    frame: Frame | None
    l1: Mapping[str, L1Entry]
    scores: Mapping[str, float]
    threshold: float | None
    patches: Mapping[str, npt.NDArray[np.uint8]]
    archive_id: str | None
    wrong_frame_size: bool = False
    drift: DriftReading | DriftUnmeasured | None = None
    l0: L0 | None = None


@dataclass(frozen=True)
class DetectorState:
    """Whether the detector loaded: ``ok``, or ``failed`` with a ``reason``."""

    state: Literal["ok", "failed"]
    reason: str | None = None


@dataclass(frozen=True)
class L1Stats:
    """The newest result's stage times in milliseconds, with the rate of results, the camera and
    the count of L1 changes per sensor.

    ``capture_ms`` runs from the frame reaching the service to the loop taking it. A time is None
    before any result, and ``cut_ms`` and ``classify_ms`` also when that result was not scored.
    ``l0`` holds the detector's, and ``detector`` whether it loaded. Both are None with no
    detector.
    """

    capture_ms: float | None
    decode_ms: float | None
    cut_ms: float | None
    classify_ms: float | None
    rate_fps: float
    camera: CaptureStatus
    changes: Mapping[str, int]
    l0: L0Stats | None = None
    detector: DetectorState | None = None


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


class CaptureSource(FrameSource, Protocol):
    """Where frames come from, with the camera's state: a ``Capture``, or a test's fake."""

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
    positions: Mapping[str, tuple[float, float]]
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
        positions={sensor.id: (sensor.x, sensor.y) for sensor in sensors},
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


def combine(entry: L1Entry, detector: DetectorSays | None) -> L1Entry:
    """One sensor's L1 from the classifier's ``entry`` and what the detector says, None with no
    fresh L0 or with the sensor outside the frame. The README's table states the rules."""
    if entry.state == "unknown" and entry.reason not in _DETECTOR_MAY_ANSWER:
        return entry
    if detector is not None and detector.occupied:
        return L1Entry(
            "occupied", confidence=max(entry.confidence or 0.0, detector.confidence or 0.0)
        )
    if entry.state != "unknown":
        return entry
    return L1Entry("clear") if detector is not None else entry


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
        self,
        settings: L1Settings,
        source: CaptureSource,
        scorer: Scorer | None = None,
        make_drift_check: Callable[[Gray], Check] = DriftCheck,
        detector: Detector | None = None,
    ) -> None:
        """Reads the manifest, and raises what ``r49.load`` raises when it cannot be read. The
        model the settings name loads in ``start()``.

        ``scorer`` stands in for the model the settings name, and ``make_drift_check`` for the
        drift check. ``detector`` stands in for the detector model the settings name.
        """
        self._settings = settings
        self._source = source
        self._scorer = scorer
        self._model_to_load = scorer is None
        self._detector = detector
        self._detector_state: DetectorState | None = None
        self._l0_loop: L0Loop | None = None
        self._lock = threading.Lock()
        self._drift = (
            None
            if settings.drift_tolerance_g is None
            else DriftMonitor(
                settings.drift_every, settings.drift_tolerance_g, source.latest, make_drift_check
            )
        )
        archive = r49.load(settings.archive)
        self._manifest = archive.manifest
        self._geometry = self._geometry_from(archive)
        if self._drift is not None:
            self._drift.use(archive, self._geometry.dpt)
        self._scored: _Scored | None = None
        self._scored_at = 0.0
        self._camera_ok_since: float | None = None
        self._newest_times: _Scored | None = None  # Outlasts ``_scored`` when the camera is lost.
        self._results = Rate(_RATE_WINDOW)
        self._changes = L1Changes()
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []
        with self._lock:
            self._advance()

    def start(self) -> None:
        if self._model_to_load:
            scorer = self._load_scorer()
            with self._lock:
                self._scorer = scorer
                self._model_to_load = False
                if scorer is None:
                    self._geometry = self._geometry_of(self._manifest)
                    self._advance()
        self._start_detector()
        self._stopping.clear()
        self._threads = [
            threading.Thread(
                target=each_new_frame,
                args=(self._source, self._stopping, self._handle, 0.0),
                name="l1",
                daemon=True,
            ),
            threading.Thread(target=self._watch, name="l1-watch", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        if self._drift is not None:
            self._drift.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._drift is not None:
            self._drift.stop()
        if self._l0_loop is not None:
            self._l0_loop.stop()
            self._l0_loop = None
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
            return self._changes.subscribe()

    def reload(self) -> None:
        """Read the manifest again, for the next frame to use, and rebuild the drift check. The
        frame in progress finishes under the old one. When this raises, as ``r49.load`` does,
        nothing has changed."""
        archive = r49.load(self._settings.archive)
        geometry = self._geometry_from(archive)
        with self._lock:
            self._manifest = archive.manifest
            self._geometry = geometry
            if self._drift is not None:
                self._drift.use(archive, geometry.dpt)
            self._advance()

    def stats(self) -> L1Stats:
        return self.read()[1]

    def read(self) -> tuple[L1Result, L1Stats]:
        """``latest()`` and ``stats()`` at one moment. A result that has a frame comes with that
        frame's stage times."""
        with self._lock:
            result = self._advance()
            times = self._newest_times
            rate = self._results.per_second(time.monotonic())
            l0_stats = self._l0_loop.newest()[1] if self._l0_loop is not None else None
            return result, L1Stats(
                capture_ms=times.capture_ms if times is not None else None,
                decode_ms=times.decode_ms if times is not None else None,
                cut_ms=times.cut_ms if times is not None else None,
                classify_ms=times.classify_ms if times is not None else None,
                rate_fps=rate,
                camera=self._source.status(),
                changes=self._changes.counts(),
                l0=l0_stats,
                detector=self._detector_state,
            )

    def _start_detector(self) -> None:
        """Loads the detector once, as the model loads, and starts its loop. A detector that does
        not load is reported, and L1 comes from the classifier alone."""
        model = self._settings.detector_model
        if self._detector is None and model is not None:
            try:
                self._detector = detector_model.load(
                    model, self._settings.detector_engine, self._settings.detector_device
                )
            except detector_model.DetectorUnavailable as error:
                _log.error("no L0: %s", error)
                self._detector_state = DetectorState("failed", str(error))
                return
        if self._detector is None:
            return
        self._detector_state = DetectorState("ok")
        if self._l0_loop is None:
            self._l0_loop = L0Loop(
                self._detector, self._source, self._l0_view, self._settings.detect_every
            )
        self._l0_loop.start()

    def _l0_view(self) -> tuple[float | None, tuple[int, int]]:
        with self._lock:
            return self._geometry.dpt, self._geometry.size

    def _fresh_l0(self) -> L0 | None:
        if self._l0_loop is None:
            return None
        l0 = self._l0_loop.newest()[0]
        return l0 if l0 is not None and is_fresh(l0, self._settings.l0_limit) else None

    def _combined(self, scored: _Scored, l0: L0 | None) -> Mapping[str, L1Entry]:
        """The classifier's L1 of ``scored`` with what ``l0`` says, under that result's manifest."""
        geometry = scored.geometry
        if l0 is None or geometry.dpt is None:
            says: Mapping[str, DetectorSays] = {}
        else:
            says = detector_says(
                l0.detections,
                geometry.positions,
                geometry.size,
                car_width(geometry.dpt),
                self._settings.margin_g * geometry.dpt,
            )
        return {id: combine(entry, says.get(id)) for id, entry in scored.l1.items()}

    def _load_scorer(self) -> Scorer | None:
        try:
            return _ClassifierScorer(self._settings)
        except Exception:  # What a model or an engine raises is theirs to choose.
            _log.exception("the model did not load")
            return None

    def _geometry_from(self, archive: r49.R49Archive) -> _Geometry:
        return self._geometry_of(archive.manifest)

    def _geometry_of(self, manifest: r49.Manifest) -> _Geometry:
        has_model = self._scorer is not None or self._model_to_load
        return _geometry_of(manifest, self._settings.no_track_limit_g, has_model)

    def _handle(self, frame: Frame) -> None:
        with self._lock:
            geometry = self._geometry
        scored = self._score(frame, geometry)
        if scored is not None:
            now = time.monotonic()
            with self._lock:
                self._scored, self._scored_at, self._newest_times = scored, now, scored
                self._results.add(now)
                self._advance()

    def _watch(self) -> None:
        while not self._stopping.wait(_WATCH_EVERY):
            with self._lock:
                self._advance()

    def _score(self, frame: Frame, geometry: _Geometry) -> _Scored | None:
        """The result of ``frame``. None when it is dropped."""
        capture_ms = frame.age_ms()
        start = time.perf_counter()
        image = decode(frame.jpeg)
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
        """The newest result as it stands now, handed to ``L1Changes`` under the geometry it came
        from. The caller holds the lock."""
        result = self._view()
        geometry = self._scored.geometry if self._scored is not None else self._geometry
        self._changes.update(result.l1, geometry)
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
        elif now - max(self._camera_ok_since, self._scored_at) > self._settings.stalled_after:
            live_reason = _SCORING_STALLED
        elif scored is None:
            live_reason = _NO_FRAMES  # The camera has just come back: there is no result yet.
        else:
            live_reason = None
        geometry = scored.geometry if scored is not None else self._geometry
        l0 = self._fresh_l0()
        if live_reason is None and scored is not None:
            l1 = self._combined(scored, None if scored.wrong_frame_size else l0)
        else:
            l1 = {
                id: L1Entry("unknown", reason=geometry.unknown.get(id, live_reason))
                for id in geometry.sensor_ids
            }
        drift = self._drift.reading() if self._drift is not None else None
        if drift is not None and drift.past:
            l1 = {id: L1Entry("unknown", reason=_DRIFT) for id in geometry.sensor_ids}
        if scored is None:
            return L1Result(None, l1, {}, threshold, {}, geometry.archive_id, drift=drift, l0=l0)
        return L1Result(
            scored.frame,
            l1,
            scored.scores,
            threshold,
            scored.patches,
            geometry.archive_id,
            scored.wrong_frame_size,
            drift,
            l0,
        )
