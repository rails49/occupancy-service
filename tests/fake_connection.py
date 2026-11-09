"""A connection to no broker, which a test drives by hand, to test what ``Publisher`` decides."""

import threading
from collections.abc import Callable
from dataclasses import dataclass

from fake_l1 import wait_for

from occupancy_service.publisher import Will


@dataclass(frozen=True)
class Published:
    topic: str
    body: bytes
    qos: int
    retain: bool


class FakeConnection:
    """Records its client id, label and will, and every message it sent, ``disconnect``,
    ``abandon`` and ``join``. It starts down: ``connect()`` sets it connected and fresh, ``drop()``
    sets it down, and ``end()`` makes ``has_ended()`` true."""

    def __init__(self, client_id: str, label: str, will: Will | None) -> None:
        self.client_id = client_id
        self.label = label
        self.will = will
        self.published: list[Published] = []  # Only what went out while connected.
        self.disconnects = 0
        self.abandons = 0
        self.joins = 0
        self._lock = threading.Lock()
        self._connected = False
        self._fresh = False
        self._ended = False

    def connect(self) -> None:
        with self._lock:
            self._connected = self._fresh = True

    def drop(self) -> None:
        with self._lock:
            self._connected = False

    def end(self) -> None:
        with self._lock:
            self._ended = True

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

    def abandon(self) -> None:
        with self._lock:
            self.abandons += 1
            self._connected = False

    def has_ended(self) -> bool:
        with self._lock:
            return self._ended

    def join(self) -> None:
        with self._lock:
            self.joins += 1


class FakeConnect:
    """The ``connect`` of a ``Publisher``: keeps every connection made, in order."""

    def __init__(self) -> None:
        self.made: list[FakeConnection] = []

    def __call__(self, client_id: str, label: str, will: Will | None) -> FakeConnection:
        connection = FakeConnection(client_id, label, will)
        self.made.append(connection)
        return connection

    def client_ids(self) -> list[str]:
        return [connection.client_id for connection in self.made]

    def made_as(self, client_id: str) -> FakeConnection:
        """The connection made as ``client_id``, once it has been, and only once."""
        wait_for(lambda: client_id in self.client_ids())
        (connection,) = [c for c in self.made if c.client_id == client_id]
        return connection

    def connected(self, client_id: str) -> FakeConnection:
        """The connection made as ``client_id``, once it has been, connected."""
        connection = self.made_as(client_id)
        connection.connect()
        return connection

    def each(self, check: Callable[[FakeConnection], bool]) -> bool:
        return bool(self.made) and all(check(connection) for connection in self.made)
