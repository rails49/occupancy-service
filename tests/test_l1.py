"""The L1 loop against a fake capture source, with the packaged model and with fake scorers."""

import io
import json
import math
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
import pytest
import r49
from fake_ffmpeg import HEIGHT as FAKE_HEIGHT
from fake_ffmpeg import WIDTH as FAKE_WIDTH
from fake_ffmpeg import install, noise_jpeg, stream
from PIL import Image

from occupancy_service.capture import Capture, CaptureSettings, CaptureStatus, Frame
from occupancy_service.l1 import L1Change, L1Entry, L1Loop, L1Result, L1Settings, Scoring

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


def served(loop: L1Loop, frame: Frame) -> L1Result:
    """Wait until ``frame`` is the frame of the newest result."""

    def found() -> L1Result | None:
        result = loop.latest()
        return result if result.frame == frame else None

    return wait_for(found)


def reasons(result: L1Result) -> dict[str, str | None]:
    return {id: entry.reason for id, entry in result.l1.items()}


def test_the_packaged_model_turns_a_frame_into_l1(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()

    with running(L1Loop(L1Settings(archive), source)) as loop:
        result = wait_for(lambda: (r := loop.latest()).frame and r)

    assert result.frame == frame
    assert result.archive_id == "archive-1"
    assert result.threshold is not None and 0 < result.threshold < 1
    score = result.scores["s1"]
    assert 0 <= score <= 1
    # This frame is a flat gray, which the packaged model calls occupied.
    assert result.l1 == {"s1": L1Entry("occupied", confidence=score)}
    assert result.patches["s1"].shape == (48, 96, 3)


def test_at_the_threshold_a_sensor_is_occupied_with_its_confidence(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()

    with running(L1Loop(L1Settings(archive), source, FakeScorer({"s1": 0.5}))) as loop:
        result = served(loop, frame)

    assert result.l1["s1"] == L1Entry("occupied", confidence=0.5)
    assert result.scores == {"s1": 0.5}
    assert result.threshold == 0.5


def test_below_the_threshold_a_sensor_is_clear_with_a_score_and_no_confidence(
    tmp_path: Path,
) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()

    with running(L1Loop(L1Settings(archive), source, FakeScorer({"s1": 0.49}))) as loop:
        result = served(loop, frame)

    assert result.l1["s1"] == L1Entry("clear")
    assert result.l1["s1"].confidence is None
    assert result.scores == {"s1": 0.49}


def test_a_sensors_heading_is_the_nearest_tracks_and_its_position_its_own(tmp_path: Path) -> None:
    vertical = {
        "nodes": [
            {"x": 200, "y": 0, "heading": math.pi / 2},
            {"x": 200, "y": 240, "heading": math.pi / 2},
        ]
    }
    archive = write_archive(tmp_path / "a", {"s1": (162, 122)}, tracks=[TRACK, vertical])
    source = FakeSource()
    frame = source.show()
    scorer = FakeScorer({"s1": 0.9})

    with running(L1Loop(L1Settings(archive), source, scorer)) as loop:
        served(loop, frame)

    # (162, 122) is 2 pixels from the horizontal track and 38 from the vertical one.
    sensor = scorer.sensors[0][0]
    assert (sensor.x, sensor.y) == (162, 122)
    assert sensor.heading == pytest.approx(0)


def test_the_loop_reads_no_image(tmp_path: Path) -> None:
    named = [{"filename": "not-in-the-archive.jpg"}]
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)}, images=named)
    source = FakeSource()
    frame = source.show()

    with running(L1Loop(L1Settings(archive), source, FakeScorer({"s1": 0.9}))) as loop:
        assert served(loop, frame).l1["s1"].state == "occupied"


def test_no_frames_when_the_camera_is_lost(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120), "s2": (170, 120)})
    source = FakeSource()
    frame = source.show()

    with running(L1Loop(L1Settings(archive), source, FakeScorer({"s1": 0.9, "s2": 0.9}))) as loop:
        served(loop, frame)
        source.state = "lost"  # latest() goes on returning the last frame, as Capture's does.
        result = loop.latest()

    assert result.frame is None
    assert reasons(result) == {"s1": "no frames", "s2": "no frames"}
    assert all(entry.state == "unknown" for entry in result.l1.values())
    assert result.scores == {}
    assert result.patches == {}


def test_a_result_from_before_the_loss_is_not_served_when_the_camera_returns(
    tmp_path: Path,
) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()

    with running(L1Loop(L1Settings(archive), source, FakeScorer({"s1": 0.9}))) as loop:
        served(loop, frame)
        source.state = "lost"
        loop.latest()
        source.state = "ok"
        result = loop.latest()

        assert result.frame is None
        assert reasons(result) == {"s1": "no frames"}
        served(loop, source.show())


def test_no_frames_before_the_first_frame(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    source.state = "lost"

    with running(L1Loop(L1Settings(archive), source, FakeScorer({"s1": 0.9}))) as loop:
        result = loop.latest()

    assert result.frame is None
    assert reasons(result) == {"s1": "no frames"}


def test_scoring_stalled_with_a_scorer_that_hangs_while_frames_keep_arriving(
    tmp_path: Path,
) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()
    release = threading.Event()
    scorer = FakeScorer({"s1": 0.9})

    try:
        with running(L1Loop(L1Settings(archive), source, scorer)) as loop:
            served(loop, frame)
            scorer.gate = release
            source.show()
            assert scorer.started.acquire(timeout=5)  # For the first frame.
            assert scorer.started.acquire(timeout=5)  # It now hangs on the second.
            assert loop.latest().l1["s1"].state == "occupied"

            def stalled() -> L1Result | None:
                source.show()
                time.sleep(0.01)
                result = loop.latest()
                return result if result.l1["s1"].state == "unknown" else None

            before = time.monotonic()
            result = wait_for(stalled)
            waited = time.monotonic() - before
    finally:
        release.set()

    assert reasons(result) == {"s1": "scoring stalled"}
    assert result.frame == frame  # It still comes from the newest result.
    assert 0.5 < waited < 3  # About 1 s, with room for a busy machine.


def test_scoring_stalled_with_a_scorer_that_raises(tmp_path: Path) -> None:
    class Raises(FakeScorer):
        def score(
            self, frame: npt.NDArray[np.uint8], sensors: Sequence[classifier.Sensor], dpt: float
        ) -> Scoring:
            raise RuntimeError("the model broke")

    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    source.show()

    with running(L1Loop(L1Settings(archive), source, Raises({}))) as loop:
        wait_for(lambda: loop.latest().l1["s1"].reason == "scoring stalled")


def test_wrong_frame_size(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show(jpeg(WIDTH // 2, HEIGHT))
    scorer = FakeScorer({"s1": 0.9})

    with running(L1Loop(L1Settings(archive), source, scorer)) as loop:
        result = served(loop, frame)

    assert reasons(result) == {"s1": "wrong frame size"}
    assert scorer.calls == 0


def test_dpt_unresolved(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)}, calibration={"points": []})
    source = FakeSource()
    frame = source.show()
    scorer = FakeScorer({"s1": 0.9})

    with running(L1Loop(L1Settings(archive), source, scorer)) as loop:
        result = served(loop, frame)

    assert reasons(result) == {"s1": "DPT unresolved"}
    assert scorer.calls == 0


def test_no_track_past_the_limit_only(tmp_path: Path) -> None:
    # The DPT is 16.494, so 0.3 G is 4.95 pixels from the track along y = 120.
    sensors = {"near": (100, 124.9), "far": (100, 125), "on": (100, 120)}
    archive = write_archive(tmp_path / "a", sensors)
    source = FakeSource()
    frame = source.show()
    scorer = FakeScorer({"near": 0.9, "on": 0.9})

    with running(L1Loop(L1Settings(archive), source, scorer)) as loop:
        result = served(loop, frame)

    assert reasons(result) == {"near": None, "far": "no track", "on": None}
    assert [s.id for s in scorer.sensors[0]] == ["near", "on"]


def test_the_no_track_limit_is_a_setting(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (100, 125)})
    source = FakeSource()
    frame = source.show()
    settings = L1Settings(archive, no_track_limit_g=0.4)

    with running(L1Loop(settings, source, FakeScorer({"s1": 0.9}))) as loop:
        assert served(loop, frame).l1["s1"] == L1Entry("occupied", confidence=0.9)


def test_no_track_on_an_archive_with_no_tracks(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (100, 120)}, tracks=[])
    source = FakeSource()
    frame = source.show()

    with running(L1Loop(L1Settings(archive), source, FakeScorer({"s1": 0.9}))) as loop:
        assert reasons(served(loop, frame)) == {"s1": "no track"}


def test_outside_the_frame_when_the_classifier_returns_none(tmp_path: Path) -> None:
    # On the line of the track, past the end of the frame: the packaged model returns None.
    track = {"nodes": [{"x": 0, "y": 120, "heading": 0}, {"x": 900, "y": 120, "heading": 0}]}
    archive = write_archive(tmp_path / "a", {"in": (160, 120), "out": (500, 120)}, tracks=[track])
    source = FakeSource()
    frame = source.show()

    with running(L1Loop(L1Settings(archive), source)) as loop:
        result = served(loop, frame)

    assert result.l1["out"] == L1Entry("unknown", reason="outside the frame")
    assert "out" not in result.scores
    assert "out" not in result.patches
    assert result.l1["in"].state == "occupied"


def test_no_model_when_the_model_does_not_load(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()
    settings = L1Settings(archive, model=tmp_path / "missing.onnx")

    with running(L1Loop(settings, source)) as loop:
        result = served(loop, frame)

    assert reasons(result) == {"s1": "no model"}
    assert result.threshold is None
    assert result.frame == frame


def test_an_archive_with_no_id_runs_with_every_sensor_unknown(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120), "s2": (170, 120)}, id=None)
    source = FakeSource()
    frame = source.show()
    scorer = FakeScorer({"s1": 0.9, "s2": 0.9})

    with running(L1Loop(L1Settings(archive), source, scorer)) as loop:
        result = served(loop, frame)
        second = served(loop, source.show())  # It goes on running.

    assert reasons(result) == {"s1": "archive has no id", "s2": "archive has no id"}
    assert result.archive_id is None
    assert second.frame is not None
    assert scorer.calls == 0


def test_a_reason_of_the_archive_stays_while_the_camera_is_lost(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)}, id=None)
    source = FakeSource()
    source.state = "lost"

    with running(L1Loop(L1Settings(archive), source)) as loop:
        assert reasons(loop.latest()) == {"s1": "archive has no id"}


def test_a_frame_that_does_not_decode_is_dropped(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    good = source.show()
    scorer = FakeScorer({"s1": 0.9})

    with running(L1Loop(L1Settings(archive), source, scorer)) as loop:
        served(loop, good)
        source.show(b"not a jpeg")
        truncated = source.show(jpeg()[:100])
        time.sleep(0.1)

        assert truncated.id > good.id
        assert loop.latest().frame == good
        assert scorer.calls == 1
        served(loop, source.show())


def test_the_same_frame_is_not_scored_twice(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()
    scorer = FakeScorer({"s1": 0.9})

    with running(L1Loop(L1Settings(archive), source, scorer)) as loop:
        served(loop, frame)
        time.sleep(0.1)
        calls = scorer.calls

    assert calls == 1


def test_a_slow_scorer_takes_the_newest_frame(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    source.show()
    release = threading.Event()
    scorer = FakeScorer({"s1": 0.9}, release)

    try:
        with running(L1Loop(L1Settings(archive), source, scorer)) as loop:
            assert scorer.started.acquire(timeout=5)
            source.show()
            source.show()
            newest = source.show()
            release.set()
            served(loop, newest)
    finally:
        release.set()

    assert scorer.calls == 2


def test_a_results_frame_id_and_l1_come_from_one_frame(tmp_path: Path) -> None:
    class ByBrightness(FakeScorer):
        """Scores a sensor by the frame it was handed: white is occupied, black is clear."""

        def score(
            self, frame: npt.NDArray[np.uint8], sensors: Sequence[classifier.Sensor], dpt: float
        ) -> Scoring:
            p = 1.0 if frame.mean() > 127 else 0.0
            return Scoring({s.id: classifier.Result(p, None) for s in sensors}, 0.0, 0.0)

    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    white, black = jpeg(color="white"), jpeg(color="black")
    source.show(white)

    with running(L1Loop(L1Settings(archive), source, ByBrightness({}))) as loop:
        for data in [black, white] * 20:
            source.show(data)
            result = loop.latest()
            if result.frame is not None:  # Often an older frame than the one just shown.
                expected = "occupied" if result.frame.jpeg == white else "clear"
                assert result.l1["s1"].state == expected
            time.sleep(0.005)


def test_a_reload_during_scoring_keeps_the_old_sensors_for_the_frame_in_progress(
    tmp_path: Path,
) -> None:
    archive = write_archive(tmp_path / "a", {"kept": (160, 120), "old": (170, 120)})
    source = FakeSource()
    first = source.show()
    release = threading.Event()
    scorer = FakeScorer({"kept": 0.9, "old": 0.9, "new": 0.9})

    try:
        with running(L1Loop(L1Settings(archive), source, scorer)) as loop:
            served(loop, first)
            assert scorer.started.acquire(timeout=5)
            scorer.gate = release
            in_progress = source.show()
            assert scorer.started.acquire(timeout=5)

            write_archive(tmp_path / "a", {"kept": (160, 120), "new": (180, 120)}, id="archive-2")
            loop.reload()
            assert set(loop.latest().l1) == {"kept", "old"}  # Nothing has come of the new one yet.

            release.set()
            finished = served(loop, in_progress)
            assert set(finished.l1) == {"kept", "old"}
            assert finished.archive_id == "archive-1"
            assert [s.id for s in scorer.sensors[1]] == ["kept", "old"]

            next_result = served(loop, source.show())
    finally:
        release.set()

    assert set(next_result.l1) == {"kept", "new"}
    assert next_result.archive_id == "archive-2"
    assert [s.id for s in scorer.sensors[2]] == ["kept", "new"]


def test_a_reload_works_out_the_headings_again(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()
    scorer = FakeScorer({"s1": 0.9})
    turned = {
        "nodes": [
            {"x": 160, "y": 0, "heading": math.pi / 2},
            {"x": 160, "y": 240, "heading": math.pi / 2},
        ]
    }

    with running(L1Loop(L1Settings(archive), source, scorer)) as loop:
        served(loop, frame)
        write_archive(tmp_path / "a", {"s1": (160, 120)}, tracks=[turned])
        loop.reload()
        served(loop, source.show())

    assert scorer.sensors[0][0].heading == pytest.approx(0)
    assert scorer.sensors[1][0].heading == pytest.approx(math.pi / 2)


def test_a_reload_that_mints_an_id_ends_archive_has_no_id(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)}, id=None)
    source = FakeSource()
    frame = source.show()

    with running(L1Loop(L1Settings(archive), source, FakeScorer({"s1": 0.9}))) as loop:
        served(loop, frame)
        write_archive(tmp_path / "a", {"s1": (160, 120)})
        loop.reload()
        result = served(loop, source.show())

    assert result.l1["s1"].state == "occupied"
    assert result.archive_id == "archive-1"


def test_a_reload_that_fails_raises_and_keeps_the_manifest_in_use(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()

    with running(L1Loop(L1Settings(archive), source, FakeScorer({"s1": 0.9}))) as loop:
        served(loop, frame)
        (archive / "manifest.json").write_text("not json")
        with pytest.raises(r49.R49Error):
            loop.reload()
        assert served(loop, source.show()).l1["s1"].state == "occupied"


def test_a_reload_takes_effect_at_once_with_no_result_to_keep(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    source.state = "lost"

    with running(L1Loop(L1Settings(archive), source)) as loop:
        write_archive(tmp_path / "a", {"s2": (160, 120)})
        loop.reload()
        assert set(loop.latest().l1) == {"s2"}


def next_change(subscription: Any) -> L1Change:
    change = subscription.get(timeout=5)
    assert change is not None
    return change


def test_a_subscriber_gets_the_state_at_subscribing_and_then_each_change(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()
    scorer = FakeScorer({"s1": 0.9})

    with running(L1Loop(L1Settings(archive), source, scorer)) as loop:
        served(loop, frame)
        subscription = loop.subscribe()
        assert subscription.state == {"s1": L1Entry("occupied", confidence=0.9)}

        scorer.scores = {"s1": 0.1}
        served(loop, source.show())
        assert next_change(subscription) == L1Change("s1", L1Entry("clear"))

        source.state = "lost"
        change = next_change(subscription)
        assert change == L1Change("s1", L1Entry("unknown", reason="no frames"))


def test_a_new_confidence_is_not_a_change(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()
    scorer = FakeScorer({"s1": 0.9})

    with running(L1Loop(L1Settings(archive), source, scorer)) as loop:
        served(loop, frame)
        subscription = loop.subscribe()
        scorer.scores = {"s1": 0.8}
        result = served(loop, source.show())

        assert result.l1["s1"].confidence == 0.8
        assert subscription.get(timeout=0.1) is None
        assert loop.stats().changes == {"s1": 1}  # Its first result; not this one.
        assert loop.subscribe().state["s1"].confidence == 0.8


def test_a_new_reason_is_a_change(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()
    release = threading.Event()
    scorer = FakeScorer({"s1": 0.9})

    try:
        with running(L1Loop(L1Settings(archive), source, scorer)) as loop:
            served(loop, frame)
            source.state = "lost"
            subscription = loop.subscribe()
            assert subscription.state["s1"].reason == "no frames"

            source.state = "ok"
            scorer.gate = release
            source.show()
            # Unknown either side, with nothing reading the loop: the reason alone changes.
            change = next_change(subscription)
            assert change == L1Change("s1", L1Entry("unknown", reason="scoring stalled"))
    finally:
        release.set()


def test_a_closed_subscription_gets_nothing_more(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()

    with running(L1Loop(L1Settings(archive), source, FakeScorer({"s1": 0.9}))) as loop:
        served(loop, frame)
        subscription = loop.subscribe()
        subscription.close()
        source.state = "lost"
        loop.latest()

        assert subscription.get(timeout=0.01) is None


def test_a_reload_tells_a_subscriber_of_a_new_sensor_without_counting_a_change(
    tmp_path: Path,
) -> None:
    archive = write_archive(tmp_path / "a", {"kept": (160, 120), "old": (170, 120)})
    source = FakeSource()
    frame = source.show()
    scorer = FakeScorer({"kept": 0.9, "old": 0.9, "new": 0.9})

    with running(L1Loop(L1Settings(archive), source, scorer)) as loop:
        served(loop, frame)
        subscription = loop.subscribe()
        write_archive(tmp_path / "a", {"kept": (160, 120), "new": (180, 120)})
        loop.reload()
        served(loop, source.show())

        assert next_change(subscription) == L1Change("new", L1Entry("occupied", confidence=0.9))
        assert loop.stats().changes == {"kept": 1, "new": 0}


def test_stats_hold_the_stage_times_the_camera_and_the_rate(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    source.show()
    scorer = FakeScorer({"s1": 0.9})

    with running(L1Loop(L1Settings(archive), source, scorer)) as loop:
        assert loop.stats().rate_fps == 0
        for _ in range(5):
            served(loop, source.show())
            time.sleep(0.02)
        stats = loop.stats()

    assert stats.cut_ms == 1.0
    assert stats.classify_ms == 2.0
    assert stats.decode_ms is not None and stats.decode_ms > 0
    assert stats.capture_ms is not None and stats.capture_ms >= 0
    assert 0 < stats.rate_fps < 60  # At most one result in each pause of 20 ms.
    assert stats.camera == source.status()
    assert stats.changes == {"s1": 1}


def test_the_packaged_model_is_timed_stage_by_stage(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()

    with running(L1Loop(L1Settings(archive), source)) as loop:
        served(loop, frame)
        stats = loop.stats()

    assert stats.cut_ms is not None and stats.cut_ms > 0
    assert stats.classify_ms is not None and stats.classify_ms > 0


def test_stats_before_any_frame(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    source.state = "lost"

    loop = L1Loop(L1Settings(archive), source, FakeScorer({}))
    stats = loop.stats()

    assert (stats.capture_ms, stats.decode_ms, stats.cut_ms, stats.classify_ms) == (None,) * 4
    assert stats.changes == {"s1": 0}


def test_frames_from_a_real_capture_become_l1(tmp_path: Path) -> None:
    """``Capture`` is a capture source, through its fake ``ffmpeg``."""
    fake = install(tmp_path, [stream(noise_jpeg())])
    track = {"nodes": [{"x": 0, "y": 24, "heading": 0}, {"x": 64, "y": 24, "heading": 0}]}
    manifest = write_archive(tmp_path / "a", {"s1": (32, 24)}, tracks=[track])
    text = json.loads((manifest / "manifest.json").read_text())
    text["camera"]["resolution"] = {"width": FAKE_WIDTH, "height": FAKE_HEIGHT}
    (manifest / "manifest.json").write_text(json.dumps(text))
    resolution = r49.load(manifest).manifest.camera.resolution
    capture = Capture(CaptureSettings(resolution=resolution, ffmpeg=fake.path))
    capture.start()

    def scored(loop: L1Loop) -> L1Result | None:
        result = loop.latest()
        return result if result.frame is not None else None

    try:
        with running(L1Loop(L1Settings(manifest), capture)) as loop:
            result = wait_for(lambda: scored(loop))
    finally:
        capture.stop()

    assert result.l1["s1"].state in ("occupied", "clear")
    assert 0 <= result.scores["s1"] <= 1
