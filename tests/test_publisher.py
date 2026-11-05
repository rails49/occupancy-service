"""The publisher against a real ``mosquitto``, on a fake result source and on the real L1 loop."""

import json
import logging
import re
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fake_l1 import FakeScorer, FakeSource, running, wait_for, write_archive
from mqtt_broker import Broker, Received, Subscriber, mosquitto

from occupancy_service.l1 import L1Change, L1Entry, L1Loop, L1Result, L1Settings, L1Subscription
from occupancy_service.live_check import check
from occupancy_service.profile import ProfileError
from occupancy_service.publisher import Publisher, PublisherSettings
from occupancy_service.server import LiveServer, ServerSettings

CLEAR = L1Entry("clear")
OCCUPIED = L1Entry("occupied", confidence=0.93)
NO_FRAMES = L1Entry("unknown", reason="no frames")
OWN = "occupancy/archive-1/sensor/"
CONTROL = "tc49/layout/sensor"


class FakeResults:
    """A result source a test drives by hand."""

    def __init__(self, l1: Mapping[str, L1Entry], archive_id: str | None = "archive-1") -> None:
        self.l1 = dict(l1)
        self.archive_id = archive_id
        self.subscriptions: list[L1Subscription] = []

    def latest(self) -> L1Result:
        return L1Result(None, dict(self.l1), {}, None, {}, self.archive_id)

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
) -> str:
    """A profile that differs from the own-vocabulary one where a test needs it to."""
    path = folder / f"{name}.toml"
    path.write_text(f"""
topic = "{topic}"
retain = {str(retain).lower()}
qos = {qos}
repeat = {repeat}
[payload]
occupied = {occupied}
clear = {clear}
unknown = {unknown}
""")
    return str(path)


def payload(message: Received) -> Any:
    return json.loads(message.payload)


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
        wait_for(lambda: len(subscriber.on(CONTROL)) >= 3)
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


def test_a_new_reason_is_a_change(broker: Broker, tmp_path: Path) -> None:
    path = profile_file(tmp_path, repeat=60)
    source = FakeResults({"a": NO_FRAMES})
    with subscribed(broker) as subscriber, publishing(source, settings_for(broker, path)):
        wait_for(lambda: subscriber.on("own/a"))
        source.show({"a": L1Entry("unknown", reason="scoring stalled")})
        subscriber.wait_for(lambda m: payload(m).get("reason") == "scoring stalled")


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


def test_a_restarted_broker_gets_every_sensor_with_no_restart_here(
    broker: Broker, tmp_path: Path
) -> None:
    """Retained, with a repeat too long to help: a late subscriber sees what the new connection
    sent, whichever of the two got to the broker first."""
    path = profile_file(tmp_path, retain=True, repeat=60)
    source = FakeResults({"a": CLEAR, "b": CLEAR})
    settings = settings_for(broker, path, retry_every=0.05)
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
    with publishing(source, settings_for(broker, retry_every=0.05)):
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


@pytest.mark.parametrize("bad", ["bad+id", "bad#id", "bad/id"])
def test_a_sensor_id_a_topic_cannot_carry_is_logged_once_and_skipped_there(
    broker: Broker, tmp_path: Path, caplog: pytest.LogCaptureFixture, bad: str
) -> None:
    fast = profile_file(tmp_path)
    fast_list = profile_file(tmp_path, name="named", topic="list", clear='{ addr = "{sensor}" }')
    source = FakeResults({bad: CLEAR, "good": CLEAR})
    with (
        caplog.at_level(logging.WARNING, "occupancy_service.publisher"),
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
    refusals = [record for record in caplog.records if bad in record.getMessage()]
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


def test_last_messages_skip_a_sensor_a_profile_did_not_send(broker: Broker, tmp_path: Path) -> None:
    source = FakeResults({"a+b": CLEAR})
    settings = settings_for(broker, "occupancy", "control")
    with subscribed(broker) as subscriber, publishing(source, settings) as publisher:
        wait_for(lambda: subscriber.on(CONTROL))
        wait_for(lambda: publisher.last_messages().get("a+b"))
        assert set(publisher.last_messages()["a+b"]) == {"control"}


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


def test_no_python_file_under_src_names_the_controller() -> None:
    src = Path(__file__).parent.parent / "src"
    named = [path for path in src.rglob("*.py") if "tc49" in path.read_text()]
    assert named == []
    assert len(list(src.rglob("*.py"))) > 3
