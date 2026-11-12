"""The loaded railroad's archive: which row names a new build, and what the loop opens for it."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fake_l1 import FakeScorer, FakeSource, running, wait_for, write_archive
from paho.mqtt import client as paho
from paho.mqtt.enums import CallbackAPIVersion
from rails49_testing.broker import Broker, mosquitto, no_broker

from occupancy_service.capture import Frame
from occupancy_service.l1 import L1Loop, L1Result, L1Settings
from occupancy_service.profile import ProfileError, RailroadTopic, load_profile
from occupancy_service.railroad import (
    RailroadFollower,
    RailroadSettings,
    archive_for,
    build_of,
    railroad_source,
)

TOPIC = "tc49/layout/state/railroad"
SOURCE = RailroadTopic(TOPIC, "name", "at")


def row(name: Any, at: Any = "t1", **more: Any) -> bytes:
    return json.dumps({"name": name, "at": at, **more}).encode()


@pytest.mark.parametrize(
    ("payload", "build"),
    [
        (row("Gleis 49"), ("Gleis 49", "t1")),
        (row("Gleis 49", built=True), ("Gleis 49", "t1")),
        (json.dumps({"name": "Gleis 49"}).encode(), ("Gleis 49", None)),
        (row("Gleis 49", built=False), None),
        (row(""), None),
        (row(3), None),
        (b'["Gleis 49"]', None),
        (b"{", None),
        (b"\xff", None),
    ],
)
def test_a_row_names_a_build_unless_it_names_no_railroad_or_one_not_built(
    payload: bytes, build: tuple[str, Any] | None
) -> None:
    assert build_of(payload, "name", "at") == build


def test_the_name_fills_the_placeholder_and_cannot_leave_the_folder() -> None:
    assert archive_for("/archives/{railroad}.r49", "Gleis 49") == "/archives/Gleis 49.r49"
    assert archive_for("/archives/{railroad}.r49", "../etc") is None


def test_the_railroad_comes_from_the_profile_that_names_one(tmp_path: Path) -> None:
    occupancy, control = load_profile("occupancy"), load_profile("control")
    assert railroad_source([occupancy]) is None
    assert railroad_source([occupancy, control]) == SOURCE
    other = tmp_path / "other.toml"
    text = (
        Path(__file__).parent.parent / "src/occupancy_service/profiles/control.toml"
    ).read_text()
    other.write_text(text.replace(TOPIC, "elsewhere/railroad"))
    with pytest.raises(ProfileError):
        railroad_source([control, load_profile(str(other))])


@pytest.fixture
def archives(tmp_path: Path) -> Path:
    folder = tmp_path / "archives"
    folder.mkdir()
    write_archive(folder / "A.r49", {"a1": (160, 120)})
    write_archive(folder / "B.r49", {"b1": (160, 120)}, id="archive-2")
    return folder


@pytest.fixture
def source() -> FakeSource:
    return FakeSource()


@pytest.fixture
def loop(source: FakeSource) -> Iterator[L1Loop]:
    with running(L1Loop(L1Settings(None), source, FakeScorer({"a1": 0.9}))) as loop:
        yield loop


def follower(archives: Path, loop: L1Loop, port: int = 1) -> RailroadFollower:
    pattern = str(archives / "{railroad}.r49")
    return RailroadFollower(RailroadSettings(pattern, SOURCE, "127.0.0.1", port), loop)


def served(loop: L1Loop, frame: Frame) -> L1Result:
    def found() -> L1Result | None:
        result = loop.latest()
        return result if result.frame == frame else None

    return wait_for(found)


def sensors(loop: L1Loop, source: FakeSource) -> set[str]:
    """The sensors of the result of a new frame."""
    return set(served(loop, source.show()).l1)


def test_a_railroad_with_an_archive_opens_it_and_one_without_leaves_none(
    archives: Path, loop: L1Loop, source: FakeSource
) -> None:
    railroad = follower(archives, loop)
    assert railroad.name() is None

    railroad.take(row("A"))
    assert (railroad.name(), loop.archive) == ("A", str(archives / "A.r49"))
    assert sensors(loop, source) == {"a1"}

    railroad.take(row("C"))
    assert (railroad.name(), loop.archive) == ("C", None)
    assert sensors(loop, source) == set()

    railroad.take(row("A", at="t2"))
    assert sensors(loop, source) == {"a1"}


def test_a_new_name_opens_its_archive_and_closes_events_clients(
    archives: Path, loop: L1Loop, source: FakeSource
) -> None:
    railroad = follower(archives, loop)
    railroad.take(row("A"))
    subscription = loop.subscribe()

    railroad.take(row("B"))

    assert loop.archive == str(archives / "B.r49")
    assert sensors(loop, source) == {"b1"}
    assert subscription.outdated


def test_a_new_stamp_on_the_same_name_reads_the_archive_again(
    archives: Path, loop: L1Loop, source: FakeSource
) -> None:
    railroad = follower(archives, loop)
    railroad.take(row("A"))
    write_archive(archives / "A.r49", {"a1": (160, 120), "a2": (200, 120)})

    railroad.take(row("A"))
    assert sensors(loop, source) == {"a1"}

    railroad.take(row("A", at="t2"))
    assert sensors(loop, source) == {"a1", "a2"}


@pytest.mark.parametrize("ignored", [row("B", built=False), row(""), b"?"])
def test_a_row_naming_no_build_changes_nothing(
    archives: Path, loop: L1Loop, ignored: bytes
) -> None:
    railroad = follower(archives, loop)
    railroad.take(row("A"))

    railroad.take(ignored)

    assert (railroad.name(), loop.archive) == ("A", str(archives / "A.r49"))


def test_an_archive_that_cannot_be_read_leaves_the_service_with_none(
    archives: Path, loop: L1Loop, source: FakeSource
) -> None:
    (archives / "C.r49").mkdir()
    (archives / "C.r49" / "manifest.json").write_text("{")
    railroad = follower(archives, loop)
    railroad.take(row("A"))

    railroad.take(row("C"))

    assert (railroad.name(), loop.archive) == ("C", None)
    assert sensors(loop, source) == set()


def test_with_no_placeholder_only_the_name_is_kept(archives: Path, loop: L1Loop) -> None:
    fixed = str(archives / "A.r49")
    settings = RailroadSettings(fixed, SOURCE)
    railroad = RailroadFollower(settings, loop)

    railroad.take(row("B"))

    assert (railroad.name(), loop.archive) == ("B", None)


def test_the_retained_row_on_the_broker_opens_the_archive(
    tmp_path: Path, archives: Path, loop: L1Loop, source: FakeSource
) -> None:
    if mosquitto() is None:
        no_broker("no mosquitto installed")
    broker = Broker(tmp_path)
    broker.start()
    client = paho.Client(CallbackAPIVersion.VERSION2, protocol=paho.MQTTv311)
    client.connect("127.0.0.1", broker.port)
    client.loop_start()
    railroad = follower(archives, loop, broker.port)
    try:
        client.publish(TOPIC, row("A"), qos=1, retain=True).wait_for_publish(5)
        railroad.start()
        wait_for(lambda: loop.archive == str(archives / "A.r49"))
        client.publish(TOPIC, row("B"), qos=1, retain=True).wait_for_publish(5)
        wait_for(lambda: loop.archive == str(archives / "B.r49"))
        assert sensors(loop, source) == {"b1"}
    finally:
        railroad.stop()
        client.disconnect()
        client.loop_stop()
        broker.stop()
