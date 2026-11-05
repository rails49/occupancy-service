"""What the tests of the L1 loop and of the server share: an archive, a capture source and a scorer
that a test drives by hand."""

import io
import json
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import classifier
import numpy as np
import numpy.typing as npt
import r49
from PIL import Image

from occupancy_service.capture import CaptureStatus, Frame
from occupancy_service.l1 import L1Loop, Scoring

WIDTH, HEIGHT = 320, 240
# 100 pixels to 100 mm at HO, whose gauge is 1435 / 87 = 16.494 mm: a DPT of 16.494.
CALIBRATION = {
    "points": [
        {"px": {"x": 10, "y": 10}, "world": {"x": 0, "y": 0, "z": 0}},
        {"px": {"x": 110, "y": 10}, "world": {"x": 100, "y": 0, "z": 0}},
    ]
}
# A straight track along y = 120, running in the direction of +x.
TRACK = {
    "nodes": [
        {"x": 0, "y": 120, "heading": 0},
        {"x": WIDTH, "y": 120, "heading": 0},
    ]
}


def jpeg(width: int = WIDTH, height: int = HEIGHT, color: str = "gray") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, "JPEG")
    return out.getvalue()


def write_archive(
    folder: Path,
    sensors: dict[str, tuple[float, float]],
    **replaced: Any,
) -> Path:
    """A folder archive holding a manifest and no image. ``replaced`` overrides manifest keys,
    with ``None`` leaving one out."""
    layout: dict[str, Any] = {
        "scale": "HO",
        "calibration": replaced.pop("calibration", CALIBRATION),
        "sensors": [{"id": id, "x": x, "y": y} for id, (x, y) in sensors.items()],
        "tracks": replaced.pop("tracks", [TRACK]),
    }
    manifest = {
        "version": r49.MANIFEST_VERSION,
        "id": "archive-1",
        "layout": layout,
        "camera": {"resolution": {"width": WIDTH, "height": HEIGHT}},
        **replaced,
    }
    manifest = {key: value for key, value in manifest.items() if value is not None}
    folder.mkdir(exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps(manifest))
    return folder


class FakeSource:
    """A capture source a test drives by hand. Each frame it is shown gets the next id."""

    def __init__(self) -> None:
        self.state: Any = "ok"
        self.next_id = 1
        self.frame: Frame | None = None

    def show(self, data: bytes | None = None) -> Frame:
        self.frame = Frame(jpeg() if data is None else data, self.next_id, datetime.now(UTC))
        self.next_id += 1
        return self.frame

    def latest(self) -> Frame | None:
        return self.frame

    def status(self) -> CaptureStatus:
        return CaptureStatus(self.state, 0.0 if self.frame else None, 0.0)


@contextmanager
def running(loop: L1Loop) -> Iterator[L1Loop]:
    loop.start()
    try:
        yield loop
    finally:
        loop.stop()


def wait_for[T](value: Callable[[], T | None], timeout: float = 5) -> T:
    """The first truthy ``value()``. A timeout fails the test."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = value()
        if found:
            return found
        time.sleep(0.005)
    raise AssertionError(f"not within {timeout} s")


class FakeScorer:
    """Scores each sensor from ``scores``, with None for a sensor it holds no number for, and logs
    the frames and sensors it is asked about. With ``gate`` it waits there before it answers."""

    threshold = 0.5

    def __init__(self, scores: Mapping[str, float], gate: threading.Event | None = None) -> None:
        self.scores = scores
        self.gate = gate
        self.started = threading.Semaphore(0)
        self.sensors: list[Sequence[classifier.Sensor]] = []
        self.calls = 0

    def score(
        self, frame: npt.NDArray[np.uint8], sensors: Sequence[classifier.Sensor], dpt: float
    ) -> Scoring:
        self.calls += 1
        self.sensors.append(sensors)
        self.started.release()
        if self.gate is not None:
            self.gate.wait()
        patch = np.zeros((2, 2, 3), np.uint8)
        results = {
            s.id: (
                classifier.Result(self.scores[s.id], patch)
                if s.id in self.scores
                else classifier.Result(None, None)
            )
            for s in sensors
        }
        return Scoring(results, cut_ms=1.0, classify_ms=2.0)
