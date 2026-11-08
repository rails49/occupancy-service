"""L0, and what its boxes say about each sensor. The README's L0 section documents the rules."""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from r49.data import STANDARD_GAUGE

from occupancy_service.capture import Frame

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
