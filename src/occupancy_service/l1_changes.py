"""Which L1 entries are L1 changes, and who hears of them."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from queue import Empty, SimpleQueue
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # l1 imports this module, so this one cannot import l1 when it runs.
    from occupancy_service.l1 import L1Entry


@dataclass(frozen=True)
class L1Change:
    """A sensor's new state or new reason, or the first state of a sensor a reload brought."""

    sensor: str
    entry: L1Entry


class L1Subscription:
    """The L1 changes since ``state``, each sensor's L1 when this subscription began.

    Changes wait here until they are read, so close it when done. ``outdated`` turns true at the
    first result under a manifest newer than ``state``. Changes keep coming after that, but none
    says that a sensor has gone, so a reader that needs the list of sensors subscribes again.
    """

    def __init__(self, state: Mapping[str, L1Entry], close: Callable[[], None]) -> None:
        self.state = state
        self.outdated = False
        self._queue: SimpleQueue[L1Change] = SimpleQueue()
        self._close = close

    def get(self, timeout: float | None = None) -> L1Change | None:
        """The next change. None when there is none within ``timeout`` seconds."""
        try:
            return self._queue.get(timeout=timeout)
        except Empty:
            return None

    def backlog(self) -> int:
        """The count of changes waiting to be read."""
        return self._queue.qsize()

    def close(self) -> None:
        self._close()

    def _put(self, change: L1Change) -> None:
        self._queue.put(change)


_NO_MANIFEST = object()


class L1Changes:
    """Each sensor's current L1, and the L1 changes each new result brings: it counts them, and
    sends each to every subscription.

    It has a lock of its own, and any thread may call it. A caller that holds a lock of its own
    while it calls takes that lock first.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, L1Entry] = {}
        self._counts: dict[str, int] = {}
        self._subscriptions: set[L1Subscription] = set()
        self._manifest: object = _NO_MANIFEST

    def update(self, l1: Mapping[str, L1Entry], manifest: object) -> None:
        """Takes each new result, ``l1``, from ``manifest``, any object compared by identity. A
        sensor not in ``l1`` is forgotten. Every subscription is out of date from the first
        result under a manifest other than the one before."""
        with self._lock:
            for id, entry in l1.items():
                before = self._states.get(id)
                if before is None or (before.state, before.reason) != (entry.state, entry.reason):
                    if before is not None:  # A reload's new sensor is not counted.
                        self._counts[id] = self._counts.get(id, 0) + 1
                    for subscription in self._subscriptions:
                        subscription._put(L1Change(id, entry))
            if manifest is not self._manifest:
                if self._manifest is not _NO_MANIFEST:
                    for subscription in self._subscriptions:
                        subscription.outdated = True
                self._manifest = manifest
            self._states = dict(l1)
            self._counts = {id: self._counts.get(id, 0) for id in l1}

    def subscribe(self) -> L1Subscription:
        """Every L1 change from now on. The subscription's ``state`` leaves no gap before them."""
        with self._lock:

            def close() -> None:
                with self._lock:
                    self._subscriptions.discard(subscription)

            subscription = L1Subscription(dict(self._states), close)
            self._subscriptions.add(subscription)
            return subscription

    def counts(self) -> dict[str, int]:
        """The count of L1 changes of each sensor in the current result."""
        with self._lock:
            return dict(self._counts)
