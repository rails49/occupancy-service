"""The publisher against a real ``mosquitto``, on a fake result source and on the real L1 loop."""

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
from mqtt_broker import Broker, Received, Subscriber, free_port, mosquitto

from occupancy_service.capture import Frame
from occupancy_service.l0 import L0
from occupancy_service.l1 import L1Change, L1Entry, L1Loop, L1Result, L1Settings, L1Subscription
from occupancy_service.live_check import check
from occupancy_service.profile import ProfileError
from occupancy_service.publisher import Publisher, PublisherSettings, _Connection
from occupancy_service.server import LiveServer, ServerSettings

CLEAR = L1Entry("clear")
OCCUPIED = L1Entry("occupied", confidence=0.93)
NO_FRAMES = L1Entry("unknown", reason="no frames")
OWN = "occupancy/archive-1/sensor/"
CONTROL = "tc49/layout/sensor"
OWN_L0 = "occupancy/archive-1/l0"


class FakeResults:
    """A result source a test drives by hand."""

    def __init__(self, l1: Mapping[str, L1Entry], archive_id: str | None = "archive-1") -> None:
        self.l1 = dict(l1)
        self.archive_id = archive_id
        self.frame: Frame | None = None
        self.l0: L0 | None = None
        self.subscriptions: list[L1Subscription] = []

    def latest(self) -> L1Result:
        return L1Result(self.frame, dict(self.l1), {}, None, {}, self.archive_id, l0=self.l0)

    def subscribe(self) -> L1Subscription:
        subscription = L1Subscription(dict(self.l1), lambda: None)
        self.subscriptions.append(subscription)
        return subscription

    def show(self, l1: Mapping[str, L1Entry], archive_id: str | None = "archive-1") -> None:
        """Change the result, telling subscribers as ``L1Loop`` does."""
        before = self.l1
        self.l1, self.archive_id = dict(l1), archive_id
        for id, entry in l1.items():
            if before.get(id) != entry:
                for subscription in self.subscriptions:
                    subscription.queue.put(L1Change(id, entry))


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


def test_a_clean_stop_sends_unknown_for_every_sensor_then_nothing(broker: Broker) -> None:
    source = FakeResults({"a": OCCUPIED, "b": CLEAR})
    settings = settings_for(broker, "occupancy", "control")
    with subscribed(broker) as subscriber:
        with publishing(source, settings):
            wait_for(lambda: subscriber.on(OWN + "a") and subscriber.on(OWN + "b"))
        stopped = {"state": "unknown", "reason": "service stopped"}
        wait_for(lambda: payload(subscriber.on(OWN + "a")[-1]) == stopped)
        wait_for(lambda: payload(subscriber.on(OWN + "b")[-1]) == stopped)
        wait_for(lambda: len(subscriber.on(CONTROL)) >= 4)
        last = {payload(m)["addr"]: payload(m) for m in subscriber.on(CONTROL)}
        assert last["a"] == {"addr": "a", "occupancy": "unknown", "reason": "service stopped"}
        assert last["b"] == {"addr": "b", "occupancy": "unknown", "reason": "service stopped"}
        count = len(subscriber.messages)
        time.sleep(1.2)
        assert len(subscriber.messages) == count
    # A broker logs this only for a client that sent DISCONNECT, and otherwise that it "closed".
    assert re.search(r"Client occupancy-service-\w+ disconnected\.", broker.log.read_text())


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


def test_a_new_set_of_sensors_and_a_new_id_are_reported_and_a_removed_sensor_is_not(
    broker: Broker, tmp_path: Path
) -> None:
    fast = profile_file(tmp_path, topic="own/{archive_id}/{sensor}")
    source = FakeResults({"kept": CLEAR, "removed": CLEAR})
    settings = settings_for(broker, fast)
    with subscribed(broker) as subscriber, publishing(source, settings) as publisher:
        wait_for(lambda: subscriber.on("own/archive-1/kept"))
        wait_for(lambda: subscriber.on("own/archive-1/removed"))
        wait_for(lambda: "removed" in publisher.last_messages())
        source.show({"kept": CLEAR, "added": OCCUPIED}, archive_id="archive-2")
        wait_for(lambda: subscriber.on("own/archive-2/kept"))
        wait_for(lambda: subscriber.on("own/archive-2/added"))
        wait_for(lambda: "removed" not in publisher.last_messages())
        changed_at = time.monotonic()
        time.sleep(0.3)
    while_running = [
        message
        for message in subscriber.messages
        if changed_at <= message.at and "service stopped" not in message.payload.decode()
    ]
    assert {message.topic for message in while_running} == {
        "own/archive-2/kept",
        "own/archive-2/added",
    }
    assert {message.topic for message in subscriber.messages} == {
        "own/archive-1/kept",
        "own/archive-1/removed",
        "own/archive-2/kept",
        "own/archive-2/added",
    }


def test_a_per_sensor_topic_with_the_archive_id_says_removed_on_the_old_one_when_it_changes(
    broker: Broker, tmp_path: Path
) -> None:
    path = profile_file(tmp_path, topic="own/{archive_id}/{sensor}", connection="per_sensor")
    source = FakeResults({"a": CLEAR})
    with subscribed(broker) as subscriber, publishing(source, settings_for(broker, path)):
        wait_for(lambda: subscriber.on("own/archive-1/a"))
        changed_at = time.monotonic()
        source.show({"a": CLEAR}, archive_id="archive-2")
        removed = subscriber.wait_for(
            lambda m: m.topic == "own/archive-1/a" and payload(m).get("state") == "unknown"
        )
        assert payload(removed) == {"state": "unknown", "reason": "sensor removed"}
        assert removed.at > changed_at
        assert payload(subscriber.wait_for(lambda m: m.topic == "own/archive-2/a")) == {
            "state": "clear"
        }
        wait_for(lambda: disconnected_clients(broker) == ["occupancy-service-fast-archive-1-a"])
        assert "occupancy-service-fast-archive-2-a" in connected_clients(broker)


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


def test_jmri_connects_a_sensor_a_reload_adds_and_disconnects_one_it_removes(
    broker: Broker,
) -> None:
    source = FakeResults({"kept": CLEAR, "removed": CLEAR})
    with subscribed(broker) as subscriber, publishing(source, settings_for(broker, "jmri")):
        wait_for(lambda: subscriber.on(JMRI + "kept") and subscriber.on(JMRI + "removed"))
        source.show({"kept": CLEAR, "added": CLEAR})
        subscriber.wait_for(lambda m: m.topic == JMRI + "added" and m.payload == b"INACTIVE")
        subscriber.wait_for(lambda m: m.topic == JMRI + "removed" and m.payload == b"ACTIVE")
        wait_for(lambda: "occupancy-service-jmri-archive-1-removed" in disconnected_clients(broker))
        removed_at = time.monotonic()
        time.sleep(1.2)  # Past a repeat.
        assert not [m for m in subscriber.on(JMRI + "removed") if m.at > removed_at]
        assert retained(broker, [JMRI + "removed"])[JMRI + "removed"].payload == b"ACTIVE"
        assert disconnected_clients(broker) == ["occupancy-service-jmri-archive-1-removed"]
        assert "occupancy-service-jmri-archive-1-added" in connected_clients(broker)


def test_jmri_sends_a_clear_sensor_no_active_when_the_archive_id_changes(broker: Broker) -> None:
    source = FakeResults({"clear": CLEAR, "busy": OCCUPIED})
    with subscribed(broker) as subscriber, publishing(source, settings_for(broker, "jmri")):
        wait_for(lambda: subscriber.on(JMRI + "clear") and subscriber.on(JMRI + "busy"))
        changed_at = time.monotonic()
        source.show({"clear": CLEAR, "busy": OCCUPIED}, archive_id="archive-2")
        wait_for(lambda: len(disconnected_clients(broker)) == 2)
        subscriber.wait_for(lambda m: m.topic == JMRI + "clear" and m.at > changed_at)
        time.sleep(0.5)
        after = [m.payload for m in subscriber.on(JMRI + "clear") if m.at > changed_at]
        assert after and set(after) == {b"INACTIVE"}
        assert retained(broker, [JMRI + "clear"])[JMRI + "clear"].payload == b"INACTIVE"
        assert sorted(disconnected_clients(broker)) == [
            "occupancy-service-jmri-archive-1-busy",
            "occupancy-service-jmri-archive-1-clear",
        ]
        new = ["occupancy-service-jmri-archive-2-busy", "occupancy-service-jmri-archive-2-clear"]
        assert set(new) <= set(connected_clients(broker))


def test_jmri_leaves_active_retained_on_a_clean_stop(broker: Broker) -> None:
    sensors = {"a": CLEAR, "b": CLEAR}
    topics = [JMRI + id for id in sensors]
    with subscribed(broker) as subscriber:
        with publishing(FakeResults(sensors), settings_for(broker, "jmri")):
            wait_for(lambda: all(subscriber.on(topic) for topic in topics))
        assert {m.payload for m in subscriber.messages if m.topic in topics} == {
            b"INACTIVE",
            b"ACTIVE",
        }
    assert {message.payload for message in retained(broker, topics).values()} == {b"ACTIVE"}
    assert sorted(disconnected_clients(broker)) == [
        "occupancy-service-jmri-archive-1-a",
        "occupancy-service-jmri-archive-1-b",
    ]


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


def test_jmri_with_no_archive_id_reports_every_sensor_active(broker: Broker) -> None:
    no_id = L1Entry("unknown", reason="archive has no id")
    source = FakeResults({"a": no_id, "b": no_id}, archive_id=None)
    with publishing(source, settings_for(broker, "jmri")):
        late = retained(broker, [JMRI + "a", JMRI + "b"])
    assert {message.payload for message in late.values()} == {b"ACTIVE"}
    assert sorted(connected_clients(broker)) == [
        "occupancy-service-jmri-a",
        "occupancy-service-jmri-b",
    ]


def test_shared_profiles_open_one_connection_between_them(broker: Broker) -> None:
    source = FakeResults(jmri_sensors(6))
    settings = settings_for(broker, "occupancy", "control")
    with subscribed(broker) as subscriber, publishing(source, settings):
        wait_for(lambda: len(subscriber.on(CONTROL)) >= 6 and subscriber.on(OWN + "s05"))
        assert len(connected_clients(broker)) == 1


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


def test_rocrail_connects_a_sensor_a_reload_adds_and_disconnects_one_it_removes(
    broker: Broker,
) -> None:
    source = FakeResults({"kept": CLEAR, "removed": CLEAR, "busy": OCCUPIED})
    with (
        subscribed(broker, qos=1) as subscriber,
        publishing(source, settings_for(broker, "rocrail")),
    ):
        wait_for(lambda: len(subscriber.on(ROCRAIL)) == 3)
        source.show({"kept": CLEAR, "added": CLEAR})
        subscriber.wait_for(lambda m: m.payload == fb("added", False))
        subscriber.wait_for(lambda m: m.payload == fb("removed", True))
        prefix = "occupancy-service-rocrail-archive-1-"
        wait_for(lambda: len(disconnected_clients(broker)) == 2)
        assert sorted(disconnected_clients(broker)) == [prefix + "busy", prefix + "removed"]
        assert prefix + "added" in connected_clients(broker)
        time.sleep(0.5)
        # "busy" was already true, so it gets nothing more: Rocrail would take it as a new event.
        assert [m.payload for m in on_rocrail(subscriber, "busy")] == [fb("busy", True)]
        assert [m.payload for m in on_rocrail(subscriber, "removed")] == [
            fb("removed", False),
            fb("removed", True),
        ]


def test_rocrail_sends_a_clear_sensor_no_true_when_the_archive_id_changes(
    broker: Broker,
) -> None:
    source = FakeResults({"clear": CLEAR, "busy": OCCUPIED})
    with (
        subscribed(broker, qos=1) as subscriber,
        publishing(source, settings_for(broker, "rocrail")),
    ):
        wait_for(lambda: len(subscriber.on(ROCRAIL)) == 2)
        changed_at = time.monotonic()
        source.show({"clear": CLEAR, "busy": OCCUPIED}, archive_id="archive-2")
        old = "occupancy-service-rocrail-archive-1-"
        wait_for(lambda: sorted(disconnected_clients(broker)) == [old + "busy", old + "clear"])
        new = "occupancy-service-rocrail-archive-2-"
        wait_for(lambda: {new + "busy", new + "clear"} <= set(connected_clients(broker)))
        subscriber.wait_for(lambda m: m.payload == fb("clear", False) and m.at > changed_at)
        time.sleep(0.5)
        after = [m.payload for m in on_rocrail(subscriber, "clear") if m.at > changed_at]
        assert after == [fb("clear", False)]
        assert fb("clear", True) not in [m.payload for m in on_rocrail(subscriber, "clear")]


def test_rocrail_with_no_archive_id_reports_every_sensor_true(broker: Broker) -> None:
    no_id = L1Entry("unknown", reason="archive has no id")
    source = FakeResults({"a": no_id, "b": no_id}, archive_id=None)
    with (
        subscribed(broker, qos=1) as subscriber,
        publishing(source, settings_for(broker, "rocrail")),
    ):
        wait_for(lambda: len(subscriber.on(ROCRAIL)) == 2)
        assert sorted(m.payload for m in subscriber.on(ROCRAIL)) == [fb("a", True), fb("b", True)]
    assert sorted(connected_clients(broker)) == [
        "occupancy-service-rocrail-a",
        "occupancy-service-rocrail-b",
    ]


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


def test_an_error_in_the_sending_loop_is_logged_and_ends_every_connection_with_its_will(
    broker: Broker, caplog: pytest.LogCaptureFixture
) -> None:
    source = FailingResults({"a": CLEAR, "b": CLEAR})
    settings = settings_for(broker, "jmri", "occupancy")
    with subscribed(broker) as subscriber, publishing(source, settings):
        wait_for(lambda: subscriber.on(JMRI + "a") and subscriber.on(JMRI + "b"))
        wait_for(lambda: subscriber.on(OWN + "a") and subscriber.on(OWN + "b"))
        source.failing = True
        for id in "ab":  # Each sensor's will.
            subscriber.wait_for(lambda m, id=id: m.topic == JMRI + id and m.payload == b"ACTIVE")
        wait_for(lambda: len(closed_clients(broker)) == 3)
        assert len(set(connected_clients(broker))) == 3
        assert disconnected_clients(broker) == []
        time.sleep(0.3)
        assert len(publisher_errors(caplog)) == 1
    (error,) = publisher_errors(caplog)
    assert error.exc_info is not None and "the source broke" in str(error.exc_info[1])
    assert {m.payload for m in retained(broker, [JMRI + "a", JMRI + "b"]).values()} == {b"ACTIVE"}


def test_a_connection_that_cannot_be_made_is_logged_and_stop_returns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    publisher = Publisher(PublisherSettings(host=""), FakeResults({"a": CLEAR}))
    publisher.start()
    wait_for(lambda: publisher_errors(caplog))
    publisher.stop()
    assert len(publisher_errors(caplog)) == 1


def jmri_threads() -> list[str]:
    """The name of each paho thread running for a ``jmri`` sensor."""
    prefix = "paho-mqtt-client-occupancy-service-jmri-"
    return [t.name for t in threading.enumerate() if t.name.startswith(prefix)]


def test_a_closed_connection_is_joined_and_dropped_while_serving(broker: Broker) -> None:
    source = FakeResults({"kept": CLEAR})
    with publishing(source, settings_for(broker, "jmri")) as publisher:
        wait_for(lambda: "occupancy-service-jmri-archive-1-kept" in connected_clients(broker))
        for round in range(1, 6):
            # A sensor removed, and every connection on an archive ``id`` change.
            source.show({"kept": CLEAR, f"r{round}": CLEAR}, archive_id=f"archive-{round % 2}")
            ids = [
                f"occupancy-service-jmri-archive-{round % 2}-{id}" for id in ("kept", f"r{round}")
            ]
            wait_for(lambda ids=ids: set(ids) <= set(connected_clients(broker)))
        source.show({"kept": CLEAR}, archive_id="archive-1")
        wait_for(lambda: "occupancy-service-jmri-archive-1-r5" in disconnected_clients(broker))
        kept = ["paho-mqtt-client-occupancy-service-jmri-archive-1-kept"]
        wait_for(lambda: not publisher._closed and jmri_threads() == kept)  # With no stop().


class CountingResults(FakeResults):
    """A result source that counts each ``latest()``: one or more on each pass of the loop."""

    calls = 0

    def latest(self) -> L1Result:
        self.calls += 1
        return super().latest()


def test_a_connection_that_never_reached_the_broker_is_dropped_once_its_thread_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holding, release = threading.Event(), threading.Event()
    on_connect_fail = _Connection._on_connect_fail

    def held(connection: _Connection, client: Any, userdata: Any) -> None:
        """Keeps the removed sensor's paho thread in a failed connect until ``release``."""
        on_connect_fail(connection, client, userdata)
        if "'removed'" in connection.label:
            holding.set()
            release.wait(10)

    monkeypatch.setattr(_Connection, "_on_connect_fail", held)
    source = CountingResults({"kept": CLEAR, "removed": CLEAR})
    nothing_listens = PublisherSettings(host="127.0.0.1", port=free_port(), profiles=("jmri",))
    with publishing(source, nothing_listens) as publisher:
        closed = publisher._closed
        wait_for(holding.is_set)
        source.show({"kept": CLEAR})
        wait_for(lambda: closed)
        calls = source.calls
        wait_for(lambda: source.calls > calls + 10)  # Serving, with the thread not ended.
        assert len(closed) == 1
        release.set()
        wait_for(lambda: not closed)
        assert jmri_threads() == ["paho-mqtt-client-occupancy-service-jmri-archive-1-kept"]


def test_no_python_file_under_src_names_rocrail() -> None:
    src = Path(__file__).parent.parent / "src"
    named = [path for path in src.rglob("*.py") if "rocrail" in path.read_text().lower()]
    assert named == []
    assert len(list(src.rglob("*.py"))) > 3


def an_l0(*detections: Any) -> L0:
    return L0(Frame(b"", 7, datetime(2026, 11, 5, 10, 15, 29, tzinfo=UTC)), detections)


def test_the_own_vocabulary_profile_sends_each_l0_once_not_retained(broker: Broker) -> None:
    source = FakeResults({"a": CLEAR})
    with subscribed(broker) as subscriber, publishing(source, settings_for(broker)):
        wait_for(lambda: subscriber.on(OWN + "a"))
        source.l0 = an_l0(detection(160, 120, confidence=0.7))
        wait_for(lambda: subscriber.on(OWN_L0))
        time.sleep(0.3)
        assert len(subscriber.on(OWN_L0)) == 1
        source.l0 = an_l0()
        wait_for(lambda: len(subscriber.on(OWN_L0)) == 2)
        with subscribed(broker) as late:
            time.sleep(0.3)
            assert late.on(OWN_L0) == []

    first, second = subscriber.on(OWN_L0)
    assert payload(first) == {
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
    assert not first.retain
    assert payload(second)["detections"] == []


def test_the_control_profile_sends_no_l0(broker: Broker) -> None:
    source = FakeResults({"a": CLEAR})
    source.l0 = an_l0(detection(160, 120))
    with subscribed(broker) as subscriber, publishing(source, settings_for(broker, "control")):
        wait_for(lambda: len(subscriber.on(CONTROL)) >= 2)
    assert {message.topic for message in subscriber.messages} == {CONTROL}


def test_no_l0_goes_out_with_no_archive_id(broker: Broker) -> None:
    source = FakeResults({"a": L1Entry("unknown", reason="archive has no id")}, archive_id=None)
    source.l0 = an_l0(detection(160, 120))
    settings = settings_for(broker, "occupancy", "control")
    with subscribed(broker) as subscriber, publishing(source, settings):
        wait_for(lambda: len(subscriber.on(CONTROL)) >= 2)
        source.l0 = an_l0()
        time.sleep(0.3)
    assert {message.topic for message in subscriber.messages} == {CONTROL}


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
