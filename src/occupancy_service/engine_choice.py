"""The engine and device a model runs on, where the settings say ``auto``. The README's Run section
gives the rules."""

import importlib.util
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from classifier.runtime import Engine

_log = logging.getLogger(__name__)

AUTO = "auto"
_GPU = "GPU"  # Only this name: OpenVINO lists GPU.0 and GPU.1 when there are two.

ClassifierEngine = Engine | Literal["auto"]


@dataclass(frozen=True)
class Choice:
    """The engine a model runs on and its device, None for an engine given none. ``gpu_error`` is
    why the model did not load on a listed GPU."""

    engine: str
    device: str | None
    gpu_error: str | None = None


def openvino_devices() -> Sequence[str] | None:
    """The devices OpenVINO lists, None when it does not import."""
    try:
        import openvino
    except ImportError:
        return None
    return openvino.Core().available_devices


def openvino_imports() -> bool:
    return importlib.util.find_spec("openvino") is not None


def choose_classifier[Model](
    engine: ClassifierEngine,
    device: str,
    devices: Callable[[], Sequence[str] | None],
    load: Callable[[Engine, str], Model],
) -> tuple[Model, Choice]:
    """The classifier, loaded by ``load``, and where it runs. ``devices`` is as
    ``openvino_devices``, and is asked only for an ``auto``. Raises what ``load`` raises, except on
    a GPU that ``auto`` tried."""
    listed = devices() if AUTO in (engine, device) else None
    if engine == AUTO:
        engine = "onnxruntime" if listed is None else "openvino"
    if device != AUTO:
        return _loaded(load, engine, device)
    gpu_error = None
    if engine == "openvino" and listed is not None and _GPU in listed:
        try:
            return _loaded(load, engine, _GPU)
        except Exception as error:  # What a model or an engine raises is theirs to choose.
            gpu_error = " ".join(str(error).split()) or type(error).__name__
    return _loaded(load, engine, "CPU", gpu_error)


def _loaded[Model](
    load: Callable[[Engine, str], Model], engine: Engine, device: str, gpu_error: str | None = None
) -> tuple[Model, Choice]:
    model = load(engine, device)
    line = f"the classifier runs on {engine} {device}"
    _log.info(line if gpu_error is None else f"{line}; it did not load on the GPU: {gpu_error}")
    return model, Choice(engine, device, gpu_error)


def choose_detector[Model](
    engine: str,
    device: str | None,
    openvino: Callable[[], bool],
    load: Callable[[str, str | None], Model],
) -> tuple[Model, Choice]:
    """The detector, loaded by ``load``, and where it runs. ``auto`` never picks the GPU, and
    gives onnxruntime no device. Raises what ``load`` raises."""
    if engine == AUTO:
        engine = "openvino" if openvino() else "onnxruntime"
    if device == AUTO:
        device = "CPU" if engine == "openvino" else None
    model = load(engine, device)
    _log.info("the detector runs on %s", engine if device is None else f"{engine} {device}")
    return model, Choice(engine, device)
