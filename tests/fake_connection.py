"""A connection to no broker, which a test drives by hand, to test what ``Publisher`` decides."""

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class Published:
    topic: str
    body: bytes
    qos: int
    retain: bool


class FakeConnection:
    """Records its client id and label, and every message it sent, ``disconnect`` and ``join``.
    It starts down: ``connect()`` sets it connected and fresh, ``drop()`` sets it down."""

    def __init__(self, client_id: str, label: str) -> None:
        self.client_id = client_id
        self.label = label
        self.published: list[Published] = []  # Only what went out while connected.
        self.disconnects = 0
        self.joins = 0
        self._lock = threading.Lock()
        self._connected = False
        self._fresh = False

    def connect(self) -> None:
        with self._lock:
            self._connected = self._fresh = True

    def drop(self) -> None:
        with self._lock:
            self._connected = False

    def on(self, topic: str) -> list[Published]:
        with self._lock:
            return [message for message in self.published if message.topic == topic]

    def publish(self, topic: str, body: bytes, qos: int, retain: bool) -> bool:
        with self._lock:
            if self._connected:
                self.published.append(Published(topic, body, qos, retain))
            return self._connected

    def take_fresh(self) -> bool:
        with self._lock:
            fresh, self._fresh = self._fresh, False
            return fresh

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def disconnect(self) -> None:
        with self._lock:
            self.disconnects += 1
            self._connected = False

    def has_ended(self) -> bool:
        with self._lock:
            return self.disconnects > 0

    def join(self) -> None:
        with self._lock:
            self.joins += 1


class FakeConnect:
    """The ``connect`` of a ``Publisher``: keeps every connection made, in order."""

    def __init__(self) -> None:
        self.made: list[FakeConnection] = []

    def __call__(self, client_id: str, label: str) -> FakeConnection:
        connection = FakeConnection(client_id, label)
        self.made.append(connection)
        return connection
