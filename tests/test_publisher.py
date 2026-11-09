"""The publisher on the wire, against a real ``mosquitto``, on a fake result source and on the
real L1 loop; and what it decides, on the test connection."""

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fake_connection import FakeConnect, FakeConnection, Published
from fake_l1 import (
    FakeChecks,
    FakeDetector,
    FakeScorer,
    FakeSource,
    detection,
    picture,
    running,
    wait_for,
    write_archive,
)
from mqtt_broker import Broker, Received, Subscriber, mosquitto

from occupancy_service.capture import Frame
from occupancy_service.l0 import L0
from occupancy_service.l1 import L1Entry, L1Loop, L1Result, L1Settings
from occupancy_service.l1_changes import L1Changes, L1Subscription
from occupancy_service.live_check import check
from occupancy_service.profile import ProfileError
from occupancy_service.publisher import Publisher, PublisherSettings, paho_connect
from occupancy_service.server import LiveServer, ServerSettings

CLEAR = L1Entry("clear")
OCCUPIED = L1Entry("occupied", confidence=0.93)
NO_FRAMES = L1Entry("unknown", reason="no frames")
OWN = "occupancy/archive-1/sensor/"
CONTROL = "tc49/layout/sensor"
OWN_L0 = "occupancy/archive-1/l0"


class FakeResults:
    """A result source a test drives by hand. Its L1 changes are ``L1Loop``'s, from the same
    ``L1Changes``, with a new manifest at each new archive ``id``."""

    def __init__(self, l1: Mapping[str, L1Entry], archive_id: str | None = "archive-1") -> None:
        self.l1 = dict(l1)
        self.archive_id = archive_id
        self.frame: Frame | None = None
        self.l0: L0 | None = None
        self._lock = threading.Lock()  # Taken before the L1Changes lock, as L1Loop does.
        self._changes = L1Changes()
        self._manifest = object()
        self._changes.update(self.l1, self._manifest)

    def latest(self) -> L1Result:
        with self._lock:
            return L1Result(self.frame, dict(self.l1), {}, None, {}, self.archive_id, l0=self.l0)

    def subscribe(self) -> L1Subscription:
        with self._lock:
            return self._changes.subscribe()

    def show(self, l1: Mapping[str, L1Entry], archive_id: str | None = "archive-1") -> None:
        """Change the result, telling subscribers what ``L1Loop`` would."""
        with self._lock:
            if archive_id != self.archive_id:
                self._manifest = object()
            self.l1, self.archive_id = dict(l1), archive_id
            self._changes.update(self.l1, self._manifest)


@pytest.fixture
def broker(tmp_path: Path) -> Iterator[Broker]:
    binary = mosquitto()
    if binary is None:
        pytest.skip("mosquitto is not installed")
    broker = Broker(binary, tmp_path)
    broker.start()
    yield broker
    broker.stop()


def test_a_broker_that_cannot_start_says_why_at_once(tmp_path: Path) -> None:
    binary = tmp_path / "mosquitto"
    binary.write_text("#!/bin/sh\necho the reason >&2\nexit 3\n")
    binary.chmod(0o755)
    broker = Broker(str(binary), tmp_path)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="exit code 3:\\nthe reason"):
        broker.start()
    broker.stop()
    assert time.monotonic() - started < 5


@contextmanager
def subscribed(broker: Broker, qos: int = 0) -> Iterator[Subscriber]:
    subscriber = Subscriber(broker.port, qos)
    try:
        yield subscriber
    finally:
        subscriber.close()


def settings_for(broker: Broker, *profiles: str, **settings: Any) -> PublisherSettings:
    return PublisherSettings(
        host="127.0.0.1", port=broker.port, profiles=profiles or ("occupancy",), **settings
    )


@contextmanager
def publishing(source: Any, settings: PublisherSettings) -> Iterator[Publisher]:
    publisher = Publisher(settings, source)
    publisher.start()
    try:
        yield publisher
    finally:
        publisher.stop()


def profile_file(
    folder: Path,
    name: str = "fast",
    topic: str = "own/{sensor}",
    repeat: float = 0.1,
    retain: bool = False,
    qos: int = 0,
    occupied: str = '{ state = "occupied", confidence = "{confidence}" }',
    clear: str = '{ state = "clear" }',
    unknown: str = '{ state = "unknown", reason = "{reason}" }',
    clear_hold_s: float = 0,
    connection: str = "shared",
) -> str:
    """A profile that differs from the own-vocabulary one where a test needs it to."""
    path = folder / f"{name}.toml"
    path.write_text(f"""
topic = "{topic}"
retain = {str(retain).lower()}
qos = {qos}
repeat = {repeat}
clear_hold_s = {clear_hold_s}
connection = "{connection}"
[payload]
occupied = {occupied}
clear = {clear}
unknown = {unknown}
""")
    return str(path)


def payload(message: Received) -> Any:
    return json.loads(message.payload)


def states(subscriber: Subscriber, topic: str = "own/a") -> list[str]:
    return [payload(message)["state"] for message in subscriber.on(topic)]


def test_the_default_is_the_own_vocabulary_profile_only() -> None:
    assert PublisherSettings().profiles == ("occupancy",)


def test_a_bad_profile_stops_the_publisher_being_made(tmp_path: Path) -> None:
    with pytest.raises(ProfileError):
        Publisher(PublisherSettings(profiles=("nothing-here",)), FakeResults({}))


def test_two_profiles_may_not_share_a_name(tmp_path: Path) -> None:
    path = profile_file(tmp_path, name="control")
    with pytest.raises(ProfileError, match="control"):
        Publisher(PublisherSettings(profiles=("control", path)), FakeResults({}))


def test_no_active_profile_sends_nothing_and_stops_cleanly(broker: Broker) -> None:
    settings = PublisherSettings(host="127.0.0.1", port=broker.port, profiles=())
    with subscribed(broker) as subscriber, publishing(FakeResults({"a": CLEAR}), settings):
        time.sleep(0.2)
    assert subscriber.messages == []


def test_own_vocabulary_topics_and_payloads(broker: Broker) -> None:
    source = FakeResults({"a": OCCUPIED, "b": CLEAR, "c": NO_FRAMES})
    with subscribed(broker) as subscriber, publishing(source, settings_for(broker)):
        wait_for(lambda: all(subscriber.on(OWN + id) for id in "abc"))
    assert payload(subscriber.on(OWN + "a")[0]) == {"state": "occupied", "confidence": 0.93}
    assert payload(subscriber.on(OWN + "b")[0]) == {"state": "clear"}
    assert payload(subscriber.on(OWN + "c")[0]) == {"state": "unknown", "reason": "no frames"}


def test_the_control_profile_has_exactly_its_keys_and_is_not_retained(broker: Broker) -> None:
    source = FakeResults({"a": OCCUPIED, "b": CLEAR, "c": NO_FRAMES})
    with subscribed(broker) as subscriber, publishing(source, settings_for(broker, "control")):
        # The profile's hold: b's clear comes about 1 s after the first message.
        subscriber.wait_for(lambda m: payload(m) == {"addr": "b", "occupancy": "clear"})
        by_addr = {payload(message)["addr"]: payload(message) for message in subscriber.messages}
        assert by_addr == {
            "a": {"addr": "a", "occupancy": "occupied"},
            "b": {"addr": "b", "occupancy": "clear"},
            "c": {"addr": "c", "occupancy": "unknown", "reason": "no frames"},
        }
        # A broker marks a message retained only for one who subscribes after it.
        with subscribed(broker) as late:
            time.sleep(0.3)
            assert [message for message in late.messages if message.retain] == []


def test_a_retained_profile_is_retained(broker: Broker, tmp_path: Path) -> None:
    """Shows the test above can tell: a late subscriber does see a retained message."""
    path = profile_file(tmp_path, retain=True, repeat=60)
    source = FakeResults({"a": CLEAR})
    with subscribed(broker) as first, publishing(source, settings_for(broker, path)):
        wait_for(lambda: first.on("own/a"))
        with subscribed(broker) as late:
            assert late.wait_for(lambda message: message.retain).topic == "own/a"
        source.show({"a": OCCUPIED})  # The late subscriber has not displaced the first.
        first.wait_for(lambda message: payload(message)["state"] == "occupied")


@pytest.mark.parametrize(("published", "delivered"), [(0, 0), (1, 1), (2, 1)])
def test_a_profile_sets_the_qos(
    broker: Broker, tmp_path: Path, published: int, delivered: int
) -> None:
    """Delivered at the lower of the publisher's QoS and the subscriber's, here 1. The broker
    holds a QoS 2 message back until the publisher finishes its exchange."""
    path = profile_file(tmp_path, qos=published, repeat=0.05)
    source = FakeResults({"a": CLEAR})
    with subscribed(broker, qos=1) as subscriber, publishing(source, settings_for(broker, path)):
        wait_for(lambda: len(subscriber.on("own/a")) >= 3)
    assert subscriber.on("own/a")[0].qos == delivered


def test_a_string_payload_is_text_and_a_table_is_json(broker: Broker, tmp_path: Path) -> None:
    path = profile_file(tmp_path, clear='"OFF {sensor}"')
    source = FakeResults({"a": CLEAR, "b": OCCUPIED})
    with subscribed(broker) as subscriber, publishing(source, settings_for(broker, path)):
        wait_for(lambda: subscriber.on("own/a") and subscriber.on("own/b"))
    assert subscriber.on("own/a")[0].payload == b"OFF a"
    assert subscriber.on("own/b")[0].payload == b'{"state": "occupied", "confidence": 0.93}'


def test_every_sensor_goes_out_at_least_once_a_second_with_nothing_changing(
    broker: Broker,
) -> None:
    source = FakeResults({"a": CLEAR, "b": OCCUPIED})
    settings = settings_for(broker, "occupancy", "control")
    with subscribed(broker) as subscriber, publishing(source, settings):
        time.sleep(3.2)
    for messages in (
        subscriber.on(OWN + "a"),
        subscriber.on(OWN + "b"),
        [m for m in subscriber.on(CONTROL) if payload(m)["addr"] == "a"],
        [m for m in subscriber.on(CONTROL) if payload(m)["addr"] == "b"],
    ):
        assert len(messages) >= 3
        # Some room for a busy machine: the exact interval is what the profile says.
        assert max(b.at - a.at for a, b in zip(messages, messages[1:], strict=False)) < 1.25


def test_a_change_goes_out_at_once_and_only_for_that_sensor(broker: Broker, tmp_path: Path) -> None:
    path = profile_file(tmp_path, repeat=60)
    source = FakeResults({"a": CLEAR, "b": CLEAR})
    with subscribed(broker) as subscriber, publishing(source, settings_for(broker, path)):
        wait_for(lambda: subscriber.on("own/a") and subscriber.on("own/b"))
        changed_at = time.monotonic()
        source.show({"a": OCCUPIED, "b": CLEAR})
        message = subscriber.wait_for(lambda m: payload(m)["state"] == "occupied")
        assert message.topic == "own/a"
        assert message.at - changed_at < 0.5
        time.sleep(0.1)
        assert len(subscriber.on("own/b")) == 1


def test_every_sensor_is_unknown_at_startup_on_the_real_loop(
    broker: Broker, tmp_path: Path
) -> None:
    archive = write_archive(tmp_path / "archive", {"s1": (160.0, 120.0), "s2": (170.0, 120.0)})
    loop = L1Loop(L1Settings(archive), FakeSource(), FakeScorer({}))
    with subscribed(broker) as subscriber, publishing(loop, settings_for(broker)):
        wait_for(lambda: subscriber.on(OWN + "s1") and subscriber.on(OWN + "s2"))
    for id in ("s1", "s2"):
        assert payload(subscriber.on(OWN + id)[0]) == {"state": "unknown", "reason": "no frames"}


def test_scoring_stalled_goes_out_when_results_stop_and_the_publisher_keeps_running(
    broker: Broker, tmp_path: Path
) -> None:
    archive = write_archive(tmp_path / "archive", {"s1": (160.0, 120.0)})
    source = FakeSource()
    loop = L1Loop(L1Settings(archive), source, FakeScorer({"s1": 0.9}))
    path = profile_file(tmp_path, topic="own/{sensor}", repeat=0.1)
    with (
        running(loop),
        subscribed(broker) as subscriber,
        publishing(loop, settings_for(broker, path)),
    ):
        source.show()
        subscriber.wait_for(lambda m: payload(m)["state"] == "occupied")
        # No new frame from here, with the camera still fine.
        stalled = subscriber.wait_for(lambda m: payload(m).get("reason") == "scoring stalled")
        last_occupied = [m for m in subscriber.on("own/s1") if payload(m)["state"] == "occupied"]
        assert stalled.at - last_occupied[-1].at < 2
        count = len(subscriber.messages)
        wait_for(lambda: len(subscriber.messages) >= count + 3)  # It keeps on repeating.
        assert payload(subscriber.messages[-1])["reason"] == "scoring stalled"
        source.show()
        subscriber.wait_for(lambda m: m.at > stalled.at and payload(m)["state"] == "occupied")


def test_every_profile_carries_a_drift_refusal_as_any_unknown(
    broker: Broker, tmp_path: Path
) -> None:
    archive = write_archive(
        tmp_path / "archive", {"s1": (160.0, 120.0)}, images=[{"filename": "first.png"}]
    )
    (archive / "first.png").write_bytes(picture())
    source = FakeSource()
    checks = FakeChecks()
    settings = L1Settings(archive, drift_every=0.01)
    loop = L1Loop(settings, source, FakeScorer({"s1": 0.9}), make_drift_check=checks)
    with (
        running(loop),
        subscribed(broker) as subscriber,
        publishing(loop, settings_for(broker, "occupancy", "control")),
    ):
        source.show()
        subscriber.wait_for(lambda m: m.topic == f"{OWN}s1" and payload(m)["state"] == "occupied")
        checks.px = 100.0
        own = subscriber.wait_for(
            lambda m: m.topic == f"{OWN}s1" and payload(m).get("reason") == "drift"
        )
        control = subscriber.wait_for(
            lambda m: m.topic == CONTROL and payload(m).get("reason") == "drift"
        )
    assert payload(own) == {"state": "unknown", "reason": "drift"}
    assert payload(control) == {"addr": "s1", "occupancy": "unknown", "reason": "drift"}


def test_a_restarted_broker_gets_every_sensor_with_no_restart_here(
    broker: Broker, tmp_path: Path
) -> None:
    """Retained, with a repeat too long to help: a late subscriber sees what the new connection
    sent, whichever of the two got to the broker first."""
    path = profile_file(tmp_path, retain=True, repeat=60)
    source = FakeResults({"a": CLEAR, "b": CLEAR})
    settings = settings_for(broker, path)
    with publishing(source, settings):
        with subscribed(broker) as before:
            wait_for(lambda: before.on("own/a") and before.on("own/b"))
        broker.stop()
        source.show({"a": OCCUPIED, "b": CLEAR})  # Lost with the broker, and not lost after it.
        time.sleep(0.2)
        broker.start()
        with subscribed(broker) as after:
            wait_for(lambda: after.on("own/a") and after.on("own/b"))
            assert payload(after.on("own/a")[-1])["state"] == "occupied"


def test_it_connects_to_a_broker_that_starts_later(broker: Broker) -> None:
    broker.stop()
    source = FakeResults({"a": CLEAR})
    with publishing(source, settings_for(broker)):
        time.sleep(0.2)
        broker.start()
        with subscribed(broker) as subscriber:
            wait_for(lambda: subscriber.on(OWN + "a"))


def test_a_stop_with_no_broker_returns(broker: Broker) -> None:
    broker.stop()
    publisher = Publisher(settings_for(broker), FakeResults({"a": CLEAR}))
    publisher.start()
    time.sleep(0.1)
    started = time.monotonic()
    publisher.stop()
    assert time.monotonic() - started < 2


def test_with_no_archive_id_only_a_topic_that_needs_none_reports(broker: Broker) -> None:
    source = FakeResults({"a": L1Entry("unknown", reason="archive has no id")}, archive_id=None)
    settings = settings_for(broker, "occupancy", "control")
    with subscribed(broker) as subscriber, publishing(source, settings):
        wait_for(lambda: len(subscriber.on(CONTROL)) >= 2)
    assert {message.topic for message in subscriber.messages} == {CONTROL}
    assert payload(subscriber.on(CONTROL)[0]) == {
        "addr": "a",
        "occupancy": "unknown",
        "reason": "archive has no id",
    }


@pytest.mark.parametrize(
    "bad",
    [
        "bad+id",
        "bad#id",
        "bad/id",
        "bad\tid",
        "bad\x7fid",
        "bad\x9fid",
        "bad\ufdd0id",
        "bad\uffffid",
        "bad\U0001ffffid",
    ],
)
def test_a_sensor_id_a_topic_cannot_carry_is_logged_once_and_skipped_there(
    broker: Broker, tmp_path: Path, caplog: pytest.LogCaptureFixture, bad: str
) -> None:
    fast = profile_file(tmp_path)
    fast_list = profile_file(tmp_path, name="named", topic="list", clear='{ addr = "{sensor}" }')
    source = FakeResults({bad: CLEAR, "good": CLEAR})
    with (
        caplog.at_level(logging.WARNING, "occupancy_service"),
        subscribed(broker) as subscriber,
        publishing(source, settings_for(broker, fast, fast_list)),
    ):
        wait_for(lambda: len(subscriber.on("own/good")) >= 3)
        wait_for(lambda: len(subscriber.on("list")) >= 6)
        addrs = {payload(message)["addr"] for message in subscriber.on("list")}
        assert addrs == {bad, "good"}
        topics = {message.topic for message in subscriber.messages}
        assert topics == {"own/good", "list"}
    # Still connected throughout: a broker drops a client that publishes to a wildcard.
    refusals = [record for record in caplog.records if repr(bad) in record.getMessage()]
    assert len(refusals) == 1
    assert "fast" in refusals[0].getMessage()
    assert not [record for record in caplog.records if "lost" in record.getMessage()]


def test_keepalive_holds_a_connection_with_nothing_to_send(
    broker: Broker, caplog: pytest.LogCaptureFixture
) -> None:
    source = FakeResults({})
    settings = settings_for(broker, keepalive=1)
    with (
        caplog.at_level(logging.WARNING, "occupancy_service.publisher"),
        subscribed(broker) as subscriber,
        publishing(source, settings),
    ):
        time.sleep(2.5)  # A broker gives up on a client after 1.5 keepalives of silence.
        source.show({"a": CLEAR})
        wait_for(lambda: subscriber.on(OWN + "a"))
    assert not [record for record in caplog.records if "lost" in record.getMessage()]


def test_last_messages_hold_the_newest_per_sensor_and_profile(
    broker: Broker, tmp_path: Path
) -> None:
    text = profile_file(tmp_path, name="text", topic="text/{sensor}", clear='"OFF"')
    source = FakeResults({"a": CLEAR})
    with (
        subscribed(broker) as subscriber,
        publishing(source, settings_for(broker, "occupancy", text)) as publisher,
    ):
        assert wait_for(lambda: subscriber.on("text/a"))
        wait_for(lambda: len(publisher.last_messages().get("a", {})) == 2)
        messages = publisher.last_messages()["a"]
        assert messages["occupancy"].message == {"state": "clear"}
        assert messages["text"].message == "OFF"
        before = messages["occupancy"].time
        assert before.utcoffset() is not None
        source.show({"a": OCCUPIED})
        wait_for(
            lambda: publisher.last_messages()["a"]["occupancy"].message
            == {"state": "occupied", "confidence": 0.93}
        )
        assert publisher.last_messages()["a"]["occupancy"].time > before


def test_the_real_service_reports_what_it_sent_in_latest(broker: Broker, tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "archive", {"s1": (160.0, 120.0)})
    source = FakeSource()
    loop = L1Loop(L1Settings(archive), source, FakeScorer({"s1": 0.9}))
    with (
        running(loop),
        subscribed(broker) as subscriber,
        publishing(loop, settings_for(broker)) as publisher,
    ):
        server = LiveServer(
            ServerSettings(port=0), archive, loop, last_messages=publisher.last_messages
        )
        server.start()
        try:
            source.show()
            subscriber.wait_for(lambda m: payload(m)["state"] == "occupied")
            base = f"http://127.0.0.1:{server.port}/live"
            assert check(base, timeout=5) == []
        finally:
            server.stop()


def test_a_clear_that_stands_for_the_hold_is_published_once_at_the_end_of_it(
    broker: Broker, tmp_path: Path
) -> None:
    path = profile_file(tmp_path, repeat=60, clear_hold_s=0.4)
    source = FakeResults({"a": OCCUPIED})
    with subscribed(broker) as subscriber, publishing(source, settings_for(broker, path)):
        wait_for(lambda: subscriber.on("own/a"))
        cleared_at = time.monotonic()
        source.show({"a": CLEAR})
        message = subscriber.wait_for(lambda m: payload(m)["state"] == "clear")
        assert 0.4 <= message.at - cleared_at < 1.5
        time.sleep(0.3)
        assert states(subscriber).count("clear") == 1


def test_a_reconnect_during_a_hold_reports_the_last_published_state(
    broker: Broker, tmp_path: Path
) -> None:
    """Retained, with a repeat too long to help: a late subscriber sees what the new connection
    sent, whichever of the two got to the broker first."""
    path = profile_file(tmp_path, retain=True, repeat=60, clear_hold_s=60)
    source = FakeResults({"a": OCCUPIED})
    with publishing(source, settings_for(broker, path)):
        with subscribed(broker) as before:
            wait_for(lambda: before.on("own/a"))
        source.show({"a": CLEAR})
        time.sleep(0.1)
        broker.stop()
        broker.start()
        with subscribed(broker) as after:
            wait_for(lambda: after.on("own/a"))
            assert states(after) == ["occupied"]


def test_a_hold_leaves_l1_and_its_change_count_alone(broker: Broker, tmp_path: Path) -> None:
    """On the real loop, with frames going on through the hold."""
    archive = write_archive(tmp_path / "archive", {"s1": (160.0, 120.0)})
    source = FakeSource()
    scores = {"s1": 0.9}
    loop = L1Loop(L1Settings(archive), source, FakeScorer(scores))
    path = profile_file(tmp_path, repeat=60, clear_hold_s=0.4)

    def next_frame_until(done: Callable[[], Any]) -> None:
        def seen() -> Any:
            source.show()
            time.sleep(0.02)
            return done()

        wait_for(seen)

    with (
        running(loop),
        subscribed(broker) as subscriber,
        publishing(loop, settings_for(broker, path)),
    ):
        next_frame_until(lambda: "occupied" in states(subscriber, "own/s1"))
        scores["s1"] = 0.1
        next_frame_until(lambda: loop.latest().l1["s1"] == CLEAR)
        cleared_at = time.monotonic()
        assert loop.stats().changes["s1"] == 2  # From unknown to occupied, then to clear.
        assert "clear" not in states(subscriber, "own/s1")
        next_frame_until(lambda: "clear" in states(subscriber, "own/s1"))
        assert time.monotonic() - cleared_at >= 0.3
        assert loop.stats().changes["s1"] == 2


def test_no_python_file_under_src_names_the_controller() -> None:
    src = Path(__file__).parent.parent / "src"
    named = [path for path in src.rglob("*.py") if "tc49" in path.read_text()]
    assert named == []
    assert len(list(src.rglob("*.py"))) > 3


def test_the_keepalive_is_5_s_by_default() -> None:
    assert PublisherSettings().keepalive == 5


JMRI = "track/sensor/"
STATES = (OCCUPIED, CLEAR, NO_FRAMES)


def jmri_sensors(count: int) -> dict[str, L1Entry]:
    """``count`` sensors, going round occupied, clear and unknown."""
    return {f"s{number:02}": STATES[number % 3] for number in range(count)}


def jmri_payload(entry: L1Entry) -> bytes:
    return b"INACTIVE" if entry.state == "clear" else b"ACTIVE"


def connected_clients(broker: Broker) -> list[str]:
    """The id of every connection this service made, in the broker's log."""
    return re.findall(r"New client connected from \S+ as (occupancy-service-\S+) ", read(broker))


def disconnected_clients(broker: Broker) -> list[str]:
    """Those that sent DISCONNECT: a broker logs that it "closed" any other."""
    return re.findall(r"Client (occupancy-service-\S+) disconnected\.", read(broker))


def read(broker: Broker) -> str:
    return broker.log.read_text(errors="replace")


def retained(broker: Broker, topics: list[str]) -> dict[str, Received]:
    """The retained message on each of ``topics``, as a new subscriber gets it."""
    with subscribed(broker) as late:
        wait_for(lambda: all(late.on(topic) for topic in topics))
        return {topic: late.on(topic)[0] for topic in topics}


def test_jmri_sends_each_state_retained_on_50_connections(broker: Broker) -> None:
    sensors = jmri_sensors(50)
    source = FakeResults(sensors)
    topics = [JMRI + id for id in sensors]
    with subscribed(broker) as subscriber, publishing(source, settings_for(broker, "jmri")):
        wait_for(lambda: all(subscriber.on(topic) for topic in topics), 10)
        for id, entry in sensors.items():
            assert {m.payload for m in subscriber.on(JMRI + id)} == {jmri_payload(entry)}
        # A new subscriber gets every sensor's state, as the broker kept it.
        late = retained(broker, topics)
        for id, entry in sensors.items():
            assert (late[JMRI + id].payload, late[JMRI + id].retain) == (jmri_payload(entry), True)
        clients = connected_clients(broker)
        assert sorted(clients) == sorted(f"occupancy-service-jmri-archive-1-{id}" for id in sensors)


def test_jmri_corrects_a_wrong_retained_value_within_about_1_s(broker: Broker) -> None:
    source = FakeResults({"a": OCCUPIED})
    with subscribed(broker) as subscriber, publishing(source, settings_for(broker, "jmri")):
        subscriber.wait_for(lambda m: m.topic == JMRI + "a")
        clicked = subscriber.client.publish(JMRI + "a", b"INACTIVE", retain=True)
        clicked.wait_for_publish(5)
        wrong = subscriber.wait_for(lambda m: m.payload == b"INACTIVE")
        right = subscriber.wait_for(lambda m: m.at > wrong.at and m.payload == b"ACTIVE")
        assert right.at - wrong.at < 1.25
        assert retained(broker, [JMRI + "a"])[JMRI + "a"].payload == b"ACTIVE"


def test_jmri_gives_a_restarted_broker_every_sensor_again(broker: Broker) -> None:
    sensors = jmri_sensors(6)
    topics = [JMRI + id for id in sensors]
    with publishing(FakeResults(sensors), settings_for(broker, "jmri")):
        retained(broker, topics)
        broker.stop()
        broker.start()  # It keeps nothing across the restart.
        after = retained(broker, topics)
    for id, entry in sensors.items():
        assert after[JMRI + id].payload == jmri_payload(entry)


def test_a_stop_with_no_broker_returns_with_a_connection_per_sensor(broker: Broker) -> None:
    broker.stop()
    publisher = Publisher(settings_for(broker, "jmri"), FakeResults(jmri_sensors(20)))
    publisher.start()
    time.sleep(0.3)
    started = time.monotonic()
    publisher.stop()
    assert time.monotonic() - started < 3


def test_no_python_file_under_src_names_jmri() -> None:
    src = Path(__file__).parent.parent / "src"
    named = [path for path in src.rglob("*.py") if "jmri" in path.read_text().lower()]
    assert named == []
    assert len(list(src.rglob("*.py"))) > 3


def on_off(broker: Broker, folder: Path, repeat: float) -> PublisherSettings:
    """A profile whose ``occupied`` and ``unknown`` are the same payload."""
    path = profile_file(
        folder, topic="t/{sensor}", repeat=repeat, occupied='"ON"', clear='"OFF"', unknown='"ON"'
    )
    return settings_for(broker, path)


def payloads(subscriber: Subscriber, topic: str = "t/a") -> list[bytes]:
    return [message.payload for message in subscriber.on(topic)]


def test_no_repeat_sends_a_change_only_where_its_payload_differs(
    broker: Broker, tmp_path: Path
) -> None:
    source = FakeResults({"a": OCCUPIED})
    with subscribed(broker) as subscriber, publishing(source, on_off(broker, tmp_path, 0)):
        subscriber.wait_for(lambda m: m.topic == "t/a")
        source.show({"a": NO_FRAMES})
        source.show({"a": L1Entry("unknown", reason="scoring stalled")})
        time.sleep(0.5)
        assert payloads(subscriber) == [b"ON"]
        changed_at = time.monotonic()
        source.show({"a": CLEAR})
        cleared = subscriber.wait_for(lambda m: m.payload == b"OFF")
        assert cleared.at - changed_at < 0.25
        source.show({"a": OCCUPIED})
        subscriber.wait_for(lambda m: m.at > cleared.at and m.payload == b"ON")


def test_no_repeat_sends_every_sensor_again_on_each_connect(broker: Broker, tmp_path: Path) -> None:
    source = FakeResults({"a": OCCUPIED, "b": CLEAR})
    with publishing(source, on_off(broker, tmp_path, 0)):
        with subscribed(broker) as subscriber:
            wait_for(lambda: subscriber.on("t/a") and subscriber.on("t/b"))
        broker.stop()
        broker.start()  # The publisher tries again after 1 s, and this subscriber is up by then.
        with subscribed(broker) as subscriber:
            wait_for(lambda: subscriber.on("t/a") and subscriber.on("t/b"))
            assert (payloads(subscriber, "t/a"), payloads(subscriber, "t/b")) == ([b"ON"], [b"OFF"])


ROCRAIL = "rocrail/service/field"


def fb(sensor: str, state: bool) -> bytes:
    return f'<fb id="{sensor}" state="{str(state).lower()}"/>'.encode()


def on_rocrail(subscriber: Subscriber, sensor: str) -> list[Received]:
    """What went out for ``sensor`` on Rocrail's one topic."""
    return [m for m in subscriber.on(ROCRAIL) if f'id="{sensor}"'.encode() in m.payload]


def test_rocrail_sends_each_state_once_at_qos_1_and_not_retained(broker: Broker) -> None:
    source = FakeResults({"occupied": OCCUPIED, "clear": CLEAR, "unknown": NO_FRAMES})
    with (
        subscribed(broker, qos=1) as subscriber,
        publishing(source, settings_for(broker, "rocrail")),
    ):
        wait_for(lambda: len(subscriber.on(ROCRAIL)) == 3)
        time.sleep(1.5)  # Past what a repeat of 1 s would send.
        assert sorted((m.payload, m.qos, m.retain) for m in subscriber.messages) == [
            (fb("clear", False), 1, False),
            (fb("occupied", True), 1, False),
            (fb("unknown", True), 1, False),
        ]
        with subscribed(broker) as late:
            time.sleep(0.5)
            assert late.messages == []


def test_rocrail_sends_a_change_only_where_true_or_false_changes(broker: Broker) -> None:
    source = FakeResults({"a": OCCUPIED})
    with (
        subscribed(broker, qos=1) as subscriber,
        publishing(source, settings_for(broker, "rocrail")),
    ):
        subscriber.wait_for(lambda m: m.payload == fb("a", True))
        source.show({"a": NO_FRAMES})  # occupied to unknown
        source.show({"a": L1Entry("unknown", reason="scoring stalled")})  # A new reason.
        time.sleep(0.5)
        assert [m.payload for m in on_rocrail(subscriber, "a")] == [fb("a", True)]
        changed_at = time.monotonic()
        source.show({"a": CLEAR})  # unknown to clear
        cleared = subscriber.wait_for(lambda m: m.payload == fb("a", False))
        assert cleared.at - changed_at < 0.25


def test_rocrail_gives_a_restarted_broker_every_sensor_again(broker: Broker) -> None:
    sensors = {"a": OCCUPIED, "b": CLEAR, "c": NO_FRAMES}
    with publishing(FakeResults(sensors), settings_for(broker, "rocrail")):
        with subscribed(broker, qos=1) as subscriber:
            wait_for(lambda: len(subscriber.on(ROCRAIL)) == 3)
        broker.stop()
        broker.start()  # The publisher tries again after 1 s, and this subscriber is up by then.
        with subscribed(broker, qos=1) as subscriber:
            wait_for(lambda: len(subscriber.on(ROCRAIL)) == 3)
            assert sorted(m.payload for m in subscriber.on(ROCRAIL)) == sorted(
                [fb("a", True), fb("b", False), fb("c", True)]
            )


def test_a_message_a_stalled_broker_never_acknowledged_does_not_reach_the_next_one(
    broker: Broker, tmp_path: Path
) -> None:
    """At QoS 1 with no repeat, as on Rocrail: only the state at the reconnect goes out."""
    path = profile_file(
        tmp_path,
        topic="t/{sensor}",
        repeat=0,
        qos=1,
        occupied='"ON"',
        clear='"OFF"',
        unknown='"ON"',
    )
    source = FakeResults({"a": CLEAR})
    with publishing(source, settings_for(broker, path)):
        with subscribed(broker, qos=1) as before:
            before.wait_for(lambda m: m.payload == b"OFF")
        broker.stall()
        source.show({"a": OCCUPIED})
        time.sleep(0.3)  # Sent, on a connection that still looks up, and never acknowledged.
        broker.kill()
        source.show({"a": CLEAR})
        broker.start()  # The publisher tries again after 1 s, and this subscriber is up by then.
        with subscribed(broker, qos=1) as after:
            after.wait_for(lambda m: m.topic == "t/a")
            time.sleep(0.5)
            assert payloads(after) == [b"OFF"]


class FailingResults(FakeResults):
    """A result source whose ``latest()`` raises once ``failing`` is set."""

    failing = False

    def latest(self) -> L1Result:
        if self.failing:
            raise RuntimeError("the source broke")
        return super().latest()


def closed_clients(broker: Broker) -> list[str]:
    """Those that ended with no DISCONNECT, whose wills the broker sends."""
    return re.findall(r"Client (occupancy-service-\S+) closed its connection\.", read(broker))


def publisher_errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "occupancy_service.publisher" and record.levelno >= logging.ERROR
    ]


def test_a_connection_that_cannot_be_made_is_logged_and_stop_returns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    publisher = Publisher(PublisherSettings(host=""), FakeResults({"a": CLEAR}))
    publisher.start()
    wait_for(lambda: publisher_errors(caplog))
    publisher.stop()
    assert len(publisher_errors(caplog)) == 1


class CountingResults(FakeResults):
    """A result source that counts each ``latest()``: one or more on each pass of the loop."""

    calls = 0

    def latest(self) -> L1Result:
        self.calls += 1
        return super().latest()


def test_no_python_file_under_src_names_rocrail() -> None:
    src = Path(__file__).parent.parent / "src"
    named = [path for path in src.rglob("*.py") if "rocrail" in path.read_text().lower()]
    assert named == []
    assert len(list(src.rglob("*.py"))) > 3


def an_l0(*detections: Any) -> L0:
    return L0(Frame(b"", 7, datetime(2026, 11, 5, 10, 15, 29, tzinfo=UTC)), detections)


def test_one_l0_message_per_detector_run_on_the_real_loop(broker: Broker, tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a", {"a": (160, 120)})
    source = FakeSource()
    source.show()
    detector = FakeDetector([detection(160, 120)])
    settings = L1Settings(archive, detect_every=0.3)
    loop = L1Loop(settings, source, FakeScorer({"a": 0.1}), detector=detector)
    with (
        subscribed(broker) as subscriber,
        running(loop),
        publishing(loop, settings_for(broker, "occupancy", "control")),
    ):

        def runs() -> int:
            source.show()  # The detector runs only on a frame it has not seen.
            time.sleep(0.05)
            return detector.runs

        wait_for(lambda: runs() >= 4)
        time.sleep(0.2)
        sent = len(subscriber.on(OWN_L0))
        ran = detector.runs
    assert ran - 1 <= sent <= ran  # A run before the broker connected sends nothing.
    assert {"state": "occupied", "confidence": 0.8} in map(payload, subscriber.on(OWN + "a"))


def test_the_paho_connection_s_will_goes_out_on_abandon_and_not_on_disconnect(
    broker: Broker,
) -> None:
    """What the test connection stands in for, on the wire."""
    connect = paho_connect(settings_for(broker))
    with subscribed(broker) as subscriber:
        clean = connect("occupancy-service-clean", "clean", ("will/clean", b"gone", 0, False))
        killed = connect("occupancy-service-killed", "killed", ("will/killed", b"gone", 0, False))
        wait_for(lambda: clean.is_connected() and killed.is_connected())
        assert clean.take_fresh() and not clean.take_fresh()
        assert clean.publish("said/clean", b"hello", 0, False)
        subscriber.wait_for(lambda m: m.topic == "said/clean")
        clean.disconnect()
        clean.join()
        assert clean.has_ended() and not clean.publish("said/clean", b"again", 0, False)
        killed.abandon()
        subscriber.wait_for(lambda m: m.topic == "will/killed")
        time.sleep(0.3)
        assert subscriber.on("will/clean") == []
    assert disconnected_clients(broker) == ["occupancy-service-clean"]
    assert closed_clients(broker) == ["occupancy-service-killed"]


# What the publisher decides, on the test connection.


@contextmanager
def faked(source: Any, *profiles: str) -> Iterator[tuple[Publisher, FakeConnect]]:
    """A publisher on the test connection, with ``profiles`` active."""
    connect = FakeConnect()
    settings = PublisherSettings(profiles=profiles or ("occupancy",))
    publisher = Publisher(settings, source, connect=connect)
    publisher.start()
    try:
        yield publisher, connect
    finally:
        publisher.stop()


def shared_connected(connect: FakeConnect) -> FakeConnection:
    """The shared connection, once it has been made, connected."""
    wait_for(lambda: connect.made)
    shared = connect.made[0]
    shared.connect()
    return shared


def bodies(connection: FakeConnection, topic: str) -> list[bytes]:
    return [message.body for message in connection.on(topic)]


def sent(message: Published) -> Any:
    return json.loads(message.body)


def test_shared_profiles_open_one_connection_between_them() -> None:
    source = FakeResults(jmri_sensors(6))
    with faked(source, "occupancy", "control") as (_, connect):
        shared = shared_connected(connect)
        wait_for(lambda: len(shared.on(CONTROL)) >= 6 and shared.on(OWN + "s05"))
        assert len(connect.made) == 1
    assert re.fullmatch(r"occupancy-service-[0-9a-f]{8}", shared.client_id)
    assert shared.will is None


@pytest.mark.parametrize(
    ("profile", "will"),
    [
        ("jmri", (JMRI + "a", b"ACTIVE", 0, True)),
        ("rocrail", (ROCRAIL, fb("a", True), 1, False)),
    ],
)
def test_each_sensor_on_a_per_sensor_profile_has_its_client_id_and_will(
    profile: str, will: tuple[str, bytes, int, bool]
) -> None:
    with faked(FakeResults({"a": CLEAR}), profile) as (_, connect):
        made = connect.made_as(f"occupancy-service-{profile}-archive-1-a")
    assert made.will == will
    assert made.label == f"for sensor 'a' on profile {profile}"


def test_a_per_sensor_profile_with_no_will_state_connects_with_no_will(tmp_path: Path) -> None:
    path = profile_file(tmp_path, connection="per_sensor")
    with faked(FakeResults({"a": CLEAR}), path) as (_, connect):
        made = connect.made_as("occupancy-service-fast-archive-1-a")
    assert made.will is None


@pytest.mark.parametrize(
    ("profile", "topic", "body"),
    [("jmri", JMRI + "{}", b"ACTIVE"), ("rocrail", ROCRAIL, fb("{}", True))],
)
def test_with_no_archive_id_each_sensor_connects_with_no_id_and_reports_unknown(
    profile: str, topic: str, body: bytes
) -> None:
    no_id = L1Entry("unknown", reason="archive has no id")
    source = FakeResults({"a": no_id, "b": no_id}, archive_id=None)
    with faked(source, profile) as (_, connect):
        for id in "ab":
            made = connect.connected(f"occupancy-service-{profile}-{id}")
            on = topic.format(id) if "{}" in topic else topic
            wait_for(lambda made=made, on=on: made.on(on))
            assert bodies(made, on)[0] == body.replace(b"{}", id.encode())
    assert sorted(connect.client_ids()) == [
        f"occupancy-service-{profile}-a",
        f"occupancy-service-{profile}-b",
    ]


def test_with_no_archive_id_only_a_shared_topic_that_needs_none_reports() -> None:
    source = FakeResults({"a": L1Entry("unknown", reason="archive has no id")}, archive_id=None)
    with faked(source, "occupancy", "control") as (_, connect):
        shared = shared_connected(connect)
        wait_for(lambda: len(shared.on(CONTROL)) >= 2)
    assert {message.topic for message in shared.published} == {CONTROL}


def test_jmri_connects_a_sensor_a_reload_adds_and_disconnects_one_it_removes() -> None:
    prefix = "occupancy-service-jmri-archive-1-"
    source = FakeResults({"kept": CLEAR, "removed": CLEAR})
    with faked(source, "jmri") as (_, connect):
        kept = connect.connected(prefix + "kept")
        removed = connect.connected(prefix + "removed")
        wait_for(lambda: kept.on(JMRI + "kept") and removed.on(JMRI + "removed"))
        source.show({"kept": CLEAR, "added": CLEAR})
        added = connect.connected(prefix + "added")
        assert wait_for(lambda: bodies(added, JMRI + "added")) == [b"INACTIVE"]
        wait_for(lambda: removed.disconnects == 1)
        assert bodies(removed, JMRI + "removed")[-1] == b"ACTIVE"  # Removed: unknown.
        assert (kept.disconnects, added.disconnects) == (0, 0)
    assert sorted(connect.client_ids()) == [prefix + "added", prefix + "kept", prefix + "removed"]


def test_rocrail_connects_a_sensor_a_reload_adds_and_disconnects_one_it_removes() -> None:
    prefix = "occupancy-service-rocrail-archive-1-"
    source = FakeResults({"kept": CLEAR, "removed": CLEAR, "busy": OCCUPIED})
    with faked(source, "rocrail") as (_, connect):
        made = {id: connect.connected(prefix + id) for id in ("kept", "removed", "busy")}
        wait_for(lambda: all(c.published for c in made.values()))
        source.show({"kept": CLEAR, "added": CLEAR})
        added = connect.connected(prefix + "added")
        assert wait_for(lambda: bodies(added, ROCRAIL)) == [fb("added", False)]
        wait_for(lambda: made["removed"].disconnects == made["busy"].disconnects == 1)
        assert made["kept"].disconnects == 0
        # "busy" was already true, so it gets nothing more: Rocrail would take it as a new event.
        assert bodies(made["busy"], ROCRAIL) == [fb("busy", True)]
        assert bodies(made["removed"], ROCRAIL) == [fb("removed", False), fb("removed", True)]


def test_a_per_sensor_topic_with_the_archive_id_says_removed_on_the_old_one_when_it_changes(
    tmp_path: Path,
) -> None:
    path = profile_file(tmp_path, topic="own/{archive_id}/{sensor}", connection="per_sensor")
    source = FakeResults({"a": CLEAR})
    with faked(source, path) as (_, connect):
        old = connect.connected("occupancy-service-fast-archive-1-a")
        wait_for(lambda: old.on("own/archive-1/a"))
        source.show({"a": CLEAR}, archive_id="archive-2")
        new = connect.connected("occupancy-service-fast-archive-2-a")
        assert sent(wait_for(lambda: new.on("own/archive-2/a"))[0]) == {"state": "clear"}
        wait_for(lambda: old.disconnects == 1)
        assert sent(old.on("own/archive-1/a")[-1]) == {
            "state": "unknown",
            "reason": "sensor removed",
        }
        assert new.disconnects == 0


@pytest.mark.parametrize(
    ("profile", "topic", "clear"),
    [("jmri", JMRI + "clear", b"INACTIVE"), ("rocrail", ROCRAIL, fb("clear", False))],
)
def test_a_clear_sensor_whose_topic_keeps_on_an_archive_id_change_says_nothing_else(
    profile: str, topic: str, clear: bytes
) -> None:
    source = FakeResults({"clear": CLEAR, "busy": OCCUPIED})
    with faked(source, profile) as (_, connect):
        old = {
            id: connect.connected(f"occupancy-service-{profile}-archive-1-{id}")
            for id in ("clear", "busy")
        }
        wait_for(lambda: old["clear"].on(topic) and old["busy"].published)
        source.show({"clear": CLEAR, "busy": OCCUPIED}, archive_id="archive-2")
        new = connect.connected(f"occupancy-service-{profile}-archive-2-clear")
        assert wait_for(lambda: bodies(new, topic))[0] == clear
        wait_for(lambda: all(c.disconnects == 1 for c in old.values()))
        assert set(bodies(old["clear"], topic)) == {clear}


def test_a_new_set_of_sensors_and_a_new_id_are_reported_and_a_removed_sensor_is_not(
    tmp_path: Path,
) -> None:
    fast = profile_file(tmp_path, topic="own/{archive_id}/{sensor}")
    source = FakeResults({"kept": CLEAR, "removed": CLEAR})
    with faked(source, fast) as (publisher, connect):
        shared = shared_connected(connect)
        wait_for(lambda: shared.on("own/archive-1/kept") and shared.on("own/archive-1/removed"))
        wait_for(lambda: "removed" in publisher.last_messages())
        source.show({"kept": CLEAR, "added": OCCUPIED}, archive_id="archive-2")
        wait_for(lambda: shared.on("own/archive-2/kept") and shared.on("own/archive-2/added"))
        wait_for(lambda: "removed" not in publisher.last_messages())
        since = len(shared.published)
        time.sleep(0.3)  # Past a repeat.
        assert {m.topic for m in shared.published[since:]} == {
            "own/archive-2/kept",
            "own/archive-2/added",
        }
    assert {message.topic for message in shared.published} == {
        "own/archive-1/kept",
        "own/archive-1/removed",
        "own/archive-2/kept",
        "own/archive-2/added",
    }


def test_a_clean_stop_sends_unknown_for_every_sensor_then_disconnects() -> None:
    source = FakeResults({"a": OCCUPIED, "b": CLEAR})
    with faked(source, "occupancy", "control") as (_, connect):
        shared = shared_connected(connect)
        wait_for(lambda: shared.on(OWN + "a") and shared.on(OWN + "b"))
    stopped = {"state": "unknown", "reason": "service stopped"}
    assert sent(shared.on(OWN + "a")[-1]) == stopped
    assert sent(shared.on(OWN + "b")[-1]) == stopped
    last = {sent(m)["addr"]: sent(m) for m in shared.on(CONTROL)}
    assert last["a"] == {"addr": "a", "occupancy": "unknown", "reason": "service stopped"}
    assert last["b"] == {"addr": "b", "occupancy": "unknown", "reason": "service stopped"}
    assert (shared.disconnects, shared.abandons, shared.joins) == (1, 0, 1)
    assert len(connect.made) == 1


def test_jmri_sends_active_for_every_sensor_on_a_clean_stop_then_disconnects() -> None:
    with faked(FakeResults({"a": CLEAR, "b": CLEAR}), "jmri") as (_, connect):
        made = [connect.connected(f"occupancy-service-jmri-archive-1-{id}") for id in "ab"]
        wait_for(lambda: all(c.published for c in made))
    for connection, id in zip(made, "ab", strict=True):
        assert bodies(connection, JMRI + id)[0] == b"INACTIVE"
        assert bodies(connection, JMRI + id)[-1] == b"ACTIVE"
        assert (connection.disconnects, connection.abandons, connection.joins) == (1, 0, 1)


def test_an_error_in_the_sending_loop_is_logged_and_abandons_every_connection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = FailingResults({"a": CLEAR, "b": CLEAR})
    with faked(source, "jmri", "occupancy") as (_, connect):
        wait_for(lambda: len(connect.made) == 3)
        for connection in connect.made:
            connection.connect()
        wait_for(lambda: connect.each(lambda c: bool(c.published)))
        source.failing = True
        wait_for(lambda: connect.each(lambda c: c.abandons == 1))
    assert connect.each(lambda c: c.disconnects == 0 and c.abandons == 1)
    (error,) = publisher_errors(caplog)
    assert error.exc_info is not None and "the source broke" in str(error.exc_info[1])


def test_a_closed_connection_is_joined_and_dropped_once_it_has_ended() -> None:
    prefix = "occupancy-service-jmri-archive-1-"
    source = CountingResults({"kept": CLEAR, "removed": CLEAR})
    with faked(source, "jmri") as (_, connect):
        kept = connect.connected(prefix + "kept")
        removed = connect.connected(prefix + "removed")
        source.show({"kept": CLEAR})
        wait_for(lambda: removed.disconnects == 1)
        calls = source.calls
        wait_for(lambda: source.calls > calls + 10)  # Serving, with it not ended.
        assert removed.joins == 0
        removed.end()
        wait_for(lambda: removed.joins == 1)
        calls = source.calls
        wait_for(lambda: source.calls > calls + 10)
        assert removed.joins == 1
    assert (removed.joins, kept.joins) == (1, 1)  # stop() joins it no more.


def test_the_own_vocabulary_profile_sends_each_l0_once_not_retained() -> None:
    source = FakeResults({"a": CLEAR})
    with faked(source) as (_, connect):
        shared = shared_connected(connect)
        wait_for(lambda: shared.on(OWN + "a"))
        source.l0 = an_l0(detection(160, 120, confidence=0.7))
        wait_for(lambda: shared.on(OWN_L0))
        time.sleep(0.3)
        assert len(shared.on(OWN_L0)) == 1
        source.l0 = an_l0()
        wait_for(lambda: len(shared.on(OWN_L0)) == 2)

    first, second = shared.on(OWN_L0)
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
    source = FakeResults({"a": CLEAR})
    source.l0 = an_l0(detection(160, 120))
    with faked(source, "control") as (_, connect):
        shared = shared_connected(connect)
        wait_for(lambda: len(shared.on(CONTROL)) >= 2)
    assert {message.topic for message in shared.published} == {CONTROL}


def test_no_l0_goes_out_with_no_archive_id() -> None:
    source = FakeResults({"a": L1Entry("unknown", reason="archive has no id")}, archive_id=None)
    source.l0 = an_l0(detection(160, 120))
    with faked(source, "occupancy", "control") as (_, connect):
        shared = shared_connected(connect)
        wait_for(lambda: len(shared.on(CONTROL)) >= 2)
        source.l0 = an_l0()
        time.sleep(0.3)
    assert {message.topic for message in shared.published} == {CONTROL}


def test_a_new_confidence_alone_sends_nothing(tmp_path: Path) -> None:
    """With no repeat, only an L1 change sends: ``FakeResults`` keeps ``L1Loop``'s rule."""
    source = FakeResults({"a": OCCUPIED})
    with faked(source, profile_file(tmp_path, repeat=0)) as (_, connect):
        shared = shared_connected(connect)
        wait_for(lambda: shared.on("own/a"))
        source.show({"a": L1Entry("occupied", confidence=0.8)})
        time.sleep(0.3)
        assert [sent(message) for message in shared.on("own/a")] == [
            {"state": "occupied", "confidence": 0.93}
        ]
