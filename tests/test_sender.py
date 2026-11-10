"""What one pass of the publisher sends, on the test connection: no broker, threads or sleeps."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from fake_connection import FakeConnection, Published
from fake_l1 import detection

from occupancy_service.capture import Frame
from occupancy_service.l0 import L0
from occupancy_service.l1 import L1Entry, L1Result
from occupancy_service.l1_changes import L1Change, L1Changes
from occupancy_service.profile import Profile, load_profile
from occupancy_service.publisher import Sender

CLEAR = L1Entry("clear")
OCCUPIED = L1Entry("occupied", confidence=0.93)
NO_FRAMES = L1Entry("unknown", reason="no frames")
OWN = "occupancy/archive-1/sensor/"
OWN_L0 = "occupancy/archive-1/l0"
CONTROL = "tc49/layout/sensor"
JMRI = "track/sensor/"
ROCRAIL = "rocrail/service/field"


def result(
    l1: Mapping[str, L1Entry], archive_id: str = "archive-1", l0: L0 | None = None
) -> L1Result:
    return L1Result(None, dict(l1), {}, None, {}, archive_id, l0=l0)


def an_l0(*detections: Any) -> L0:
    return L0(Frame(b"", 7, datetime(2026, 11, 5, 10, 15, 29, tzinfo=UTC)), detections)


def a_profile(topic: str = "own/{sensor}", repeat: float = 0.1) -> Profile:
    """The own vocabulary's payloads, on ``topic``, with no L0."""
    shipped = load_profile("occupancy")
    return Profile("fast", topic, shipped.payload, retain=False, qos=0, repeat=repeat)


def connected(*profiles: Profile) -> tuple[Sender, FakeConnection]:
    """A sender of ``profiles`` on a connection that has just connected."""
    connection = FakeConnection("occupancy-service-00000000", "shared")
    connection.connect()
    return Sender(profiles, connection), connection


def shipped(*names: str) -> list[Profile]:
    return [load_profile(name) for name in names]


def sent(message: Published) -> Any:
    return json.loads(message.body)


def bodies(connection: FakeConnection, topic: str) -> list[bytes]:
    return [message.body for message in connection.on(topic)]


def test_every_profile_sends_on_the_one_connection() -> None:
    sensors = {f"s{number:02}": (OCCUPIED, CLEAR, NO_FRAMES)[number % 3] for number in range(6)}
    sender, connection = connected(*shipped("occupancy", "control", "jmri", "rocrail"))
    sender.step([], result(sensors), now=0)
    for id in sensors:
        assert connection.on(OWN + id) and connection.on(JMRI + id)
    assert {sent(message)["addr"] for message in connection.on(CONTROL)} == set(sensors)
    assert len(connection.on(ROCRAIL)) == 6


def test_a_new_set_of_sensors_and_a_new_id_are_reported_and_a_removed_sensor_is_not() -> None:
    sender, connection = connected(a_profile(topic="own/{archive_id}/{sensor}"))
    sender.step([], result({"kept": CLEAR, "removed": CLEAR}), now=0)
    assert set(sender.last_messages()) == {"kept", "removed"}
    reloaded = result({"kept": CLEAR, "added": OCCUPIED}, archive_id="archive-2")
    sender.step([L1Change("added", OCCUPIED)], reloaded, now=0.05)
    assert set(sender.last_messages()) == {"kept", "added"}
    since = len(connection.published)
    sender.step([], reloaded, now=0.2)  # Past a repeat.
    assert {m.topic for m in connection.published[since:]} == {
        "own/archive-2/kept",
        "own/archive-2/added",
    }
    assert {message.topic for message in connection.published} == {
        "own/archive-1/kept",
        "own/archive-1/removed",
        "own/archive-2/kept",
        "own/archive-2/added",
    }


def test_a_clean_stop_sends_unknown_for_every_sensor() -> None:
    sender, connection = connected(*shipped("occupancy", "control"))
    newest = result({"a": OCCUPIED, "b": CLEAR})
    sender.step([], newest, now=0)
    sender.stop(newest)
    stopped = {"state": "unknown", "reason": "service stopped"}
    assert sent(connection.on(OWN + "a")[-1]) == stopped
    assert sent(connection.on(OWN + "b")[-1]) == stopped
    last = {sent(m)["addr"]: sent(m) for m in connection.on(CONTROL)}
    assert last["a"] == {"addr": "a", "occupancy": "unknown", "reason": "service stopped"}
    assert last["b"] == {"addr": "b", "occupancy": "unknown", "reason": "service stopped"}


def test_jmri_sends_active_for_every_sensor_on_a_clean_stop() -> None:
    sender, connection = connected(*shipped("jmri"))
    newest = result({"a": CLEAR, "b": CLEAR})
    sender.step([], newest, now=0)
    sender.stop(newest)
    for id in "ab":
        assert bodies(connection, JMRI + id) == [b"INACTIVE", b"ACTIVE"]
        assert connection.on(JMRI + id)[-1].retain


def test_the_own_vocabulary_profile_sends_each_l0_once_not_retained() -> None:
    sender, connection = connected(*shipped("occupancy"))
    first_l0 = an_l0(detection(160, 120, confidence=0.7))
    sender.step([], result({"a": CLEAR}, l0=first_l0), now=0)
    sender.step([], result({"a": CLEAR}, l0=first_l0), now=0.1)
    assert len(connection.on(OWN_L0)) == 1
    sender.step([], result({"a": CLEAR}, l0=an_l0()), now=0.2)

    first, second = connection.on(OWN_L0)
    assert sent(first) == {
        "captured_at": "2026-11-05T10:15:29+00:00",
        "detections": [
            {
                "centre": {"x": 160, "y": 120},
                "length": 50,
                "width": 30,
                "angle": 0,
                "class": "passenger",
                "confidence": 0.7,
            }
        ],
    }
    assert (first.qos, first.retain, second.retain) == (0, False, False)
    assert sent(second)["detections"] == []


def test_the_control_profile_sends_no_l0() -> None:
    sender, connection = connected(*shipped("control"))
    sender.step([], result({"a": CLEAR}, l0=an_l0(detection(160, 120))), now=0)
    assert {message.topic for message in connection.published} == {CONTROL}


def test_an_l0_that_arrives_while_the_connection_is_down_is_not_sent_after_it_comes_back() -> None:
    connection = FakeConnection("occupancy-service-00000000", "shared")
    sender = Sender(shipped("occupancy"), connection)
    newest = result({"a": CLEAR}, l0=an_l0(detection(160, 120)))
    sender.step([], newest, now=0)
    connection.connect()
    sender.step([], newest, now=0.1)
    assert connection.on(OWN + "a")  # What the new connection carries.
    assert connection.on(OWN_L0) == []


def test_a_new_confidence_alone_sends_nothing() -> None:
    """With no repeat, only an L1 change sends, and ``L1Changes`` says a new confidence is not
    one."""
    changes = L1Changes()
    changes.update({"a": OCCUPIED}, manifest=None)
    subscription = changes.subscribe()
    sender, connection = connected(a_profile(repeat=0))
    sender.step([], result({"a": OCCUPIED}), now=0)
    newer = {"a": L1Entry("occupied", confidence=0.8)}
    changes.update(newer, manifest=None)
    sender.step(list(iter(lambda: subscription.get(0), None)), result(newer), now=1)
    assert [sent(message) for message in connection.on("own/a")] == [
        {"state": "occupied", "confidence": 0.93}
    ]


def test_with_no_connection_nothing_is_sent() -> None:
    sender = Sender(shipped("occupancy"), None)
    newest = result({"a": CLEAR}, l0=an_l0())
    sender.step([L1Change("a", CLEAR)], newest, now=0)
    sender.stop(newest)
    assert sender.last_messages() == {}


def test_next_due_is_the_earliest_repeat_of_its_profiles() -> None:
    assert Sender([], None).next_due() == float("inf")
    sender, _ = connected(*shipped("occupancy", "control", "rocrail"))
    sender.step([], result({"a": CLEAR}), now=10)
    assert sender.next_due() == 10.9  # control's repeat, which is sooner than occupancy's 1 s.


def test_last_messages_are_by_sensor_then_profile() -> None:
    sender, _ = connected(*shipped("occupancy", "jmri"))
    sender.step([], result({"a": CLEAR}), now=0)
    messages = sender.last_messages()["a"]
    assert messages["occupancy"].message == {"state": "clear"}
    assert messages["jmri"].message == "INACTIVE"
