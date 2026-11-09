"""The one module that imports ``rails49-detector``, which is installed separately (AGPL) and is
not a dependency of this package. Nothing imports it unless a detector model path is set.

It uses the interface rails49/detector's SPEC gives: ``detector.Detector(model_path, engine,
device)``, whose ``detect(frame, dpt)`` takes a frame as height × width × RGB, 8 bits, and the DPT,
and returns one dict per detection with ``centre`` (``x`` and ``y``), ``length``, ``width``,
``angle`` (in radians), ``class`` and ``confidence``, in the frame's pixels.
"""

import importlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from occupancy_service.l0 import Detection, Detector, detection_from_json

PACKAGE = "detector"  # The import name of rails49-detector.


class DetectorUnavailable(Exception):
    """The detector cannot run. The message is the reason ``latest`` gives."""


def load(model: Path, engine: str, device: str | None) -> Detector:
    """The model at ``model``. Raises ``DetectorUnavailable`` when the package is not installed
    or the model does not load."""
    try:
        package: Any = importlib.import_module(PACKAGE)
    except ImportError as error:
        raise DetectorUnavailable("rails49-detector is not installed") from error
    try:
        return _Adapter(package.Detector(model, engine, device))
    except Exception as error:  # What a model or an engine raises is theirs to choose.
        text = " ".join(str(error).split()) or type(error).__name__
        raise DetectorUnavailable(f"the detector model did not load: {text}") from error


class _Adapter:
    """The package's detector, giving this service's ``Detection``."""

    def __init__(self, detector: Any) -> None:
        self._detector = detector

    def detect(self, frame: npt.NDArray[np.uint8], dpt: float) -> Sequence[Detection]:
        return [detection_from_json(d) for d in self._detector.detect(frame, dpt)]
