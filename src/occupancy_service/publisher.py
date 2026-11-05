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

from occupancy_service.l1 import L1Entry, L1Result, L1Subscription
from occupancy_service.profile import Message, Profile, ProfileError, encode, load_profile

_log = logging.getLogger(__name__)

_WAKE = 0.05  # Seconds at most between looks at the broker and at stop().
_JOIN_WAIT = 10.0  # Seconds stop() waits: connecting has its own timeout.
_STOPPED = L1Entry("unknown", reason="service stopped")


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
    keepalive: int = 30
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


class Publisher:
    """One connection for all profiles. paho's thread keeps it up and reconnects, and this class's
    own thread does the sending.

    It keeps no result: each message comes from ``latest()``, or from a change it has just been
    told of, so a reload needs no restart and a repeat is as fresh as ``latest()``.
    """

    def __init__(self, settings: PublisherSettings, source: ResultSource) -> None:
        """Raises ``ProfileError`` for an active profile that is wrong."""
        self._settings = settings
        self._source = source
        self._profiles = [load_profile(name) for name in settings.profiles]
        names = [profile.name for profile in self._profiles]
        for name in {name for name in names if names.count(name) > 1}:
            raise ProfileError(f"two active profiles are named {name}")
        self._lock = threading.Lock()
        self._last: dict[str, dict[str, LastMessage]] = {}
        self._refused: set[tuple[str, str]] = set()
        self._stopping = threading.Event()
        self._connected = threading.Event()  # Set by each new connection, for _run to take.
        self._log_connect_failure = True  # Once for each time the broker is out of reach.
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="mqtt", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Sends ``unknown`` for every sensor, when connected, then disconnects."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(_JOIN_WAIT)
            self._thread = None

    def last_messages(self) -> dict[str, dict[str, LastMessage]]:
        """Sensor id to profile name to what that profile last sent for it."""
        with self._lock:
            return {sensor: dict(by_profile) for sensor, by_profile in self._last.items()}

    def _run(self) -> None:
        settings = self._settings
        subscription = self._source.subscribe()
        client = paho.Client(
            CallbackAPIVersion.VERSION2,
            client_id=f"occupancy-service-{secrets.token_hex(4)}",
            protocol=paho.MQTTv311,
        )
        client.on_connect = self._on_connect
        client.on_connect_fail = self._on_connect_fail
        client.on_disconnect = self._on_disconnect
        client.connect_timeout = settings.timeout
        client.reconnect_delay_set(settings.retry_every, settings.retry_every)
        client.connect_async(settings.host, settings.port, settings.keepalive)
        client.loop_start()
        try:
            while not self._stopping.is_set():
                if self._connected.wait(_WAKE):
                    self._connected.clear()
                    _discard(subscription)  # The connection starts by sending every sensor.
                    self._serve(client, subscription)
            if client.is_connected():
                self._send_all(client, self._profiles, stopping=True)
            client.disconnect()
            client.loop_stop()
        finally:
            subscription.close()

    def _on_connect(
        self,
        client: paho.Client,
        userdata: Any,
        flags: paho.ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        if reason_code.is_failure:
            _log.warning("the broker refused the connection: %s", reason_code)
            return
        settings = self._settings
        _log.info("connected to the broker at %s:%s", settings.host, settings.port)
        self._log_connect_failure = True
        self._connected.set()

    def _on_connect_fail(self, client: paho.Client, userdata: Any) -> None:
        if self._log_connect_failure:
            self._log_connect_failure = False
            settings = self._settings
            _log.warning(
                "cannot reach the broker at %s:%s, and will keep trying",
                settings.host,
                settings.port,
            )

    def _on_disconnect(
        self,
        client: paho.Client,
        userdata: Any,
        flags: paho.DisconnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        if not self._stopping.is_set():
            _log.warning("lost the broker: %s", reason_code)

    def _serve(self, client: paho.Client, subscription: L1Subscription) -> None:
        """Every sensor now and on each repeat, and each change as it comes, until ``stop()``, the
        connection ends, or a new one has replaced it."""
        self._send_all(client, self._profiles)
        now = time.monotonic()
        due = {profile.name: now + profile.repeat for profile in self._profiles}
        while (
            not self._stopping.is_set() and not self._connected.is_set() and client.is_connected()
        ):
            next_due = min(due.values(), default=math.inf)
            change = subscription.get(max(min(_WAKE, next_due - time.monotonic()), 0))
            if change is not None:
                result = self._source.latest()
                if change.sensor in result.l1:  # Otherwise a reload has just removed it.
                    for profile in self._profiles:
                        self._send(client, profile, change.sensor, change.entry, result.archive_id)
            now = time.monotonic()
            repeating = [profile for profile in self._profiles if due[profile.name] <= now]
            if repeating:
                self._send_all(client, repeating)
            for profile in repeating:
                # On the same beat, unless it has fallen a whole interval behind.
                next_at = due[profile.name] + profile.repeat
                due[profile.name] = next_at if next_at > now else now + profile.repeat

    def _send_all(
        self, client: paho.Client, profiles: list[Profile], stopping: bool = False
    ) -> None:
        """Every sensor in the newest result, on ``profiles``."""
        result = self._source.latest()
        self._forget_all_but(result.l1)
        for profile in profiles:
            for sensor, entry in result.l1.items():
                sent = _STOPPED if stopping else entry
                self._send(client, profile, sensor, sent, result.archive_id)

    def _send(
        self,
        client: paho.Client,
        profile: Profile,
        sensor: str,
        entry: L1Entry,
        archive_id: str | None,
    ) -> None:
        if archive_id is None and profile.needs_archive_id:
            return
        topic = profile.topic_for(sensor, archive_id)
        if topic is None:
            if (profile.name, sensor) not in self._refused:
                self._refused.add((profile.name, sensor))
                _log.warning(
                    "profile %s cannot put sensor %r in a topic, and will not report it",
                    profile.name,
                    sensor,
                )
            return
        message = profile.message_for(sensor, entry)
        sent = client.publish(topic, encode(message), profile.qos, profile.retain)
        if sent.rc != MQTTErrorCode.MQTT_ERR_SUCCESS:
            return  # The connection has just ended, and the next one sends every sensor.
        with self._lock:
            by_profile = self._last.setdefault(sensor, {})
            by_profile[profile.name] = LastMessage(message, datetime.now(UTC))

    def _forget_all_but(self, sensors: Mapping[str, L1Entry]) -> None:
        with self._lock:
            self._last = {id: messages for id, messages in self._last.items() if id in sensors}


def _discard(subscription: L1Subscription) -> None:
    while subscription.get(0) is not None:
        pass
