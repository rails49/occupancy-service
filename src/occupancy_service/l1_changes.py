"""Which L1 entries are L1 changes, and who hears of them."""

from __future__ import annotations

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
        self.queue: SimpleQueue[L1Change] = SimpleQueue()
        self.outdated = False
        self._close = close

    def get(self, timeout: float | None = None) -> L1Change | None:
        """The next change. None when there is none within ``timeout`` seconds."""
        try:
            return self.queue.get(timeout=timeout)
        except Empty:
            return None

    def close(self) -> None:
        self._close()
