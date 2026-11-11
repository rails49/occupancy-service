"""Pausing the L1 loop, and following control's track power to do it."""

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fake_l1 import (
    FakeChecks,
    FakeDetector,
    FakeScorer,
    FakeSource,
    picture,
    running,
    wait_for,
    write_archive,
)
from mqtt_broker import Broker, mosquitto
from paho.mqtt import client as paho
from paho.mqtt.enums import CallbackAPIVersion

from occupancy_service.l1 import L1Entry, L1Loop, L1Result, L1Settings
from occupancy_service.power import PowerGate, PowerSettings, is_off

SENSORS = {"s1": (160.0, 120.0)}
OCCUPIED = {"s1": L1Entry("occupied", confidence=0.9)}
POWER_OFF = {"s1": L1Entry("unknown", reason="track power off")}
TOPIC = "tc49/layout/state/power"
SETTLE = 0.1  # Seconds for a call already under way to finish.


def _archive(tmp_path: Path) -> Path:
    """An archive with a first image, so that the drift check reads frames."""
    folder = write_archive(tmp_path / "archive", SENSORS, images=[{"filename": "first.png"}])
    (folder / "first.png").write_bytes(picture())
    return folder


def _loop(
    tmp_path: Path,
    source: FakeSource,
    scorer: FakeScorer | None = None,
    checks: FakeChecks | None = None,
    detector: FakeDetector | None = None,
    **settings: Any,
) -> L1Loop:
    settings = {"drift_every": 0.01, "detect_every": 0.01, "stalled_after": 60.0, **settings}
    return L1Loop(
        L1Settings(_archive(tmp_path), **settings),
        source,
        scorer or FakeScorer({"s1": 0.9}),
        make_drift_check=checks or FakeChecks(),
        detector=detector or FakeDetector(),
    )


def _where(loop: L1Loop, holds: Callable[[L1Result], bool]) -> L1Result:
    def found() -> L1Result | None:
        result = loop.latest()
        return result if holds(result) else None

    return wait_for(found)


def test_while_paused_nothing_is_scored_detected_or_checked_for_drift(tmp_path: Path) -> None:
    source = FakeSource()
    source.show(picture())
    scorer, checks, detector = FakeScorer({"s1": 0.9}), FakeChecks(), FakeDetector()
    with running(_loop(tmp_path, source, scorer, checks, detector)) as loop:
        _where(loop, lambda r: r.l1 == OCCUPIED)
        wait_for(lambda: checks.reads and detector.runs)

        loop.pause(True)
        result = loop.latest()
        assert result.l1 == POWER_OFF
        assert result.frame is None
        time.sleep(SETTLE)
        counts = scorer.calls, checks.reads, detector.runs
        for _ in range(5):
            source.show(picture())
            time.sleep(0.02)

        assert (scorer.calls, checks.reads, detector.runs) == counts
        assert loop.latest().l1 == POWER_OFF


def test_after_a_pause_the_reason_stands_until_the_first_result(tmp_path: Path) -> None:
    source = FakeSource()
    source.show(picture())
    gate = threading.Event()
    gate.set()
    scorer = FakeScorer({"s1": 0.9}, gate)
    with running(_loop(tmp_path, source, scorer)) as loop:
        _where(loop, lambda r: r.l1 == OCCUPIED)
        loop.pause(True)
        time.sleep(SETTLE)
        gate.clear()
        calls = scorer.calls

        loop.pause(False)
        frame = source.show(picture())
        wait_for(lambda: scorer.calls > calls)
        assert loop.latest().l1 == POWER_OFF

        gate.set()
        assert _where(loop, lambda r: r.frame == frame).l1 == OCCUPIED


def test_a_scorer_that_hangs_after_a_pause_reads_scoring_stalled(tmp_path: Path) -> None:
    source = FakeSource()
    source.show(picture())
    gate = threading.Event()
    gate.set()
    scorer = FakeScorer({"s1": 0.9}, gate)
    with running(_loop(tmp_path, source, scorer, stalled_after=0.2)) as loop:
        _where(loop, lambda r: r.l1 == OCCUPIED)
        loop.pause(True)
        time.sleep(SETTLE)
        gate.clear()

        loop.pause(False)
        source.show(picture())
        try:
            _where(loop, lambda r: r.l1["s1"].reason == "scoring stalled")
        finally:
            gate.set()


def test_a_result_scored_as_the_pause_came_is_dropped(tmp_path: Path) -> None:
    source = FakeSource()
    gate = threading.Event()
    scorer = FakeScorer({"s1": 0.9}, gate)
    with running(_loop(tmp_path, source, scorer)) as loop:
        source.show(picture())
        wait_for(lambda: scorer.calls)

        loop.pause(True)
        gate.set()
        time.sleep(SETTLE)

        result = loop.latest()
        assert result.l1 == POWER_OFF
        assert result.frame is None


def test_a_lost_camera_reads_no_frames_while_paused(tmp_path: Path) -> None:
    source = FakeSource()
    source.show(picture())
    with running(_loop(tmp_path, source)) as loop:
        _where(loop, lambda r: r.l1 == OCCUPIED)
        loop.pause(True)

        source.state = "lost"
        _where(loop, lambda r: r.l1["s1"].reason == "no frames")
        source.state = "ok"
        _where(loop, lambda r: r.l1 == POWER_OFF)


@pytest.mark.parametrize(
    ("payload", "off"),
    [
        (b'{"power": "off"}', True),
        (b'{"power": "off", "stamp": 3}', True),
        (b'{"power": "on"}', False),
        (b'{"power": "draining"}', False),
        (b'{"power": "OFF"}', False),
        (b'{"power": "off"', False),
        (b'["off"]', False),
        (b"off", False),
        (b"", False),
        (b"\xff", False),
    ],
)
def test_only_power_off_reads_off(payload: bytes, off: bool) -> None:
    assert is_off(payload) is off


class Pauses:
    """Stands in for the loop, and keeps whether it is paused."""

    def __init__(self) -> None:
        self.paused = False

    def pause(self, paused: bool) -> None:
        self.paused = paused


@pytest.fixture
def broker(tmp_path: Path) -> Iterator[Broker]:
    binary = mosquitto()
    if binary is None:
        pytest.skip("mosquitto is not installed")
    broker = Broker(binary, tmp_path)
    broker.start()
    yield broker
    broker.stop()


def publish(broker: Broker, payload: bytes) -> None:
    """``payload`` on the power topic, retained as control's row is."""
    client = paho.Client(CallbackAPIVersion.VERSION2, protocol=paho.MQTTv311)
    client.connect("127.0.0.1", broker.port)
    client.loop_start()
    try:
        client.publish(TOPIC, payload, qos=1, retain=True).wait_for_publish(5)
    finally:
        client.disconnect()
        client.loop_stop()


@pytest.fixture
def gate(broker: Broker) -> Iterator[tuple[PowerGate, Pauses]]:
    loop = Pauses()
    gate = PowerGate(PowerSettings(TOPIC, "127.0.0.1", broker.port), loop)
    gate.start()
    yield gate, loop
    gate.stop()


def test_a_retained_off_pauses_the_loop_on_connect(broker: Broker) -> None:
    publish(broker, b'{"power": "off"}')
    loop = Pauses()
    gate = PowerGate(PowerSettings(TOPIC, "127.0.0.1", broker.port), loop)
    gate.start()
    try:
        wait_for(lambda: loop.paused)
    finally:
        gate.stop()


@pytest.mark.parametrize("payload", [b'{"power": "on"}', b'{"power": "draining"}', b"?"])
def test_anything_but_off_resumes_the_loop(
    broker: Broker, gate: tuple[PowerGate, Pauses], payload: bytes
) -> None:
    _, loop = gate
    publish(broker, b'{"power": "off"}')
    wait_for(lambda: loop.paused)

    publish(broker, payload)
    wait_for(lambda: not loop.paused)


def test_a_lost_broker_resumes_the_loop_and_a_new_one_is_followed(
    broker: Broker, gate: tuple[PowerGate, Pauses]
) -> None:
    _, loop = gate
    publish(broker, b'{"power": "off"}')
    wait_for(lambda: loop.paused)

    broker.kill()
    wait_for(lambda: not loop.paused)

    broker.start()
    publish(broker, b'{"power": "off"}')
    wait_for(lambda: loop.paused)
