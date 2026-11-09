"""What one active profile sends."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from occupancy_service.l1 import L1Entry, L1Result
from occupancy_service.profile import Message

_STOPPED = L1Entry("unknown", reason="service stopped")
_NOT_YET_HELD = L1Entry("unknown", reason="clear not yet held")  # A sensor with nothing sent yet.
_REMOVED = L1Entry("unknown", reason="sensor removed")  # Before its own connection ends.


@dataclass(frozen=True)
class LastMessage:
    """What a profile last sent for a sensor, and when: a table as a table, a string as a string."""

    message: Message
    time: datetime


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


def _captured_at(result: L1Result) -> datetime:
    """When the frame of ``result`` was captured. A result with no frame stands for now."""
    return result.frame.arrived_at if result.frame is not None else datetime.now(UTC)
