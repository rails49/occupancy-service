"""The drift check, on the shared cases and on what they do not cover."""

import base64
import io
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fake_l1 import HEIGHT, WIDTH, FakeScorer, FakeSource, running, wait_for, write_archive
from PIL import Image

from occupancy_service.capture import Frame
from occupancy_service.drift import DriftCheck, DriftReading, DriftUnmeasured, Gray, gray_of
from occupancy_service.l1 import L1Change, L1Entry, L1Loop, L1Result, L1Settings

_SHARED = json.loads((Path(__file__).parent.parent / "conformance" / "drift.json").read_text())


def _plane(name: str) -> Any:
    raw = base64.b64decode(_SHARED["images"][name])
    shape = (_SHARED["height"], _SHARED["width"])
    return (np.frombuffer(raw, np.uint8).reshape(shape) / 255).astype(np.float32)


def _noise(width: int, height: int, dx: int = 0) -> Any:
    """A view of one noise picture, from ``dx`` pixels to the right."""
    rng = np.random.default_rng(7)
    picture = rng.random((height, width + 64), np.float32)
    return picture[:, dx : dx + width]


@pytest.mark.parametrize("case", _SHARED["cases"], ids=lambda case: case["name"])
def test_the_shared_cases(case: dict[str, Any]) -> None:
    check = DriftCheck(_plane(case["reference"]))
    assert check.read(_plane(case["frame"])) == pytest.approx(case["expected_px"], abs=1e-6)


def test_a_reading_is_in_pixels_of_the_reference_and_not_of_the_working_width() -> None:
    check = DriftCheck(_noise(1920, 256))
    assert check.read(_noise(1920, 256)) == 0
    assert check.read(_noise(1920, 256, dx=10)) == pytest.approx(10, abs=1e-6)


def test_a_frame_of_another_size_is_stretched_onto_the_reference() -> None:
    check = DriftCheck(np.full((256, 256), 0.5, np.float32))
    assert check.read(np.full((128, 128), 0.5, np.float32)) == 0


def test_a_reference_too_small_to_hold_a_block_is_refused() -> None:
    with pytest.raises(ValueError, match="too small"):
        DriftCheck(np.zeros((16, 64), np.float32))


def test_gray_is_rec_601_luma_from_0_to_1() -> None:
    out = io.BytesIO()
    Image.fromarray(np.array([[[255, 0, 0], [0, 255, 0]]], np.uint8)).save(out, "PNG")
    assert gray_of(out.getvalue())[0].tolist() == pytest.approx([0.299, 0.587], abs=1e-6)


SENSORS = {"s1": (160.0, 120.0), "s2": (170.0, 120.0)}
SCORES = {"s1": 0.9, "s2": 0.1}
REPORTING = {"s1": L1Entry("occupied", confidence=0.9), "s2": L1Entry("clear")}
DRIFT = L1Entry("unknown", reason="drift")
TOLERANCE_PX: Any = pytest.approx(8.247, abs=1e-3)  # 0.5 G at the test archive's DPT of 16.494.


class FakeChecks:
    """Stands in for ``DriftCheck``: every check reads ``px``, or raises it. With ``gate`` a reading
    waits there before it answers."""

    def __init__(self, px: float | Exception = 0.0) -> None:
        self.px = px
        self.gate: threading.Event | None = None
        self.built = 0
        self.reads = 0
        self.reading = threading.Event()

    def __call__(self, reference: Gray) -> "FakeChecks":
        self.built += 1
        return self

    def read(self, frame: Gray) -> float:
        self.reads += 1
        self.reading.set()
        if self.gate is not None:
            self.gate.wait()
        if isinstance(self.px, Exception):
            raise self.px
        return self.px


def _png(plane: Any) -> bytes:
    out = io.BytesIO()
    Image.fromarray(plane).save(out, "PNG")
    return out.getvalue()


def _picture(dx: int = 0) -> bytes:
    """A view at the test camera's size of one noise picture, from ``dx`` pixels to the right."""
    rng = np.random.default_rng(11)
    picture = rng.integers(0, 256, (HEIGHT, WIDTH + 64, 3), np.uint8)
    return _png(picture[:, dx : dx + WIDTH])


def _archive(tmp_path: Path, **replaced: Any) -> Path:
    """An archive whose first image is ``first.png``, and which also holds ``second.png``, the
    camera's view from 20 pixels away."""
    folder = write_archive(
        tmp_path / "archive", SENSORS, **{"images": [{"filename": "first.png"}], **replaced}
    )
    (folder / "first.png").write_bytes(_picture())
    (folder / "second.png").write_bytes(_picture(dx=20))
    return folder


def _loop(
    archive: Path, source: FakeSource, checks: FakeChecks | None = None, **settings: Any
) -> L1Loop:
    settings = {"drift_every": 0.01, **settings}
    scorer = FakeScorer(SCORES)
    if checks is None:
        return L1Loop(L1Settings(archive, **settings), source, scorer)
    return L1Loop(L1Settings(archive, **settings), source, scorer, make_drift_check=checks)


def _result_where(loop: L1Loop, holds: Callable[[L1Result], bool]) -> L1Result:
    def found() -> L1Result | None:
        result = loop.latest()
        return result if holds(result) else None

    return wait_for(found)


def _served(loop: L1Loop, frame: Frame) -> L1Result:
    return _result_where(loop, lambda result: result.frame == frame)


def _l1_is(loop: L1Loop, l1: dict[str, L1Entry]) -> L1Result:
    return _result_where(loop, lambda result: result.l1 == l1)


def _first_reading(loop: L1Loop) -> DriftReading:
    drift = _result_where(loop, lambda result: isinstance(result.drift, DriftReading)).drift
    assert isinstance(drift, DriftReading)
    return drift


def test_a_moved_camera_refuses_every_sensor_and_putting_it_back_restores_l1(
    tmp_path: Path,
) -> None:
    source = FakeSource()
    source.show(_picture())
    with running(_loop(_archive(tmp_path), source)) as loop:
        _l1_is(loop, REPORTING)
        assert _first_reading(loop) == DriftReading(0.0, TOLERANCE_PX)

        source.show(_picture(dx=20))
        refused = _l1_is(loop, {"s1": DRIFT, "s2": DRIFT})
        assert refused.drift == DriftReading(20.0, TOLERANCE_PX)

        source.show(_picture())
        assert _l1_is(loop, REPORTING).drift == DriftReading(0.0, TOLERANCE_PX)


def test_models_and_sampling_keep_running_while_refused(tmp_path: Path) -> None:
    source = FakeSource()
    checks = FakeChecks(px=100.0)
    scorer = FakeScorer(SCORES)
    settings = L1Settings(_archive(tmp_path), drift_every=0.01)
    with running(L1Loop(settings, source, scorer, make_drift_check=checks)) as loop:
        source.show()
        _l1_is(loop, {"s1": DRIFT, "s2": DRIFT})
        calls, reads = scorer.calls, checks.reads
        frame = source.show()
        assert _served(loop, frame).scores == SCORES
        assert scorer.calls > calls
        wait_for(lambda: checks.reads > reads)


def test_a_subscriber_hears_the_refusal_and_its_end(tmp_path: Path) -> None:
    source = FakeSource()
    checks = FakeChecks()
    with running(_loop(_archive(tmp_path), source, checks)) as loop:
        source.show()
        _l1_is(loop, REPORTING)
        subscription = loop.subscribe()
        checks.px = 100.0
        heard = {subscription.get(5), subscription.get(5)}
        assert heard == {L1Change("s1", DRIFT), L1Change("s2", DRIFT)}
        checks.px = 0.0
        heard = {subscription.get(5), subscription.get(5)}
        assert heard == {L1Change(id, entry) for id, entry in REPORTING.items()}
        subscription.close()


def test_a_reading_exactly_at_the_tolerance_passes_and_one_past_it_refuses(
    tmp_path: Path,
) -> None:
    source = FakeSource()
    checks = FakeChecks()
    with running(_loop(_archive(tmp_path), source, checks)) as loop:
        source.show()
        _l1_is(loop, REPORTING)
        reading = _first_reading(loop)
        assert reading.tolerance_px == TOLERANCE_PX

        at = reading.tolerance_px
        checks.px = at
        result = _result_where(loop, lambda r: r.drift == DriftReading(at, at))
        assert result.l1 == REPORTING
        assert result.drift is not None and not result.drift.past

        past = np.nextafter(at, np.inf).item()
        checks.px = past
        assert _l1_is(loop, {"s1": DRIFT, "s2": DRIFT}).drift == DriftReading(past, at)


def test_the_tolerance_is_a_setting(tmp_path: Path) -> None:
    source = FakeSource()
    source.show()
    checks = FakeChecks(px=1.0)
    with running(_loop(_archive(tmp_path), source, checks, drift_tolerance_g=0.01)) as loop:
        refused = _l1_is(loop, {"s1": DRIFT, "s2": DRIFT})
    tolerance: Any = pytest.approx(0.16494, abs=1e-3)
    assert refused.drift == DriftReading(1.0, tolerance)


def test_the_check_samples_the_newest_frame_at_the_interval(tmp_path: Path) -> None:
    source = FakeSource()
    checks = FakeChecks()
    with running(_loop(_archive(tmp_path), source, checks, drift_every=0.2)):
        source.show()
        wait_for(lambda: checks.reads == 1)
        time.sleep(0.05)
        assert checks.reads == 1
        wait_for(lambda: checks.reads >= 2)


def test_before_any_reading_l1_reports_and_drift_says_so(tmp_path: Path) -> None:
    source = FakeSource()
    with running(_loop(_archive(tmp_path), source, FakeChecks())) as loop:
        assert loop.latest().drift == DriftUnmeasured("no reading yet")
        frame = source.show()
        wait_for(lambda: loop.latest().drift != DriftUnmeasured("no reading yet"))
        assert _served(loop, frame).l1 == REPORTING


def test_with_no_first_image_l1_reports_and_drift_says_why(tmp_path: Path) -> None:
    source = FakeSource()
    checks = FakeChecks(px=100.0)
    with running(_loop(_archive(tmp_path, images=[]), source, checks)) as loop:
        result = _served(loop, source.show())
    assert result.l1 == REPORTING
    assert result.drift == DriftUnmeasured("no first image")
    assert checks.built == 0


def test_with_a_first_image_the_archive_does_not_hold_l1_reports(tmp_path: Path) -> None:
    source = FakeSource()
    archive = _archive(tmp_path, images=[{"filename": "absent.png"}])
    with running(_loop(archive, source, FakeChecks(px=100.0))) as loop:
        frame = source.show()
        wait_for(lambda: loop.latest().drift == DriftUnmeasured("no first image"))
        assert _served(loop, frame).l1 == REPORTING


def test_with_no_dpt_drift_says_why_and_never_refuses(tmp_path: Path) -> None:
    source = FakeSource()
    checks = FakeChecks(px=100.0)
    with running(_loop(_archive(tmp_path, calibration={}), source, checks)) as loop:
        result = _served(loop, source.show())
    assert result.drift == DriftUnmeasured("DPT unresolved")
    assert {entry.reason for entry in result.l1.values()} == {"DPT unresolved"}
    assert checks.reads == 0


def test_a_first_image_that_does_not_decode_is_a_failed_check(tmp_path: Path) -> None:
    source = FakeSource()
    archive = _archive(tmp_path)
    (archive / "first.png").write_bytes(b"not an image")
    with running(_loop(archive, source)) as loop:
        frame = source.show()
        wait_for(lambda: loop.latest().drift == DriftUnmeasured("check failed"))
        assert _served(loop, frame).l1 == REPORTING


def test_a_first_image_too_small_to_read_is_a_failed_check(tmp_path: Path) -> None:
    source = FakeSource()
    archive = _archive(tmp_path)
    (archive / "first.png").write_bytes(_png(np.zeros((16, 64, 3), np.uint8)))
    with running(_loop(archive, source)) as loop:
        frame = source.show()
        wait_for(lambda: loop.latest().drift == DriftUnmeasured("check failed"))
        assert _served(loop, frame).l1 == REPORTING


def test_a_reading_that_fails_ends_a_refusal_and_a_good_one_is_taken_again(
    tmp_path: Path,
) -> None:
    source = FakeSource()
    checks = FakeChecks(px=100.0)
    with running(_loop(_archive(tmp_path), source, checks)) as loop:
        source.show()
        _l1_is(loop, {"s1": DRIFT, "s2": DRIFT})
        checks.px = RuntimeError("the check failed")
        assert _l1_is(loop, REPORTING).drift == DriftUnmeasured("check failed")
        checks.px = 100.0
        _l1_is(loop, {"s1": DRIFT, "s2": DRIFT})


def test_a_frame_that_does_not_decode_is_a_failed_check(tmp_path: Path) -> None:
    source = FakeSource()
    with running(_loop(_archive(tmp_path), source, FakeChecks())) as loop:
        source.show(b"not an image")
        wait_for(lambda: loop.latest().drift == DriftUnmeasured("check failed"))


def test_turned_off_there_is_no_reading_and_no_refusal(tmp_path: Path) -> None:
    source = FakeSource()
    checks = FakeChecks(px=100.0)
    with running(_loop(_archive(tmp_path), source, checks, drift_tolerance_g=None)) as loop:
        result = _served(loop, source.show())
        loop.reload()
        time.sleep(0.05)
        assert loop.latest().l1 == REPORTING
    assert result.drift is None
    assert result.l1 == REPORTING
    assert (checks.built, checks.reads) == (0, 0)


def test_a_reload_measures_against_the_new_first_image(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    source = FakeSource()
    source.show(_picture())
    with running(_loop(archive, source)) as loop:
        assert _first_reading(loop) == DriftReading(0.0, TOLERANCE_PX)
        _l1_is(loop, REPORTING)

        write_archive(archive, SENSORS, images=[{"filename": "second.png"}])
        loop.reload()

        refused = _l1_is(loop, {"s1": DRIFT, "s2": DRIFT})
        assert refused.drift == DriftReading(20.0, TOLERANCE_PX)


def test_a_reload_that_removes_the_first_image_ends_a_refusal_at_once(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    source = FakeSource()
    checks = FakeChecks(px=100.0)
    with running(_loop(archive, source, checks)) as loop:
        source.show()
        _l1_is(loop, {"s1": DRIFT, "s2": DRIFT})
        write_archive(archive, SENSORS, images=[])
        loop.reload()
        assert loop.latest().drift == DriftUnmeasured("no first image")


def test_a_refusal_lasts_through_a_reload_until_the_next_reading(tmp_path: Path) -> None:
    source = FakeSource()
    checks = FakeChecks(px=100.0)
    with running(_loop(_archive(tmp_path), source, checks, drift_every=60)) as loop:
        source.show()
        _l1_is(loop, {"s1": DRIFT, "s2": DRIFT})
        loop.reload()
        assert loop.latest().l1 == {"s1": DRIFT, "s2": DRIFT}


def test_a_reading_begun_before_a_reload_is_not_taken(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    source = FakeSource()
    checks = FakeChecks(px=100.0)
    checks.gate = threading.Event()
    with running(_loop(archive, source, checks)) as loop:
        source.show()
        assert checks.reading.wait(5)
        write_archive(archive, SENSORS, images=[])
        loop.reload()
        reads = checks.reads
        checks.gate.set()
        time.sleep(0.05)
        assert loop.latest().drift == DriftUnmeasured("no first image")
        assert checks.reads == reads


def test_a_slow_check_does_not_delay_l1(tmp_path: Path) -> None:
    source = FakeSource()
    checks = FakeChecks()
    checks.gate = threading.Event()
    with running(_loop(_archive(tmp_path), source, checks)) as loop:
        source.show()
        assert checks.reading.wait(5)
        for _ in range(3):  # The check is still on its one reading.
            result = _served(loop, source.show())
            assert result.l1 == REPORTING
        assert checks.reads == 1
        checks.gate.set()
