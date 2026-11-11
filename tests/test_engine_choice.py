"""The engine and device a model runs on when the settings say ``auto``, against fake device lists
and fake loads."""

import logging
from collections.abc import Sequence

import pytest

from occupancy_service.engine_choice import Choice, choose_classifier, choose_detector


class Loads:
    """A load that records what it was asked for, and fails on the devices in ``failing``."""

    def __init__(self, *failing: str) -> None:
        self.failing = failing
        self.tried: list[tuple[str, str | None]] = []

    def __call__(self, engine: str, device: str | None) -> str:
        self.tried.append((engine, device))
        if device in self.failing:
            raise RuntimeError(f"cannot compile on\n{device}")
        return f"model on {engine} {device}"


def listed(devices: Sequence[str] | None):
    def devices_of() -> Sequence[str] | None:
        return devices

    return devices_of


def never_asked() -> Sequence[str] | None:
    raise AssertionError("the device list was asked for")


def test_a_listed_gpu_that_loads_runs_openvino_on_it(caplog: pytest.LogCaptureFixture) -> None:
    load = Loads()
    with caplog.at_level(logging.INFO):
        model, choice = choose_classifier("auto", "auto", listed(["CPU", "GPU"]), load)
    assert (model, choice) == ("model on openvino GPU", Choice("openvino", "GPU"))
    assert load.tried == [("openvino", "GPU")]
    assert caplog.messages == ["the classifier runs on openvino GPU"]


def test_a_listed_gpu_that_fails_falls_back_to_the_cpu_and_logs_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    load = Loads("GPU")
    with caplog.at_level(logging.INFO):
        model, choice = choose_classifier("auto", "auto", listed(["CPU", "GPU"]), load)
    assert model == "model on openvino CPU"
    assert choice == Choice("openvino", "CPU", "cannot compile on GPU")
    assert load.tried == [("openvino", "GPU"), ("openvino", "CPU")]
    assert caplog.messages == [
        "the classifier runs on openvino CPU; it did not load on the GPU: cannot compile on GPU"
    ]


@pytest.mark.parametrize("devices", [["CPU"], ["CPU", "GPU.0", "GPU.1"], ["AUTO", "CPU"]])
def test_with_no_device_named_gpu_openvino_runs_on_the_cpu(devices: list[str]) -> None:
    load = Loads()
    model, choice = choose_classifier("auto", "auto", listed(devices), load)
    assert (model, choice) == ("model on openvino CPU", Choice("openvino", "CPU"))
    assert load.tried == [("openvino", "CPU")]


def test_with_no_openvino_onnxruntime_runs_on_the_cpu() -> None:
    load = Loads()
    model, choice = choose_classifier("auto", "auto", listed(None), load)
    assert (model, choice) == ("model on onnxruntime CPU", Choice("onnxruntime", "CPU"))
    assert load.tried == [("onnxruntime", "CPU")]


def test_an_explicit_engine_and_device_are_used_and_nothing_else_is_tried() -> None:
    load = Loads()
    model, choice = choose_classifier("onnxruntime", "GPU", never_asked, load)
    assert (model, choice) == ("model on onnxruntime GPU", Choice("onnxruntime", "GPU"))
    assert load.tried == [("onnxruntime", "GPU")]


def test_an_explicit_device_that_fails_is_not_replaced() -> None:
    load = Loads("GPU")
    with pytest.raises(RuntimeError):
        choose_classifier("auto", "GPU", listed(["CPU", "GPU"]), load)
    assert load.tried == [("openvino", "GPU")]


def test_an_explicit_onnxruntime_with_an_auto_device_runs_on_the_cpu() -> None:
    load = Loads()
    _, choice = choose_classifier("onnxruntime", "auto", listed(["CPU", "GPU"]), load)
    assert choice == Choice("onnxruntime", "CPU")
    assert load.tried == [("onnxruntime", "CPU")]


def test_the_detector_runs_openvino_on_the_cpu_and_never_tries_the_gpu(
    caplog: pytest.LogCaptureFixture,
) -> None:
    load = Loads()
    with caplog.at_level(logging.INFO):
        model, choice = choose_detector("auto", "auto", lambda: True, load)
    assert (model, choice) == ("model on openvino CPU", Choice("openvino", "CPU"))
    assert load.tried == [("openvino", "CPU")]
    assert caplog.messages == ["the detector runs on openvino CPU"]


def test_the_detector_gives_onnxruntime_no_device_with_no_openvino(
    caplog: pytest.LogCaptureFixture,
) -> None:
    load = Loads()
    with caplog.at_level(logging.INFO):
        model, choice = choose_detector("auto", "auto", lambda: False, load)
    assert (model, choice) == ("model on onnxruntime None", Choice("onnxruntime", None))
    assert load.tried == [("onnxruntime", None)]
    assert caplog.messages == ["the detector runs on onnxruntime"]


def test_the_detector_uses_an_explicit_gpu() -> None:
    load = Loads()
    _, choice = choose_detector("openvino", "GPU", lambda: False, load)
    assert choice == Choice("openvino", "GPU")
    assert load.tried == [("openvino", "GPU")]


def test_a_detector_that_does_not_load_logs_no_choice(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError):
        choose_detector("auto", "auto", lambda: True, Loads("CPU"))
    assert caplog.messages == []
