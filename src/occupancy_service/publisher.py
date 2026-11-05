"""Publish L1 over MQTT, as the active profiles say."""

import logging
import math
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from occupancy_service.l1 import L1Entry, L1Result, L1Subscription
from occupancy_service.mqtt import MqttClient
from occupancy_service.profile import Message, Profile, ProfileError, encode, load_profile

_log = logging.getLogger(__name__)

_WAKE = 0.05  # Seconds at most between looks at the broker and at stop().
_JOIN_WAIT = 10.0  # Seconds stop() waits: each socket call on the thread has its own timeout.
_STOPPED = L1Entry("unknown", reason="service stopped")


@dataclass(frozen=True)
class PublisherSettings:
    """The broker, with no authentication, and the active profiles: each a shipped profile's name
    or a path to a file. A lost broker is tried again every ``retry_every`` seconds.

    ``keepalive`` is the seconds promised to the broker, and ``timeout`` bounds connecting and each
    write. How often a profile repeats is that profile's ``repeat``.
    """

    host: str = "localhost"
    port: int = 1883
    profiles: tuple[str, ...] = ("occupancy",)
    retry_every: float = 1.0
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
    """One connection for all profiles, on its own thread, which reconnects for as long as it runs.

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
        subscription = self._source.subscribe()
        try:
            was_connected = True  # So that a broker that is down at startup is logged.
            while not self._stopping.is_set():
                _discard(subscription)  # The connection starts by sending every sensor.
                client = self._connect(was_connected)
                was_connected = client is not None
                if client is None:
                    self._stopping.wait(self._settings.retry_every)
                    continue
                try:
                    self._serve(client, subscription)
                    self._send_all(client, self._profiles, stopping=True)
                    client.disconnect()
                except OSError as error:
                    _log.warning("lost the broker: %s", error)
                finally:
                    client.close()
        finally:
            subscription.close()

    def _connect(self, log_failure: bool) -> MqttClient | None:
        settings = self._settings
        try:
            client = MqttClient(settings.host, settings.port, settings.keepalive, settings.timeout)
        except OSError as error:
            if log_failure:
                _log.warning(
                    "cannot reach the broker at %s:%s, and will keep trying: %s",
                    settings.host,
                    settings.port,
                    error,
                )
            return None
        _log.info("connected to the broker at %s:%s", settings.host, settings.port)
        return client

    def _serve(self, client: MqttClient, subscription: L1Subscription) -> None:
        """Every sensor now and on each repeat, and each change as it comes, until ``stop()``."""
        self._send_all(client, self._profiles)
        now = time.monotonic()
        due = {profile.name: now + profile.repeat for profile in self._profiles}
        while not self._stopping.is_set():
            next_due = min(due.values(), default=math.inf)
            change = subscription.get(max(min(_WAKE, next_due - time.monotonic()), 0))
            if change is not None:
                result = self._source.latest()
                if change.sensor in result.l1:  # Otherwise a reload has just removed it.
                    for profile in self._profiles:
                        self._send(client, profile, change.sensor, change.entry, result.archive_id)
            client.poll()
            now = time.monotonic()
            repeating = [profile for profile in self._profiles if due[profile.name] <= now]
            if repeating:
                self._send_all(client, repeating)
            for profile in repeating:
                # On the same beat, unless it has fallen a whole interval behind.
                next_at = due[profile.name] + profile.repeat
                due[profile.name] = next_at if next_at > now else now + profile.repeat

    def _send_all(
        self, client: MqttClient, profiles: list[Profile], stopping: bool = False
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
        client: MqttClient,
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
        client.publish(topic, encode(message), profile.qos, profile.retain)
        with self._lock:
            by_profile = self._last.setdefault(sensor, {})
            by_profile[profile.name] = LastMessage(message, datetime.now(UTC))

    def _forget_all_but(self, sensors: Mapping[str, L1Entry]) -> None:
        with self._lock:
            self._last = {id: messages for id, messages in self._last.items() if id in sensors}


def _discard(subscription: L1Subscription) -> None:
    while subscription.get(0) is not None:
        pass
