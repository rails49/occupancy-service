"""Publish L1 over MQTT, as the active profiles say."""

import logging
import math
import secrets
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from paho.mqtt import client as paho
from paho.mqtt.enums import CallbackAPIVersion, MQTTErrorCode
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from occupancy_service.l0 import L0, l0_json
from occupancy_service.l1 import L1Change, L1Entry, L1Result, L1Subscription
from occupancy_service.profile import Message, Profile, ProfileError, encode, load_profile

_log = logging.getLogger(__name__)

_WAKE = 0.05  # Seconds at most between looks at the broker and at stop().
_JOIN_WAIT = 10.0  # Seconds stop() waits: connecting has its own timeout.
_STOPPED = L1Entry("unknown", reason="service stopped")
_NOT_YET_HELD = L1Entry("unknown", reason="clear not yet held")  # A sensor with nothing sent yet.
_REMOVED = L1Entry("unknown", reason="sensor removed")  # Before its own connection ends.


@dataclass(frozen=True)
class PublisherSettings:
    """The broker, with no authentication, and the active profiles: each a shipped profile's name
    or a path to a file. A lost broker is tried again every ``retry_every`` seconds.

    ``keepalive`` is the seconds promised to the broker, and ``timeout`` bounds connecting. How
    often a profile repeats is that profile's ``repeat``.
    """

    host: str = "localhost"
    port: int = 1883
    profiles: tuple[str, ...] = ("occupancy",)
    retry_every: int = 1
    keepalive: int = 5
    timeout: float = 2.0


@dataclass(frozen=True)
class LastMessage:
    """What a profile last sent for a sensor, and when: a table as a table, a string as a string."""

    message: Message
    time: datetime


class ResultSource(Protocol):
    """Where L1 comes from: an ``L1Loop``, or a test's fake."""

    def latest(self) -> L1Result: ...

    def subscribe(self) -> L1Subscription: ...


@dataclass
class _HeldSensor:
    """The last entry a profile reported for a sensor, and the capture time of the first ``clear``
    frame since, while that entry is not ``clear``."""

    reported: L1Entry
    clear_since: datetime | None = None


class _ClearHold:
    """Keeps one profile's sensors from going to ``clear`` until ``clear`` has stood for
    ``seconds``. The README documents the rules."""

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._sensors: dict[str, _HeldSensor] = {}

    def report(self, sensor: str, entry: L1Entry, captured_at: datetime) -> L1Entry:
        """What to say for ``sensor`` now that it reads ``entry``. This never ends a hold."""
        held = self._sensors.setdefault(sensor, _HeldSensor(_NOT_YET_HELD))
        if entry.state != "clear":
            held.reported, held.clear_since = entry, None
        elif held.reported.state != "clear" and held.clear_since is None:
            held.clear_since = captured_at
        return held.reported

    def release(self, sensor: str, entry: L1Entry, captured_at: datetime) -> bool:
        """Whether a result captured at ``captured_at`` ends the hold on ``sensor``. The caller has
        passed every change up to that result to ``report``, or a break could go unseen."""
        held = self._sensors.get(sensor)
        if held is None or held.clear_since is None or entry.state != "clear":
            return False
        if (captured_at - held.clear_since).total_seconds() < self._seconds:
            return False
        held.reported, held.clear_since = entry, None
        return True

    def forget_all_but(self, sensors: Mapping[str, L1Entry]) -> None:
        self._sensors = {id: held for id, held in self._sensors.items() if id in sensors}


class _Connection:
    """One paho client, which paho's thread keeps up and reconnects. Each new connection drops
    what paho still holds from the one before, then sets ``fresh``, for the publisher's thread to
    take and send what this connection carries."""

    def __init__(
        self,
        settings: PublisherSettings,
        client_id: str,
        label: str,
        will: tuple[str, bytes, int, bool] | None = None,
        archive_id: str | None = None,
    ) -> None:
        self.label = label
        self.archive_id = archive_id
        self.fresh = threading.Event()
        self._settings = settings
        self._closing = False
        self._log_connect_failure = True  # Once for each time the broker is out of reach.
        self.client = client = paho.Client(
            CallbackAPIVersion.VERSION2, client_id=client_id, protocol=paho.MQTTv311
        )
        client.on_connect = self._on_connect
        client.on_connect_fail = self._on_connect_fail
        client.on_disconnect = self._on_disconnect
        if will is not None:
            client.will_set(*will)
        client.connect_timeout = settings.timeout
        client.reconnect_delay_set(settings.retry_every, settings.retry_every)
        client.connect_async(settings.host, settings.port, settings.keepalive)
        client.loop_start()

    def is_connected(self) -> bool:
        return self.client.is_connected()

    def disconnect(self) -> None:
        """Ends the connection cleanly, so the broker drops its will. ``join`` waits for it."""
        self._closing = True
        self.client.disconnect()

    def join(self) -> None:
        self.client.loop_stop()

    def abandon(self) -> None:
        """Ends the connection with no DISCONNECT, so the broker sends its will, and sends
        nothing more: what paho still holds is dropped, as paho's thread would wait for it."""
        self._closing = True
        self.client.drop_out_messages()
        self.client.loop_stop()
        sock = self.client.socket()
        if sock is not None:
            sock.close()

    def _on_connect(
        self,
        client: paho.Client,
        userdata: Any,
        flags: paho.ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        if reason_code.is_failure:
            _log.warning("the broker refused the connection %s: %s", self.label, reason_code)
            return
        settings = self._settings
        _log.info("connected %s to the broker at %s:%s", self.label, settings.host, settings.port)
        self._log_connect_failure = True
        # A QoS 1 or 2 message the broker never acknowledged, which paho would send again after
        # this callback returns. It is stale by now: ``fresh`` sends what is current instead.
        client.drop_out_messages()
        self.fresh.set()

    def _on_connect_fail(self, client: paho.Client, userdata: Any) -> None:
        if self._log_connect_failure:
            self._log_connect_failure = False
            settings = self._settings
            _log.warning(
                "cannot reach the broker at %s:%s for %s, and will keep trying",
                settings.host,
                settings.port,
                self.label,
            )

    def _on_disconnect(
        self,
        client: paho.Client,
        userdata: Any,
        flags: paho.DisconnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        if not self._closing:
            _log.warning("lost the broker on %s: %s", self.label, reason_code)


class _SensorConnection(_Connection):
    """The connection that carries one sensor on one ``per_sensor`` profile."""

    def __init__(
        self,
        settings: PublisherSettings,
        client_id: str,
        will: tuple[str, bytes, int, bool] | None,
        archive_id: str | None,
        profile: Profile,
        sensor: str,
    ) -> None:
        label = f"for sensor {sensor!r} on profile {profile.name}"
        super().__init__(settings, client_id, label, will, archive_id)
        self.profile = profile
        self.sensor = sensor


class Publisher:
    """One connection for the profiles with a ``shared`` connection, and one for each sensor on
    each ``per_sensor`` profile. paho's threads keep them up and reconnect, and this class's own
    thread does the sending, and opens and closes the connections of sensors a reload adds and
    removes.

    It keeps no result: each message comes from ``latest()``, a change only saying when to send,
    so a reload needs no restart and nothing it sends is older than ``latest()``. The one thing
    it keeps, for a profile with a hold, is what that profile last reported.
    """

    def __init__(self, settings: PublisherSettings, source: ResultSource) -> None:
        """Raises ``ProfileError`` for an active profile that is wrong."""
        self._settings = settings
        self._source = source
        self._profiles = [load_profile(name) for name in settings.profiles]
        names = [profile.name for profile in self._profiles]
        for name in {name for name in names if names.count(name) > 1}:
            raise ProfileError(f"two active profiles are named {name}")
        self._shared_profiles = [p for p in self._profiles if p.connection == "shared"]
        self._per_sensor_profiles = [p for p in self._profiles if p.connection == "per_sensor"]
        self._l0_profiles = [p for p in self._shared_profiles if p.l0_topic is not None]
        self._l0_seen: L0 | None = None  # The newest L0 this has sent, or passed over.
        self._holds = {
            profile.name: _ClearHold(profile.clear_hold_s)
            for profile in self._profiles
            if profile.clear_hold_s > 0
        }
        self._lock = threading.Lock()
        self._last: dict[str, dict[str, LastMessage]] = {}
        self._refused: set[tuple[str, str]] = set()
        # The topic and payload last sent, by profile name and sensor, on a profile with no repeat.
        self._sent: dict[tuple[str, str], tuple[str, bytes]] = {}
        self._stopping = threading.Event()
        self._shared: _Connection | None = None
        # By profile name and sensor.
        self._per_sensor: dict[tuple[str, str], _SensorConnection] = {}
        self._closed: list[_Connection] = []  # Disconnected, and not yet joined.
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="mqtt", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Sends ``unknown`` for every sensor on each connection that is up, then disconnects."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(_JOIN_WAIT)
            self._thread = None

    def last_messages(self) -> dict[str, dict[str, LastMessage]]:
        """Sensor id to profile name to what that profile last sent for it."""
        with self._lock:
            return {sensor: dict(by_profile) for sensor, by_profile in self._last.items()}

    def _run(self) -> None:
        """Serves until ``stop()``. An error here ends publishing: it is logged, and every
        connection ends with no DISCONNECT, so that each will goes out. Nothing restarts it."""
        subscription: L1Subscription | None = None
        try:
            subscription = self._source.subscribe()
            if self._shared_profiles:
                client_id = f"occupancy-service-{secrets.token_hex(4)}"
                self._shared = _Connection(self._settings, client_id, "shared")
            now = time.monotonic()
            due = {p.name: now + p.repeat for p in self._profiles if p.repeat > 0}
            while not self._stopping.is_set():
                self._serve_once(subscription, due)
            self._send_all(self._profiles, stopping=True)
            connections = self._connections()
            for connection in connections:
                connection.disconnect()
            for connection in connections:  # Apart, so that each waits on its own at once.
                connection.join()
        except Exception:
            _log.exception("publishing has stopped, and every connection to the broker is dropped")
            for connection in self._connections():
                connection.abandon()
        finally:
            if subscription is not None:
                subscription.close()

    def _connections(self) -> list[_Connection]:
        """Every connection made, up or not, including those disconnected and not yet joined."""
        connections = [*self._per_sensor.values(), *self._closed]
        if self._shared is not None:
            connections.append(self._shared)
        return connections

    def _serve_once(self, subscription: L1Subscription, due: dict[str, float]) -> None:
        """Every sensor on each new connection, each change as it comes, and every sensor on each
        repeat. A change is told to the holds with no connection up too, as a break in ``clear``
        restarts a hold."""
        result = self._source.latest()
        self._open_and_close(result)
        self._send_on_fresh(result)
        self._send_l0(result)
        next_due = min(due.values(), default=math.inf)
        change = subscription.get(max(min(_WAKE, next_due - time.monotonic()), 0))
        if change is not None:
            self._send_change(change)
        self._end_holds(subscription)
        now = time.monotonic()
        repeating = [p for p in self._profiles if p.name in due and due[p.name] <= now]
        if repeating:
            self._send_all(repeating)
        for profile in repeating:
            # On the same beat, unless it has fallen a whole interval behind.
            next_at = due[profile.name] + profile.repeat
            due[profile.name] = next_at if next_at > now else now + profile.repeat

    def _open_and_close(self, result: L1Result) -> None:
        """A connection for each sensor of ``result`` on each ``per_sensor`` profile that can
        report it, opened with no restart. A connection whose archive ``id`` is not the newest one
        disconnects cleanly. First it says ``unknown`` if its sensor has gone or its topic has
        changed, as nothing will send on that topic again. A sensor that keeps its topic says
        nothing, so a controller never sees a clear sensor go ``unknown`` and back for no reason."""
        wanted: dict[tuple[str, str], Profile] = {}
        for profile in self._per_sensor_profiles:
            if result.archive_id is None and profile.needs_archive_id:
                continue
            for sensor in result.l1:
                if profile.topic_for(sensor, result.archive_id) is not None:
                    wanted[(profile.name, sensor)] = profile
        for key, connection in list(self._per_sensor.items()):
            if key not in wanted or connection.archive_id != result.archive_id:
                profile, sensor = connection.profile, connection.sensor
                topic = profile.topic_for(sensor, connection.archive_id)
                if key not in wanted or profile.topic_for(sensor, result.archive_id) != topic:
                    self._publish(connection, profile, sensor, _REMOVED, connection.archive_id)
                connection.disconnect()
                self._closed.append(connection)
                del self._per_sensor[key]
        for key, profile in wanted.items():
            if key not in self._per_sensor:
                self._per_sensor[key] = self._connect(profile, key[1], result.archive_id)
        if any(sensor not in result.l1 for _, sensor in self._sent):
            # So a sensor a reload adds back goes out on its first change.
            self._sent = {key: sent for key, sent in self._sent.items() if key[1] in result.l1}

    def _connect(self, profile: Profile, sensor: str, archive_id: str | None) -> _SensorConnection:
        topic = profile.topic_for(sensor, archive_id)
        will = profile.will_for(sensor)
        last_will = None
        if topic is not None and will is not None:
            last_will = (topic, encode(will), profile.qos, profile.retain)
        parts = ["occupancy-service", profile.name, *([archive_id] if archive_id else []), sensor]
        return _SensorConnection(
            self._settings, "-".join(parts), last_will, archive_id, profile, sensor
        )

    def _send_on_fresh(self, result: L1Result) -> None:
        """What each connection carries, when it has just connected."""
        if self._shared is not None and self._shared.fresh.is_set():
            self._shared.fresh.clear()
            self._send_all(self._shared_profiles, fresh=True)
        for connection in self._per_sensor.values():
            sensor = connection.sensor
            if connection.fresh.is_set() and sensor in result.l1:
                connection.fresh.clear()
                self._report(connection.profile, sensor, result.l1[sensor], result, fresh=True)

    def _send_l0(self, result: L1Result) -> None:
        """A new L0, once, on each profile with an ``l0_topic``, not retained. Nothing goes out
        with no archive ``id``, or while the broker is gone."""
        l0 = result.l0
        if l0 is None or l0 is self._l0_seen:
            return
        self._l0_seen = l0
        if self._shared is None or not self._shared.is_connected():
            return
        body = encode(l0_json(l0))
        for profile in self._l0_profiles:
            topic = profile.l0_topic_for(result.archive_id)
            if topic is not None:
                self._shared.client.publish(topic, body, profile.qos, retain=False)

    def _send_change(self, change: L1Change) -> None:
        """Sends the sensor's entry in the newest result, not the change's: a later change may
        already have replaced it. Each hold is told the change's entry first, as a break in
        ``clear`` restarts a hold even when ``latest()`` has already left it."""
        result = self._source.latest()
        entry = result.l1.get(change.sensor)
        if entry is None:
            return  # A reload has just removed it.
        for profile in self._profiles:
            hold = self._holds.get(profile.name)
            if hold is not None:
                hold.report(change.sensor, change.entry, _captured_at(result))
            self._report(profile, change.sensor, entry, result)

    def _end_holds(self, subscription: L1Subscription) -> None:
        """Sends ``clear`` for each sensor whose hold the newest result ends, where it can be sent:
        until then the profile says what it last published."""
        if not self._holds:
            return
        result = self._source.latest()
        self._forget_all_but(result.l1)
        # latest() queues each change up to its result before it returns, so after this no break
        # in ``clear`` up to that result is still unread.
        while (change := subscription.get(0)) is not None:
            self._send_change(change)
        captured_at = _captured_at(result)
        for profile in self._profiles:
            hold = self._holds.get(profile.name)
            if hold is None:
                continue
            for sensor, entry in result.l1.items():
                connection = self._connection(profile, sensor, result.archive_id)
                if connection is None or not connection.is_connected():
                    continue
                if hold.release(sensor, entry, captured_at):
                    self._send(profile, sensor, entry, result.archive_id)

    def _send_all(
        self, profiles: list[Profile], stopping: bool = False, fresh: bool = False
    ) -> None:
        """Every sensor in the newest result, on ``profiles``. ``fresh`` for a new connection."""
        result = self._source.latest()
        self._forget_all_but(result.l1)
        for profile in profiles:
            for sensor, entry in result.l1.items():
                self._report(profile, sensor, _STOPPED if stopping else entry, result, fresh)

    def _report(
        self,
        profile: Profile,
        sensor: str,
        entry: L1Entry,
        result: L1Result,
        fresh: bool = False,
    ) -> None:
        """Sends ``entry``, or on a profile holding this sensor what that profile last reported."""
        hold = self._holds.get(profile.name)
        if hold is not None:
            entry = hold.report(sensor, entry, _captured_at(result))
        self._send(profile, sensor, entry, result.archive_id, fresh)

    def _connection(
        self, profile: Profile, sensor: str, archive_id: str | None
    ) -> _Connection | None:
        """The connection that carries ``sensor`` on ``profile``, if it has one."""
        if profile.connection == "shared":
            return self._shared
        connection = self._per_sensor.get((profile.name, sensor))
        return (
            connection if connection is not None and connection.archive_id == archive_id else None
        )

    def _send(
        self,
        profile: Profile,
        sensor: str,
        entry: L1Entry,
        archive_id: str | None,
        fresh: bool = False,
    ) -> None:
        if archive_id is None and profile.needs_archive_id:
            return
        if profile.topic_for(sensor, archive_id) is None:
            if (profile.name, sensor) not in self._refused:
                self._refused.add((profile.name, sensor))
                _log.warning(
                    "profile %s cannot put sensor %r in a topic, and will not report it",
                    profile.name,
                    sensor,
                )
            return
        connection = self._connection(profile, sensor, archive_id)
        if connection is not None:
            self._publish(connection, profile, sensor, entry, archive_id, fresh)

    def _publish(
        self,
        connection: _Connection,
        profile: Profile,
        sensor: str,
        entry: L1Entry,
        archive_id: str | None,
        fresh: bool = False,
    ) -> None:
        """Sends ``entry`` on ``connection``. On a profile with no repeat, only what differs from
        what went last, unless ``fresh``: a new connection sends everything it carries."""
        # Not while it is down. What paho holds unacknowledged when a connection ends, the next
        # one drops: it would go out stale after the reconnect.
        topic = profile.topic_for(sensor, archive_id)
        if topic is None or not connection.is_connected():
            return  # Each new connection sends what it carries.
        message = profile.message_for(sensor, entry)
        body = encode(message)
        key = (profile.name, sensor)
        if profile.repeat == 0 and not fresh and self._sent.get(key) == (topic, body):
            return
        sent = connection.client.publish(topic, body, profile.qos, profile.retain)
        if sent.rc != MQTTErrorCode.MQTT_ERR_SUCCESS:
            return  # The connection has just ended, and the next one sends what it carries.
        if profile.repeat == 0:
            self._sent[key] = (topic, body)
        with self._lock:
            by_profile = self._last.setdefault(sensor, {})
            by_profile[profile.name] = LastMessage(message, datetime.now(UTC))

    def _forget_all_but(self, sensors: Mapping[str, L1Entry]) -> None:
        with self._lock:
            self._last = {id: messages for id, messages in self._last.items() if id in sensors}
        for hold in self._holds.values():
            hold.forget_all_but(sensors)


def _captured_at(result: L1Result) -> datetime:
    """When the frame of ``result`` was captured. A result with no frame stands for now."""
    return result.frame.arrived_at if result.frame is not None else datetime.now(UTC)
