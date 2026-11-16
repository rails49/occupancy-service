"""The publisher on the wire, against a real ``mosquitto``, on a fake result source and on the
real L1 loop; and its thread and connection, on the test connection."""

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
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
from rails49_testing.broker import Broker
from rails49_testing.ports import free_port
from rails49_testing.subscriber import Received, Subscriber

from occupancy_service.capture import Frame
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

    def __init__(self, l1: Mapping[str, L1Entry], archive_id: str = "archive-1") -> None:
        self.l1 = dict(l1)
        self.archive_id = archive_id
        self.frame: Frame | None = None
        self._lock = threading.Lock()  # Taken before the L1Changes lock, as L1Loop does.
        self._changes = L1Changes()
        self._manifest = object()
        self._changes.update(self.l1, self._manifest)

    def latest(self) -> L1Result:
        with self._lock:
            return L1Result(self.frame, dict(self.l1), {}, None, {}, self.archive_id)

    def subscribe(self) -> L1Subscription:
        with self._lock:
            return self._changes.subscribe()

    def show(self, l1: Mapping[str, L1Entry], archive_id: str = "archive-1") -> None:
        """Change the result, telling subscribers what ``L1Loop`` would."""
        with self._lock:
            if archive_id != self.archive_id:
                self._manifest = object()
            self.l1, self.archive_id = dict(l1), archive_id
            self._changes.update(self.l1, self._manifest)


@contextmanager
def subscribed(broker: Broker) -> Iterator[Subscriber]:
    subscriber = Subscriber(broker.port)
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
    occupied: str = '{ state = "occupied", confidence = "{confidence}" }',
    clear: str = '{ state = "clear" }',
    unknown: str = '{ state = "unknown", reason = "{reason}" }',
    clear_hold_s: float = 0,
) -> str:
    """A profile that differs from the own-vocabulary one where a test needs it to."""
    path = folder / f"{name}.toml"
    path.write_text(f"""
topic = "{topic}"
repeat = {repeat}
clear_hold_s = {clear_hold_s}
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
    path = profile_file(tmp_path, repeat=0.2)
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
    text = profile_file(tmp_path, name="text", topic="text/{sensor}", clear='{ s = "OFF" }')
    source = FakeResults({"a": CLEAR})
    with (
        subscribed(broker) as subscriber,
        publishing(source, settings_for(broker, "occupancy", text)) as publisher,
    ):
        assert wait_for(lambda: subscriber.on("text/a"))
        wait_for(lambda: len(publisher.last_messages().get("a", {})) == 2)
        messages = publisher.last_messages()["a"]
        assert messages["occupancy"].message == {"state": "clear"}
        assert messages["text"].message == {"s": "OFF"}
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
        server = LiveServer(ServerSettings(port=0), loop, publisher.last_messages)
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
    path = profile_file(tmp_path, repeat=0.2, clear_hold_s=60)
    source = FakeResults({"a": OCCUPIED})
    with publishing(source, settings_for(broker, path)):
        with subscribed(broker) as before:
            wait_for(lambda: before.on("own/a"))
        source.show({"a": CLEAR})
        time.sleep(0.1)
        broker.stop()
        broker.start()
        with subscribed(broker) as after:
            wait_for(lambda: len(after.on("own/a")) >= 2)
            assert set(states(after)) == {"occupied"}


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


def connected_clients(broker: Broker) -> list[str]:
    """The id of every connection this service made, in the broker's log."""
    return re.findall(r"New client connected from \S+ as (occupancy-service-\S+) ", read(broker))


def disconnected_clients(broker: Broker) -> list[str]:
    """Those that sent DISCONNECT: a broker logs that it "closed" any other."""
    return re.findall(r"Client (occupancy-service-\S+) disconnected\.", read(broker))


def read(broker: Broker) -> str:
    return broker.log.read_text(errors="replace")


class FailingResults(FakeResults):
    """A result source whose ``latest()`` raises once ``failing`` is set."""

    failing = False

    def latest(self) -> L1Result:
        if self.failing:
            raise RuntimeError("the source broke")
        return super().latest()


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


def test_the_paho_connection_is_fresh_once_and_ends_with_a_disconnect(broker: Broker) -> None:
    """What the test connection stands in for, on the wire."""
    connect = paho_connect(settings_for(broker))
    with subscribed(broker) as subscriber:
        clean = connect("occupancy-service-clean", "clean")
        wait_for(clean.is_connected)
        assert clean.take_fresh() and not clean.take_fresh()
        assert clean.publish("said/clean", b"hello")
        subscriber.wait_for(lambda m: m.topic == "said/clean")
        clean.disconnect()
        clean.join()
        assert clean.has_ended() and not clean.publish("said/clean", b"again")
    assert disconnected_clients(broker) == ["occupancy-service-clean"]


def test_a_paho_connection_that_never_connected_ends_after_disconnect() -> None:
    nothing_listens = PublisherSettings(host="127.0.0.1", port=free_port())
    connection = paho_connect(nothing_listens)("occupancy-service-alone", "alone")
    time.sleep(0.2)  # Past its first connect attempt, in its wait to retry.
    assert not connection.is_connected() and not connection.has_ended()
    connection.disconnect()
    wait_for(connection.has_ended)
    connection.join()


# The publisher's thread and connection, on the test connection.


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


def test_one_connection_with_a_random_client_id_is_disconnected_and_joined_once_on_stop() -> None:
    source = FakeResults({"a": OCCUPIED})
    with faked(source, "occupancy", "control") as (_, connect):
        shared = shared_connected(connect)
        wait_for(lambda: shared.on(OWN + "a") and shared.on(CONTROL))
    assert len(connect.made) == 1
    assert re.fullmatch(r"occupancy-service-[0-9a-f]{8}", shared.client_id)
    assert sent(shared.on(OWN + "a")[-1]) == {"state": "unknown", "reason": "service stopped"}
    assert (shared.disconnects, shared.joins) == (1, 1)


def test_no_active_profile_makes_no_connection() -> None:
    connect = FakeConnect()
    publisher = Publisher(PublisherSettings(profiles=()), FakeResults({"a": CLEAR}), connect)
    publisher.start()
    publisher.stop()
    assert connect.made == []


def test_an_error_in_the_sending_loop_is_logged_and_disconnects_cleanly_sending_nothing_more(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = FailingResults({"a": CLEAR, "b": CLEAR})
    with faked(source, "control", "occupancy") as (_, connect):
        shared = shared_connected(connect)
        wait_for(lambda: shared.on(CONTROL) and shared.on(OWN + "b"))
        source.failing = True
        wait_for(lambda: shared.joins == 1)
        sent_before = list(shared.published)
        time.sleep(0.3)
        # No "service stopped", and no repeat: what went out before the error is all there is.
        assert shared.published == sent_before
        assert b"service stopped" not in b"".join(bodies(shared, OWN + "a"))
    assert (shared.disconnects, shared.joins) == (1, 1)
    assert len(connect.made) == 1
    (error,) = publisher_errors(caplog)
    assert error.exc_info is not None and "the source broke" in str(error.exc_info[1])
