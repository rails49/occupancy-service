"""The L1 loop against a fake capture source, with the packaged model and with fake scorers."""

import json
import math
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import classifier
import numpy as np
import numpy.typing as npt
import pytest
import r49
from classifier.runtime import Engine
from fake_ffmpeg import HEIGHT as FAKE_HEIGHT
from fake_ffmpeg import WIDTH as FAKE_WIDTH
from fake_ffmpeg import install, noise_jpeg, stream
from fake_l1 import (
    HEIGHT,
    TRACK,
    WIDTH,
    FakeScorer,
    FakeSource,
    jpeg,
    running,
    wait_for,
    write_archive,
)

from occupancy_service.capture import Capture, CaptureSettings, Frame
from occupancy_service.engine_choice import Choice
from occupancy_service.l1 import L1Entry, L1Loop, L1Result, L1Settings, Scoring
from occupancy_service.l1_changes import L1Change


def served(loop: L1Loop, frame: Frame) -> L1Result:
    """Wait until ``frame`` is the frame of the newest result."""

    def found() -> L1Result | None:
        result = loop.latest()
        return result if result.frame == frame else None

    return wait_for(found)


def reasons(result: L1Result) -> dict[str, str | None]:
    return {id: entry.reason for id, entry in result.l1.items()}


@pytest.mark.parametrize("engine", ["onnxruntime", "openvino"])
def test_the_packaged_model_turns_a_frame_into_l1(tmp_path: Path, engine: Engine) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()

    with running(L1Loop(L1Settings(archive, engine=engine), source)) as loop:
        result = wait_for(lambda: (r := loop.latest()).frame and r)
        stats = loop.stats()

    assert stats.classifier == Choice(engine, "CPU")
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


def test_the_time_until_scoring_stalled_is_a_setting(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()
    release = threading.Event()
    scorer = FakeScorer({"s1": 0.9})
    settings = L1Settings(archive, stalled_after=0.2)

    try:
        with running(L1Loop(settings, source, scorer)) as loop:
            served(loop, frame)
            scorer.gate = release
            before = time.monotonic()

            def stalled() -> L1Result | None:
                source.show()
                time.sleep(0.01)
                result = loop.latest()
                return result if result.l1["s1"].state == "unknown" else None

            result = wait_for(stalled)
            waited = time.monotonic() - before
    finally:
        release.set()

    assert reasons(result) == {"s1": "scoring stalled"}
    assert waited < 0.8


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
    assert loop.stats().classifier is None


def test_with_no_openvino_the_default_settings_run_onnxruntime_on_the_cpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "openvino", None)  # import openvino raises ImportError.
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()

    with running(L1Loop(L1Settings(archive), source)) as loop:
        result = served(loop, frame)
        stats = loop.stats()

    assert stats.classifier == Choice("onnxruntime", "CPU")
    assert result.l1["s1"].state == "occupied"


def test_with_a_scorer_given_no_classifier_is_named(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    with running(L1Loop(L1Settings(archive), FakeSource(), FakeScorer({"s1": 0.1}))) as loop:
        assert loop.stats().classifier is None


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


def test_a_subscription_is_outdated_only_from_the_first_result_under_the_new_manifest(
    tmp_path: Path,
) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()
    scorer = FakeScorer({"s1": 0.9, "s2": 0.9})

    with running(L1Loop(L1Settings(archive), source, scorer)) as loop:
        served(loop, frame)
        subscription = loop.subscribe()
        write_archive(tmp_path / "a", {"s1": (160, 120), "s2": (170, 120)})
        loop.reload()
        assert not subscription.outdated

        served(loop, source.show())

        assert subscription.outdated
        assert not loop.subscribe().outdated


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


def test_a_result_read_with_its_stats_has_that_frames_times(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()
    frame = source.show()

    with running(L1Loop(L1Settings(archive), source, FakeScorer({"s1": 0.9}))) as loop:
        served(loop, frame)
        result, stats = loop.read()

    assert result.frame == frame
    assert (stats.cut_ms, stats.classify_ms) == (1.0, 2.0)
    assert stats.changes == {"s1": 1}


def test_a_result_says_when_its_frame_is_not_at_the_cameras_resolution(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"s1": (160, 120)})
    source = FakeSource()

    with running(L1Loop(L1Settings(archive), source, FakeScorer({"s1": 0.9}))) as loop:
        assert served(loop, source.show()).wrong_frame_size is False
        assert served(loop, source.show(jpeg(WIDTH // 2, HEIGHT))).wrong_frame_size is True
