"""Publish L1 over MQTT, as the active profiles say."""

import logging
import math
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from paho.mqtt import client as paho
from paho.mqtt.enums import CallbackAPIVersion, MQTTErrorCode
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from occupancy_service.active_profile import ActiveProfile, LastMessage, Send
from occupancy_service.l0 import L0, l0_json
from occupancy_service.l1 import L1Result, L1Subscription
from occupancy_service.profile import Profile, ProfileError, encode, load_profile

_log = logging.getLogger(__name__)

_WAKE = 0.05  # Seconds at most between looks at the broker and at stop().
_JOIN_WAIT = 10.0  # Seconds stop() waits: connecting has its own timeout.


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


class ResultSource(Protocol):
    """Where L1 comes from: an ``L1Loop``, or a test's fake."""

    def latest(self) -> L1Result: ...

    def subscribe(self) -> L1Subscription: ...


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
        # paho 2.2 has no public handle on its thread: ``_thread`` is it, until it ends.
        self._thread: threading.Thread | None = client._thread

    def is_connected(self) -> bool:
        return self.client.is_connected()

    def disconnect(self) -> None:
        """Ends the connection cleanly, so the broker drops its will. ``join`` waits for it."""
        self._closing = True
        self.client.disconnect()

    def join(self) -> None:
        self.client.loop_stop()

    def has_ended(self) -> bool:
        """Whether paho's thread has ended, so that ``join`` returns at once. After
        ``disconnect``, it ends soon after the DISCONNECT goes out, or with no connection up,
        after its connect attempt or its wait to retry."""
        return self._thread is None or not self._thread.is_alive()

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
    thread does the sending, through an ``ActiveProfile`` per profile, and opens and closes the
    connections of sensors a reload adds and removes, and sends L0.

    It keeps no result: each message comes from ``latest()``, a change only saying when to send,
    so a reload needs no restart and nothing it sends is older than ``latest()``.
    """

    def __init__(self, settings: PublisherSettings, source: ResultSource) -> None:
        """Raises ``ProfileError`` for an active profile that is wrong."""
        self._settings = settings
        self._source = source
        profiles = [load_profile(name) for name in settings.profiles]
        names = [profile.name for profile in profiles]
        for name in {name for name in names if names.count(name) > 1}:
            raise ProfileError(f"two active profiles are named {name}")
        self._active = {
            profile.name: ActiveProfile(profile, self._sender(profile)) for profile in profiles
        }
        self._shared_profiles = [
            a for a in self._active.values() if a.profile.connection == "shared"
        ]
        self._per_sensor_profiles = [
            a for a in self._active.values() if a.profile.connection == "per_sensor"
        ]
        self._l0_profiles = [
            a.profile for a in self._shared_profiles if a.profile.l0_topic is not None
        ]
        self._l0_seen: L0 | None = None  # The newest L0 this has sent, or passed over.
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
        merged: dict[str, dict[str, LastMessage]] = {}
        for name, active in self._active.items():
            for sensor, last in active.last_messages().items():
                merged.setdefault(sensor, {})[name] = last
        return merged

    def _run(self) -> None:
        """Serves until ``stop()``. An error here ends publishing: it is logged, and every
        connection ends with no DISCONNECT, so that each will goes out. Nothing restarts it."""
        subscription: L1Subscription | None = None
        try:
            subscription = self._source.subscribe()
            if self._shared_profiles:
                client_id = f"occupancy-service-{secrets.token_hex(4)}"
                self._shared = _Connection(self._settings, client_id, "shared")
            while not self._stopping.is_set():
                self._serve_once(subscription)
            newest = self._source.latest()
            for active in self._active.values():
                active.stop(newest)
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

    def _serve_once(self, subscription: L1Subscription) -> None:
        """One pass: waits for the first change or the next repeat, then sends what each new
        connection carries, a new L0, and each active profile's step. A change is told to the
        holds with no connection up too, as a break in ``clear`` restarts a hold."""
        next_due = min((a.next_due() for a in self._active.values()), default=math.inf)
        first = subscription.get(max(min(_WAKE, next_due - time.monotonic()), 0))
        newest = self._source.latest()
        # latest() queues each change up to its result before it returns, so after this no
        # change up to that result is still unread.
        changes = [] if first is None else [first]
        while (change := subscription.get(0)) is not None:
            changes.append(change)
        self._open_and_close(newest)
        self._join_ended()
        self._send_on_fresh(newest)
        self._send_l0(newest)
        now = time.monotonic()
        for active in self._active.values():
            active.step(changes, newest, now)

    def _open_and_close(self, result: L1Result) -> None:
        """A connection for each sensor of ``result`` on each ``per_sensor`` profile that can
        report it, opened with no restart. A connection whose archive ``id`` is not the newest one
        disconnects cleanly. First it says ``unknown`` if its sensor has gone or its topic has
        changed, as nothing will send on that topic again. A sensor that keeps its topic says
        nothing, so a controller never sees a clear sensor go ``unknown`` and back for no reason."""
        wanted: dict[tuple[str, str], ActiveProfile] = {}
        for active in self._per_sensor_profiles:
            for sensor in result.l1:
                if active.carries(sensor, result.archive_id):
                    wanted[(active.profile.name, sensor)] = active
        for key, connection in list(self._per_sensor.items()):
            if key not in wanted or connection.archive_id != result.archive_id:
                profile, sensor = connection.profile, connection.sensor
                topic = profile.topic_for(sensor, connection.archive_id)
                if key not in wanted or profile.topic_for(sensor, result.archive_id) != topic:
                    self._active[profile.name].removed(sensor, connection.archive_id)
                connection.disconnect()
                self._closed.append(connection)
                del self._per_sensor[key]
        for key, active in wanted.items():
            if key not in self._per_sensor:
                self._per_sensor[key] = self._connect(active.profile, key[1], result.archive_id)

    def _join_ended(self) -> None:
        """Joins and drops each closed connection whose thread has ended, and waits on none."""
        ended = [connection for connection in self._closed if connection.has_ended()]
        for connection in ended:
            connection.join()
            self._closed.remove(connection)

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
            for active in self._shared_profiles:
                active.fresh(result)
        for connection in self._per_sensor.values():
            sensor = connection.sensor
            if connection.fresh.is_set() and sensor in result.l1:
                connection.fresh.clear()
                self._active[connection.profile.name].fresh(result, sensor)

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

    def _sender(self, profile: Profile) -> Send:
        """The ``send`` of ``profile``'s ``ActiveProfile``: on the connection that carries the
        sensor, and only while it is up. What paho holds unacknowledged when a connection ends,
        the next one drops: it would go out stale after the reconnect."""

        def send(
            sensor: str, archive_id: str | None, topic: str, body: bytes, qos: int, retain: bool
        ) -> bool:
            connection = self._connection(profile, sensor, archive_id)
            if connection is None or not connection.is_connected():
                return False  # Each new connection sends what it carries.
            sent = connection.client.publish(topic, body, qos, retain)
            # Else the connection has just ended, and the next one sends what it carries.
            return sent.rc == MQTTErrorCode.MQTT_ERR_SUCCESS

        return send

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
