"""The detector in the L1 loop: its own loop, L0, and L1 from the classifier and the detector.
Every test uses a fake detector: CI installs no AGPL package."""

import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest
from fake_l1 import (
    FakeDetector,
    FakeScorer,
    FakeSource,
    detection,
    running,
    wait_for,
    write_archive,
)

from occupancy_service.l0 import Detection, DetectorSays
from occupancy_service.l1 import (
    DetectorState,
    L1Entry,
    L1Loop,
    L1Result,
    L1Settings,
    combine,
)

ROOT = Path(__file__).parent.parent
OCCUPIED = DetectorSays(True, 0.8)
CLEAR = DetectorSays(False)
# The reasons the detector never answers.
STANDING = [
    "no frames",
    "scoring stalled",
    "wrong frame size",
    "DPT unresolved",
    "outside the frame",
    "archive has no id",
]


@pytest.mark.parametrize(
    "detector, expected",
    [
        (OCCUPIED, L1Entry("occupied", confidence=0.8)),
        (DetectorSays(True, 0.3), L1Entry("occupied", confidence=0.6)),
        (CLEAR, L1Entry("occupied", confidence=0.6)),
        (None, L1Entry("occupied", confidence=0.6)),
    ],
)
def test_row_occupied_with_any_answer_is_occupied_with_the_higher_confidence(
    detector: DetectorSays | None, expected: L1Entry
) -> None:
    assert combine(L1Entry("occupied", confidence=0.6), detector) == expected


@pytest.mark.parametrize(
    "classifier",
    [
        L1Entry("clear"),
        L1Entry("unknown", reason="no track"),
        L1Entry("unknown", reason="no model"),
    ],
)
def test_row_the_detector_occupied_makes_any_answer_but_the_last_rows_occupied(
    classifier: L1Entry,
) -> None:
    assert combine(classifier, OCCUPIED) == L1Entry("occupied", confidence=0.8)


@pytest.mark.parametrize("detector", [CLEAR, None])
def test_row_clear_with_clear_or_no_fresh_l0_is_clear(detector: DetectorSays | None) -> None:
    assert combine(L1Entry("clear"), detector) == L1Entry("clear")


@pytest.mark.parametrize("reason", ["no track", "no model"])
def test_row_no_track_or_no_model_with_the_detector_clear_is_clear(reason: str) -> None:
    assert combine(L1Entry("unknown", reason=reason), CLEAR) == L1Entry("clear")


@pytest.mark.parametrize("reason", ["no track", "no model"])
def test_row_no_track_or_no_model_with_no_fresh_l0_is_unknown_with_that_reason(
    reason: str,
) -> None:
    entry = L1Entry("unknown", reason=reason)
    assert combine(entry, None) == entry


@pytest.mark.parametrize("reason", STANDING)
@pytest.mark.parametrize("detector", [OCCUPIED, CLEAR, None])
def test_row_the_last_rows_reasons_stand_whatever_the_detector_says(
    reason: str, detector: DetectorSays | None
) -> None:
    entry = L1Entry("unknown", reason=reason)
    assert combine(entry, detector) == entry


def newest(loop: L1Loop, test: Any) -> L1Result:
    """Wait until the newest result passes ``test``."""

    def found() -> L1Result | None:
        result = loop.latest()
        return result if test(result) else None

    return wait_for(found)


def test_a_car_the_detector_finds_makes_a_clear_sensor_occupied(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120), "s2": (60, 120)})
    source = FakeSource()
    frame = source.show()
    detector = FakeDetector([detection(160, 120, confidence=0.7)])
    scorer = FakeScorer({"s1": 0.1, "s2": 0.1})

    with running(L1Loop(L1Settings(archive), source, scorer, detector=detector)) as loop:
        result = newest(loop, lambda r: r.frame == frame and r.l0 is not None)
        _, stats = loop.read()

    assert result.l1 == {"s1": L1Entry("occupied", confidence=0.7), "s2": L1Entry("clear")}
    assert result.l0 is not None and result.l0.frame == frame
    assert detector.dpts == [pytest.approx(16.494, abs=0.001)]
    assert stats.detector == DetectorState("ok")
    assert stats.l0 is not None and stats.l0.detect_ms is not None and stats.l0.age_ms is not None


def test_l0_has_its_own_frame_when_the_detector_is_slower(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    first = source.show()
    detector = FakeDetector([detection(160, 120)])
    settings = L1Settings(archive, detect_every=60)

    with running(L1Loop(settings, source, FakeScorer({"s1": 0.1}), detector=detector)) as loop:
        newest(loop, lambda r: r.frame is not None and r.l0 is not None)
        later = source.show()
        result = newest(loop, lambda r: r.frame == later)

    assert result.l0 is not None and result.l0.frame == first
    assert result.l1["s1"].state == "occupied"
    assert detector.runs == 1


def test_a_hung_detector_leaves_the_classifier_running_and_its_l0_ends_at_the_limit(
    tmp_path: Path,
) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    source.show()
    release = threading.Event()
    detector = FakeDetector([detection(160, 120)])
    scorer = FakeScorer({"s1": 0.1})
    settings = L1Settings(archive, detect_every=0, l0_limit=0.5)

    try:
        with running(L1Loop(settings, source, scorer, detector=detector)) as loop:
            first = newest(loop, lambda r: r.frame is not None and r.l0 is not None)
            assert first.l1["s1"].state == "occupied"
            detector.gate = release
            source.show()
            assert detector.started.acquire(timeout=5)  # For the first frame.
            assert detector.started.acquire(timeout=5)  # It now hangs on the second.

            calls = scorer.calls
            start = time.monotonic()
            while time.monotonic() - start < 1.0:
                source.show()
                time.sleep(0.02)
            result = loop.latest()
            stats = loop.stats()
    finally:
        release.set()

    assert scorer.calls - calls > 25  # Every new frame is scored, at about 50 a second.
    assert result.frame is not None
    assert result.l0 is None
    assert result.l1["s1"] == L1Entry("clear")
    assert stats.l0 is not None and stats.l0.age_ms is not None and stats.l0.age_ms > 500
    assert stats.detector == DetectorState("ok")


def test_a_detector_that_raises_leaves_its_l0_to_end_at_the_limit(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    source.show()
    detector = FakeDetector([detection(160, 120)])
    settings = L1Settings(archive, detect_every=0, l0_limit=0.3)

    with running(L1Loop(settings, source, FakeScorer({"s1": 0.1}), detector=detector)) as loop:
        newest(loop, lambda r: r.frame is not None and r.l0 is not None)
        detector.error = RuntimeError("broken")
        stale = newest(loop, lambda r: (source.show(), r.l0 is None)[1])
        stats = loop.stats()

    assert stale.l1["s1"] == L1Entry("clear")
    assert stats.detector == DetectorState("ok")


def test_a_reload_changes_the_next_l1_with_no_new_detector_run(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    source.show()
    detector = FakeDetector([detection(160, 120)])
    scorer = FakeScorer({"s1": 0.1})
    settings = L1Settings(archive, detect_every=60)

    with running(L1Loop(settings, source, scorer, detector=detector)) as loop:
        assert (
            newest(loop, lambda r: r.frame is not None and r.l0 is not None).l1["s1"].state
            == "occupied"
        )
        write_archive(tmp_path / "a", {"s1": (300, 120)})
        loop.reload()
        frame = source.show()
        result = newest(loop, lambda r: r.frame == frame)

    assert result.l1["s1"] == L1Entry("clear")
    assert detector.runs == 1


def test_no_l0_with_no_dpt(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)}, calibration={"points": []})
    source = FakeSource()
    frame = source.show()
    detector = FakeDetector([detection(160, 120)])

    with running(L1Loop(L1Settings(archive), source, FakeScorer({}), detector=detector)) as loop:
        result = newest(loop, lambda r: r.frame == frame)
        time.sleep(0.1)
        result = loop.latest()

    assert result.l0 is None
    assert detector.runs == 0
    assert result.l1["s1"] == L1Entry("unknown", reason="DPT unresolved")


def test_no_track_with_a_fresh_l0_is_what_the_detector_says(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"far": (60, 30), "car": (160, 30)})
    source = FakeSource()
    frame = source.show()
    detector = FakeDetector([detection(160, 30, confidence=0.9)])

    with running(L1Loop(L1Settings(archive), source, FakeScorer({}), detector=detector)) as loop:
        result = newest(loop, lambda r: r.frame == frame and r.l0 is not None)

    assert result.l1 == {"far": L1Entry("clear"), "car": L1Entry("occupied", confidence=0.9)}


def test_no_detector_field_and_no_l0_with_no_model_path(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()

    with running(L1Loop(L1Settings(archive), source, FakeScorer({"s1": 0.1}))) as loop:
        result = newest(loop, lambda r: r.frame == frame)
        stats = loop.stats()

    assert result.l0 is None
    assert stats.detector is None
    assert stats.l0 is None


# One detection as rails49-detector's ``detect`` returns it: a dict, as its SPEC gives.
PACKAGE_DETECTION = {
    "centre": {"x": 160, "y": 120},
    "length": 50,
    "width": 31,
    "angle": 0.5,
    "class": "loco",
    "confidence": 0.75,
}


def fake_package(monkeypatch: pytest.MonkeyPatch, make: Any) -> None:
    """Install a ``detector`` module whose ``Detector`` is ``make``."""
    module = types.ModuleType("detector")
    monkeypatch.setattr(module, "Detector", make, raising=False)
    monkeypatch.setitem(sys.modules, "detector", module)


def test_a_model_that_does_not_load_fails_and_l1_comes_from_the_classifier_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(model: Path, engine: str, device: str | None) -> None:
        raise ValueError(f"no model at {model}")

    fake_package(monkeypatch, refuse)
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()
    settings = L1Settings(archive, detector_model=tmp_path / "missing.onnx")

    with running(L1Loop(settings, source, FakeScorer({"s1": 0.1}))) as loop:
        result = newest(loop, lambda r: r.frame == frame)
        later = source.show()
        assert newest(loop, lambda r: r.frame == later).l1["s1"] == L1Entry("clear")
        stats = loop.stats()

    assert result.l0 is None
    assert stats.detector is not None and stats.detector.state == "failed"
    assert stats.detector.reason and "missing.onnx" in stats.detector.reason


def test_a_model_path_with_the_package_not_installed_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "detector", None)  # import detector raises ImportError.
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()
    settings = L1Settings(archive, detector_model=tmp_path / "model.onnx")

    with running(L1Loop(settings, source, FakeScorer({"s1": 0.9}))) as loop:
        result = newest(loop, lambda r: r.frame == frame)
        stats = loop.stats()

    assert result.l1["s1"] == L1Entry("occupied", confidence=0.9)
    assert result.l0 is None
    assert stats.detector == DetectorState("failed", "rails49-detector is not installed")


def test_the_package_is_used_through_its_detector_with_the_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[tuple[Path, str, str | None]] = []

    class PackageDetector:
        def __init__(self, model: Path, engine: str, device: str | None) -> None:
            made.append((model, engine, device))

        def detect(self, frame: Any, dpt: float) -> list[dict[str, Any]]:
            assert frame.shape == (240, 320, 3)
            return [PACKAGE_DETECTION]

    fake_package(monkeypatch, PackageDetector)
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    source.show()
    model = tmp_path / "model.onnx"
    settings = L1Settings(
        archive, detector_model=model, detector_engine="openvino", detector_device="GPU"
    )

    with running(L1Loop(settings, source, FakeScorer({"s1": 0.1}))) as loop:
        result = newest(loop, lambda r: r.frame is not None and r.l0 is not None)

    assert made == [(model, "openvino", "GPU")]
    assert result.l0 is not None
    assert result.l0.detections == (Detection(160, 120, 50, 31, 0.5, "loco", 0.75),)
    assert result.l1["s1"] == L1Entry("occupied", confidence=0.75)


def test_the_default_settings_give_onnxruntime_no_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class PackageDetector:  # As rails49-detector's: onnxruntime refuses a device.
        def __init__(
            self, model: Path, engine: str = "onnxruntime", device: str | None = None
        ) -> None:
            if engine == "onnxruntime" and device is not None:
                raise ValueError("onnxruntime takes no device")

        def detect(self, frame: Any, dpt: float) -> list[dict[str, Any]]:
            return [PACKAGE_DETECTION]

    fake_package(monkeypatch, PackageDetector)
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    source.show()
    settings = L1Settings(archive, detector_model=tmp_path / "model.onnx")

    with running(L1Loop(settings, source, FakeScorer({"s1": 0.1}))) as loop:
        result = newest(loop, lambda r: r.frame is not None and r.l0 is not None)
        stats = loop.stats()

    assert stats.detector == DetectorState("ok")
    assert result.l1["s1"] == L1Entry("occupied", confidence=0.75)


@pytest.mark.parametrize("name", ["pyproject.toml", "uv.lock"])
def test_the_manifests_do_not_name_the_detector(name: str) -> None:
    assert "rails49-detector" not in (ROOT / name).read_text()


def test_the_service_imports_no_detector() -> None:
    code = (
        "import sys, occupancy_service.__main__, occupancy_service.l1;"
        "assert 'detector' not in sys.modules, 'detector imported'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
