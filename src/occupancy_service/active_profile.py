"""What one active profile sends."""

import logging
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from occupancy_service.l1 import L1Entry, L1Result
from occupancy_service.l1_changes import L1Change
from occupancy_service.profile import Message, Profile, encode

_log = logging.getLogger(__name__)

_STOPPED = L1Entry("unknown", reason="service stopped")
_NOT_YET_HELD = L1Entry("unknown", reason="clear not yet held")  # A sensor with nothing sent yet.
_REMOVED = L1Entry("unknown", reason="sensor removed")  # Before its own connection ends.

type Send = Callable[[str, str | None, str, bytes, int, bool], bool]
"""``send(sensor, archive_id, topic, body, qos, retain)``: whether the message went to paho. False
when the connection that carries the sensor is down, or paho refused the message."""


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

    def ends(self, sensor: str, entry: L1Entry, captured_at: datetime) -> bool:
        """Whether a result captured at ``captured_at`` ends the hold on ``sensor``. The caller has
        passed every change up to that result to ``report``, or a break could go unseen."""
        held = self._sensors.get(sensor)
        if held is None or held.clear_since is None or entry.state != "clear":
            return False
        return (captured_at - held.clear_since).total_seconds() >= self._seconds

    def release(self, sensor: str, entry: L1Entry) -> None:
        """Ends the hold on ``sensor``, once the ``clear`` that ``ends`` allowed has gone out."""
        self._sensors[sensor] = _HeldSensor(entry)

    def forget_all_but(self, sensors: Mapping[str, L1Entry]) -> None:
        self._sensors = {id: held for id, held in self._sensors.items() if id in sensors}


def _captured_at(result: L1Result) -> datetime:
    """When the frame of ``result`` was captured. A result with no frame stands for now."""
    return result.frame.arrived_at if result.frame is not None else datetime.now(UTC)


class ActiveProfile:
    """What one active profile sends, and what it has sent: its clear hold, the payloads a profile
    with ``repeat = 0`` has sent, the sensors its topic cannot carry, and when it next repeats.

    It sends through ``send``, and counts a message as sent only when ``send`` says so. One thread
    calls everything but ``last_messages``, which any thread may call.
    """

    def __init__(self, profile: Profile, send: Send) -> None:
        self.profile = profile
        self._send_to = send
        self._hold = _ClearHold(profile.clear_hold_s) if profile.clear_hold_s > 0 else None
        self._sent: dict[str, tuple[str, bytes]] = {}  # The last topic and payload, with no repeat.
        self._refused: set[str] = set()
        self._due: float | None = None  # Set on the first step, with a repeat.
        self._lock = threading.Lock()
        self._last: dict[str, LastMessage] = {}

    def step(self, changes: Sequence[L1Change], newest: L1Result, now: float) -> None:
        """One pass: ``changes`` are those up to ``newest`` at least, in order, and ``now`` is
        ``time.monotonic()``. Each changed sensor sends its entry in ``newest`` once, then each
        hold ``newest`` ends releases, then the repeat goes out if it is due."""
        self._forget_all_but(newest.l1)
        captured_at = _captured_at(newest)
        changed: dict[str, None] = {}
        for change in changes:
            if self._hold is not None:
                # A break in ``clear`` restarts a hold even when ``newest`` has already left it.
                self._hold.report(change.sensor, change.entry, captured_at)
            if change.sensor in newest.l1:  # Not when a reload has just removed it.
                changed[change.sensor] = None
        for sensor in changed:
            self._report(sensor, newest.l1[sensor], newest)
        if self._hold is not None:
            for sensor, entry in newest.l1.items():
                if self._hold.ends(sensor, entry, captured_at):
                    if self._send(sensor, entry, newest.archive_id):
                        self._hold.release(sensor, entry)
        repeat = self.profile.repeat
        if repeat > 0:
            if self._due is None:
                self._due = now + repeat
            elif self._due <= now:
                for sensor, entry in newest.l1.items():
                    self._report(sensor, entry, newest)
                # On the same beat, unless it has fallen a whole interval behind.
                next_at = self._due + repeat
                self._due = next_at if next_at > now else now + repeat

    def fresh(self, newest: L1Result, sensor: str | None = None) -> None:
        """What a new connection carries: every sensor in ``newest``, or only ``sensor``, sent
        even where the payload is the one last sent."""
        self._forget_all_but(newest.l1)
        for id, entry in newest.l1.items():
            if sensor is None or id == sensor:
                self._report(id, entry, newest, fresh=True)

    def stop(self, newest: L1Result) -> None:
        """``unknown`` with the reason ``service stopped`` for every sensor in ``newest``."""
        self._forget_all_but(newest.l1)
        for sensor in newest.l1:
            self._report(sensor, _STOPPED, newest)

    def removed(self, sensor: str, archive_id: str | None) -> None:
        """``unknown`` with the reason ``sensor removed`` on ``sensor``'s topic under
        ``archive_id``, before the connection that carries it there closes."""
        self._send(sensor, _REMOVED, archive_id)

    def carries(self, sensor: str, archive_id: str | None) -> bool:
        """Whether this profile has a topic for ``sensor`` under ``archive_id``."""
        return self._topic(sensor, archive_id) is not None

    def next_due(self) -> float:
        """The ``time.monotonic()`` of the next repeat: infinity with none, or none yet set."""
        return math.inf if self._due is None else self._due

    def last_messages(self) -> dict[str, LastMessage]:
        """Sensor id to what this profile last sent for it."""
        with self._lock:
            return dict(self._last)

    def _report(self, sensor: str, entry: L1Entry, newest: L1Result, fresh: bool = False) -> None:
        """Sends ``entry``, or while a hold is on ``sensor`` what this profile last reported."""
        if self._hold is not None:
            entry = self._hold.report(sensor, entry, _captured_at(newest))
        self._send(sensor, entry, newest.archive_id, fresh)

    def _send(
        self, sensor: str, entry: L1Entry, archive_id: str | None, fresh: bool = False
    ) -> bool:
        """Whether ``entry`` is on the topic now: sent, or with no repeat, unless ``fresh``, the
        payload last sent there."""
        topic = self._topic(sensor, archive_id)
        if topic is None:
            if not (archive_id is None and self.profile.needs_archive_id):
                self._refuse(sensor)
            return False
        profile = self.profile
        message = profile.message_for(sensor, entry)
        body = encode(message)
        if profile.repeat == 0 and not fresh and self._sent.get(sensor) == (topic, body):
            return True
        if not self._send_to(sensor, archive_id, topic, body, profile.qos, profile.retain):
            return False
        if profile.repeat == 0:
            self._sent[sensor] = (topic, body)
        with self._lock:
            self._last[sensor] = LastMessage(message, datetime.now(UTC))
        return True

    def _topic(self, sensor: str, archive_id: str | None) -> str | None:
        """``sensor``'s topic: None while the archive has no ``id`` and the topic needs one, or
        when the topic cannot carry the sensor's id or the archive's."""
        if archive_id is None and self.profile.needs_archive_id:
            return None
        return self.profile.topic_for(sensor, archive_id)

    def _refuse(self, sensor: str) -> None:
        if sensor not in self._refused:
            self._refused.add(sensor)
            _log.warning(
                "profile %s cannot put sensor %r in a topic, and will not report it",
                self.profile.name,
                sensor,
            )

    def _forget_all_but(self, sensors: Mapping[str, L1Entry]) -> None:
        """Forgets each sensor not in ``sensors``, so one a reload adds back starts afresh."""
        self._sent = {id: sent for id, sent in self._sent.items() if id in sensors}
        self._refused = {id for id in self._refused if id in sensors}
        with self._lock:
            self._last = {id: last for id, last in self._last.items() if id in sensors}
        if self._hold is not None:
            self._hold.forget_all_but(sensors)
