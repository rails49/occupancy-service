"""What one active profile sends, on a ``send`` that records it: no broker, threads or sleeps."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from occupancy_service.active_profile import ActiveProfile
from occupancy_service.capture import Frame
from occupancy_service.l1 import L1Entry, L1Result
from occupancy_service.l1_changes import L1Change
from occupancy_service.profile import Profile

CLEAR = L1Entry("clear")
OCCUPIED = L1Entry("occupied", confidence=0.93)
NO_FRAMES = L1Entry("unknown", reason="no frames")
STALLED = L1Entry("unknown", reason="scoring stalled")
START = datetime(2026, 11, 5, 12, tzinfo=UTC)


@dataclass
class Sent:
    """A ``send`` that records what it is given, and fails while ``up`` is false."""

    up: bool = True
    messages: list[tuple[str, str | None, str, bytes, int, bool]] = field(default_factory=list)
    refused: list[tuple[str, str | None, str, bytes, int, bool]] = field(default_factory=list)

    def __call__(
        self, sensor: str, archive_id: str | None, topic: str, body: bytes, qos: int, retain: bool
    ) -> bool:
        (self.messages if self.up else self.refused).append(
            (sensor, archive_id, topic, body, qos, retain)
        )
        return self.up

    def bodies(self, sensor: str = "a") -> list[str]:
        return [body.decode() for s, _, _, body, _, _ in self.messages if s == sensor]

    def clear(self) -> None:
        self.messages.clear()
        self.refused.clear()


def a_profile(
    repeat: float = 0,
    clear_hold_s: float = 0,
    topic: str = "t/{sensor}",
    unknown: str = "? {reason}",
) -> Profile:
    """A text profile, so each payload reads as itself."""
    return Profile(
        name="p",
        topic=topic,
        payload={"occupied": "ON", "clear": "OFF", "unknown": unknown},
        retain=True,
        qos=1,
        repeat=repeat,
        clear_hold_s=clear_hold_s,
    )


def result(
    l1: Mapping[str, L1Entry], seconds: float = 0, archive_id: str | None = "archive-1"
) -> L1Result:
    """A result whose frame was captured ``seconds`` after ``START``."""
    frame = Frame(b"", 1, START + timedelta(seconds=seconds))
    return L1Result(frame, dict(l1), {}, None, {}, archive_id)


def changes(**entries: L1Entry) -> list[L1Change]:
    return [L1Change(sensor, entry) for sensor, entry in entries.items()]


def active(profile: Profile) -> tuple[ActiveProfile, Sent]:
    sent = Sent()
    return ActiveProfile(profile, sent), sent


# What goes out, and how.


def test_fresh_sends_every_sensor_on_its_topic_with_the_profiles_qos_and_retain() -> None:
    profile, sent = active(a_profile())
    profile.fresh(result({"a": OCCUPIED, "b": NO_FRAMES}))
    assert sent.messages == [
        ("a", "archive-1", "t/a", b"ON", 1, True),
        ("b", "archive-1", "t/b", b"? no frames", 1, True),
    ]


def test_fresh_for_one_sensor_sends_only_that_sensor() -> None:
    profile, sent = active(a_profile())
    profile.fresh(result({"a": OCCUPIED, "b": CLEAR}), "b")
    assert sent.bodies("b") == ["OFF"]
    assert sent.bodies("a") == []


def test_a_change_sends_the_sensors_entry_in_the_newest_result_and_only_that_sensor() -> None:
    profile, sent = active(a_profile(repeat=60))
    profile.step([], result({"a": CLEAR, "b": CLEAR}), now=0)
    profile.step(changes(a=OCCUPIED), result({"a": STALLED, "b": CLEAR}), now=1)
    assert sent.messages == [("a", "archive-1", "t/a", b"? scoring stalled", 1, True)]


def test_a_sensor_that_changes_more_than_once_between_passes_sends_its_newest_entry_once() -> None:
    profile, sent = active(a_profile(repeat=60))
    profile.step([], result({"a": CLEAR}), now=0)
    profile.step(
        [L1Change("a", OCCUPIED), L1Change("a", CLEAR), L1Change("a", OCCUPIED)],
        result({"a": OCCUPIED}),
        now=1,
    )
    assert sent.bodies() == ["ON"]


def test_a_change_for_a_sensor_the_newest_result_no_longer_has_sends_nothing() -> None:
    profile, sent = active(a_profile(repeat=60))
    profile.step(changes(a=OCCUPIED), result({"b": CLEAR}), now=0)
    assert sent.messages == []


def test_stop_sends_unknown_for_every_sensor() -> None:
    profile, sent = active(a_profile())
    profile.stop(result({"a": OCCUPIED, "b": CLEAR}))
    assert (sent.bodies("a"), sent.bodies("b")) == (["? service stopped"], ["? service stopped"])


def test_removed_sends_sensor_removed_on_the_topic_of_the_archive_id_it_is_given() -> None:
    profile, sent = active(a_profile(topic="t/{archive_id}/{sensor}"))
    profile.removed("a", "archive-0")
    assert sent.messages == [("a", "archive-0", "t/archive-0/a", b"? sensor removed", 1, True)]


# The repeat.


def test_a_profile_with_no_repeat_is_never_due() -> None:
    profile, sent = active(a_profile(repeat=0))
    profile.step([], result({"a": CLEAR}), now=0)
    assert profile.next_due() == float("inf")
    profile.step([], result({"a": CLEAR}), now=1e9)
    assert sent.messages == []


def test_the_repeat_sends_every_sensor_on_the_same_beat() -> None:
    profile, sent = active(a_profile(repeat=1))
    profile.step([], result({"a": CLEAR, "b": OCCUPIED}), now=10)
    assert sent.messages == []
    assert profile.next_due() == 11
    profile.step([], result({"a": CLEAR, "b": OCCUPIED}), now=10.99)
    assert sent.messages == []
    profile.step([], result({"a": CLEAR, "b": OCCUPIED}), now=11.2)
    assert (sent.bodies("a"), sent.bodies("b")) == (["OFF"], ["ON"])
    assert profile.next_due() == 12


def test_a_repeat_a_whole_interval_behind_starts_a_new_beat() -> None:
    profile, sent = active(a_profile(repeat=1))
    profile.step([], result({"a": CLEAR}), now=10)
    profile.step([], result({"a": CLEAR}), now=13.5)
    assert sent.bodies() == ["OFF"]
    assert profile.next_due() == 14.5


def test_a_profile_that_repeats_sends_a_change_whose_payload_is_unchanged() -> None:
    profile, sent = active(a_profile(repeat=60, unknown="ON"))
    profile.fresh(result({"a": OCCUPIED}))
    profile.step(changes(a=NO_FRAMES), result({"a": NO_FRAMES}), now=0)
    assert sent.bodies() == ["ON", "ON"]


# A profile with repeat = 0.


def test_no_repeat_sends_a_change_only_where_its_payload_differs() -> None:
    profile, sent = active(a_profile(repeat=0, unknown="ON"))
    profile.fresh(result({"a": OCCUPIED}))
    profile.step(changes(a=NO_FRAMES), result({"a": NO_FRAMES}), now=0)
    profile.step(changes(a=STALLED), result({"a": STALLED}), now=1)
    assert sent.bodies() == ["ON"]
    profile.step(changes(a=CLEAR), result({"a": CLEAR}), now=2)
    profile.step(changes(a=OCCUPIED), result({"a": OCCUPIED}), now=3)
    assert sent.bodies() == ["ON", "OFF", "ON"]


def test_no_repeat_sends_everything_on_fresh_even_where_unchanged() -> None:
    profile, sent = active(a_profile(repeat=0))
    profile.fresh(result({"a": OCCUPIED}))
    profile.fresh(result({"a": OCCUPIED}))
    assert sent.bodies() == ["ON", "ON"]


def test_no_repeat_sends_stop_and_removed_only_where_the_payload_differs() -> None:
    profile, sent = active(a_profile(repeat=0, unknown="ON"))
    profile.fresh(result({"a": OCCUPIED, "b": CLEAR}))
    profile.removed("a", "archive-1")
    profile.stop(result({"b": CLEAR}))
    assert (sent.bodies("a"), sent.bodies("b")) == (["ON"], ["OFF", "ON"])


def test_no_repeat_does_not_count_a_send_that_failed() -> None:
    profile, sent = active(a_profile(repeat=0))
    profile.fresh(result({"a": OCCUPIED}))
    sent.up = False
    profile.step(changes(a=CLEAR), result({"a": CLEAR}), now=0)
    sent.up = True
    profile.step(changes(a=CLEAR), result({"a": CLEAR}), now=1)
    assert sent.bodies() == ["ON", "OFF"]


def test_no_repeat_sends_a_sensor_a_reload_adds_back_on_its_first_change() -> None:
    profile, sent = active(a_profile(repeat=0))
    profile.fresh(result({"a": OCCUPIED}))
    profile.step([], result({"b": CLEAR}), now=0)
    profile.step(changes(a=OCCUPIED), result({"a": OCCUPIED, "b": CLEAR}), now=1)
    assert sent.bodies() == ["ON", "ON"]


# A sensor or archive id the topic cannot carry.


def refusals(caplog: pytest.LogCaptureFixture, sensor: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if repr(sensor) in record.getMessage()]


def test_a_sensor_the_topic_cannot_carry_is_logged_once_and_never_sent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    profile, sent = active(a_profile(repeat=1))
    newest = result({"bad/id": CLEAR, "good": CLEAR})
    with caplog.at_level(logging.WARNING, "occupancy_service.active_profile"):
        profile.fresh(newest)
        profile.step(changes(**{"bad/id": OCCUPIED}), newest, now=0)
        profile.step([], newest, now=5)
        profile.stop(newest)
    assert {s for s, *_ in sent.messages} == {"good"}
    (refusal,) = refusals(caplog, "bad/id")
    assert "profile p" in refusal.getMessage()
    assert not profile.carries("bad/id", "archive-1")
    assert profile.carries("good", "archive-1")


def test_a_sensor_a_reload_removes_and_adds_back_is_logged_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    profile, _ = active(a_profile())
    with caplog.at_level(logging.WARNING, "occupancy_service.active_profile"):
        profile.fresh(result({"bad/id": CLEAR}))
        profile.step([], result({"good": CLEAR}), now=0)
        profile.step([], result({"bad/id": CLEAR}), now=1)
        profile.fresh(result({"bad/id": CLEAR}))
    assert len(refusals(caplog, "bad/id")) == 2


def test_with_no_archive_id_a_topic_that_needs_one_sends_nothing_and_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    profile, sent = active(a_profile(repeat=1, topic="t/{archive_id}/{sensor}"))
    newest = result({"a": CLEAR}, archive_id=None)
    with caplog.at_level(logging.WARNING, "occupancy_service.active_profile"):
        profile.fresh(newest)
        profile.step(changes(a=OCCUPIED), newest, now=0)
        profile.step([], newest, now=5)
        profile.stop(newest)
    assert sent.messages == []
    assert caplog.records == []
    assert not profile.carries("a", None)
    assert profile.carries("a", "archive-1")


def test_with_no_archive_id_a_topic_that_needs_none_sends() -> None:
    profile, sent = active(a_profile())
    profile.fresh(result({"a": CLEAR}, archive_id=None))
    assert sent.messages == [("a", None, "t/a", b"OFF", 1, True)]
    assert profile.carries("a", None)


# What it last sent.


def test_last_messages_hold_the_newest_sent_per_sensor() -> None:
    profile, _ = active(a_profile(repeat=60))
    before = datetime.now(UTC)
    profile.fresh(result({"a": CLEAR, "b": OCCUPIED}))
    profile.step(changes(a=OCCUPIED), result({"a": OCCUPIED, "b": OCCUPIED}), now=0)
    last = profile.last_messages()
    assert {sensor: sent.message for sensor, sent in last.items()} == {"a": "ON", "b": "ON"}
    assert last["a"].time >= before
    assert last["a"].time.utcoffset() is not None


def test_last_messages_keep_a_table_as_a_table() -> None:
    profile = Profile(
        name="p",
        topic="t/{sensor}",
        payload={"occupied": {"state": "occupied"}, "clear": {"state": "clear"}, "unknown": "?"},
        retain=False,
        qos=0,
        repeat=0,
    )
    active_profile = ActiveProfile(profile, Sent())
    active_profile.fresh(result({"a": CLEAR}))
    assert active_profile.last_messages()["a"].message == {"state": "clear"}


def test_last_messages_skip_what_failed_and_what_was_refused() -> None:
    profile, sent = active(a_profile())
    profile.fresh(result({"bad/id": CLEAR}))
    sent.up = False
    profile.fresh(result({"a": CLEAR}))
    assert profile.last_messages() == {}


def test_last_messages_forget_a_sensor_no_longer_in_the_newest_result() -> None:
    profile, _ = active(a_profile(repeat=60))
    profile.fresh(result({"a": CLEAR, "b": CLEAR}))
    profile.step([], result({"b": CLEAR}), now=0)
    assert set(profile.last_messages()) == {"b"}


# The clear hold.


def held(hold: float = 1, repeat: float = 60) -> tuple[ActiveProfile, Sent]:
    """A held profile that has sent ``a`` as occupied."""
    profile, sent = active(a_profile(repeat=repeat, clear_hold_s=hold))
    profile.fresh(result({"a": OCCUPIED}))
    sent.clear()
    return profile, sent


def test_a_clear_shorter_than_the_hold_never_goes_out() -> None:
    profile, sent = held(hold=1)
    profile.step(changes(a=CLEAR), result({"a": CLEAR}, 0), now=0)
    profile.step([], result({"a": CLEAR}, 0.5), now=0.5)
    profile.step(changes(a=OCCUPIED), result({"a": OCCUPIED}, 0.6), now=0.6)
    profile.step(changes(a=CLEAR), result({"a": CLEAR}, 0.7), now=0.7)
    profile.step([], result({"a": CLEAR}, 1.5), now=1.5)  # Past the end of the first one.
    assert "OFF" not in sent.bodies()


def test_a_clear_goes_out_once_on_the_first_result_captured_at_the_end_of_the_hold() -> None:
    profile, sent = held(hold=30)
    profile.step(changes(a=CLEAR), result({"a": CLEAR}, 0), now=0)
    assert sent.bodies() == ["ON"]  # What it last reported.
    profile.step([], result({"a": CLEAR}, 29.9), now=1)
    assert "OFF" not in sent.bodies()
    profile.step([], result({"a": CLEAR}, 30), now=2)
    profile.step([], result({"a": CLEAR}, 31), now=3)
    assert sent.bodies() == ["ON", "OFF"]


def test_until_then_the_repeat_and_fresh_report_what_was_last_sent() -> None:
    profile, sent = held(hold=30, repeat=1)
    profile.step(changes(a=CLEAR), result({"a": CLEAR}, 0), now=0)
    profile.step([], result({"a": CLEAR}, 1), now=5)
    profile.fresh(result({"a": CLEAR}, 2))
    assert sent.bodies() == ["ON", "ON", "ON"]
    assert profile.last_messages()["a"].message == "ON"


def test_a_break_in_clear_already_gone_from_the_newest_result_restarts_the_hold() -> None:
    profile, sent = held(hold=30)
    profile.step(changes(a=CLEAR), result({"a": CLEAR}, 0), now=0)
    profile.step([L1Change("a", OCCUPIED), L1Change("a", CLEAR)], result({"a": CLEAR}, 1), now=1)
    profile.step([], result({"a": CLEAR}, 30.5), now=2)
    assert "OFF" not in sent.bodies()
    profile.step([], result({"a": CLEAR}, 31), now=3)
    assert sent.bodies()[-1] == "OFF"


def test_a_break_in_clear_while_sends_fail_restarts_the_hold() -> None:
    profile, sent = held(hold=30)
    sent.up = False
    profile.step(changes(a=CLEAR), result({"a": CLEAR}, 0), now=0)
    profile.step(changes(a=OCCUPIED), result({"a": OCCUPIED}, 1), now=1)
    profile.step(changes(a=CLEAR), result({"a": CLEAR}, 2), now=2)
    profile.step([], result({"a": CLEAR}, 31), now=3)
    sent.up = True
    profile.fresh(result({"a": CLEAR}, 31))
    profile.step([], result({"a": CLEAR}, 31.5), now=4)
    assert sent.bodies() == ["ON"]
    profile.step([], result({"a": CLEAR}, 32), now=5)
    assert sent.bodies() == ["ON", "OFF"]


def test_unknown_goes_out_at_once_and_the_clear_after_it_waits_with_its_reason() -> None:
    profile, sent = held(hold=1)
    profile.step(changes(a=CLEAR), result({"a": CLEAR}, 0), now=0)
    profile.step(changes(a=NO_FRAMES), result({"a": NO_FRAMES}, 0.1), now=0.1)
    assert sent.bodies()[-1] == "? no frames"
    profile.step(changes(a=CLEAR), result({"a": CLEAR}, 0.2), now=0.2)
    profile.step([], result({"a": CLEAR}, 1.1), now=1.1)
    assert sent.bodies() == ["ON", "? no frames", "? no frames"]
    profile.step([], result({"a": CLEAR}, 1.2), now=1.2)
    assert sent.bodies()[-1] == "OFF"


def test_occupied_and_unknown_go_out_at_once_in_a_hold() -> None:
    profile, sent = active(a_profile(repeat=60, clear_hold_s=60))
    profile.fresh(result({"a": CLEAR}))
    profile.step(changes(a=OCCUPIED), result({"a": OCCUPIED}), now=0)
    profile.step(changes(a=NO_FRAMES), result({"a": NO_FRAMES}), now=1)
    profile.step(changes(a=STALLED), result({"a": STALLED}), now=2)
    assert sent.bodies()[1:] == ["ON", "? no frames", "? scoring stalled"]


def test_a_sensor_already_clear_says_unknown_until_the_hold_has_passed() -> None:
    profile, sent = active(a_profile(clear_hold_s=1))
    profile.fresh(result({"a": CLEAR}, 0))
    profile.step([], result({"a": CLEAR}, 0.9), now=0)
    assert sent.bodies() == ["? clear not yet held"]
    profile.step([], result({"a": CLEAR}, 1), now=1)
    assert sent.bodies() == ["? clear not yet held", "OFF"]


def test_a_sensor_a_reload_removes_is_forgotten_and_comes_back_unknown() -> None:
    profile, sent = held(hold=60)
    profile.step([], result({"b": CLEAR}), now=0)
    profile.step(changes(a=CLEAR), result({"a": CLEAR, "b": CLEAR}), now=1)
    assert sent.bodies() == ["? clear not yet held"]


def test_stop_during_a_hold_sends_unknown() -> None:
    profile, sent = held(hold=60)
    profile.step(changes(a=CLEAR), result({"a": CLEAR}), now=0)
    profile.stop(result({"a": CLEAR}))
    assert sent.bodies() == ["ON", "? service stopped"]


def test_a_hold_is_released_only_when_the_clear_that_ends_it_is_sent() -> None:
    """The connection drops just as the hold ends: the reconnect sends the held entry, and
    ``clear`` follows on the next pass."""
    profile, sent = held(hold=1)
    profile.step(changes(a=CLEAR), result({"a": CLEAR}, 0), now=0)
    sent.up = False
    profile.step([], result({"a": CLEAR}, 1), now=1)
    assert [body for *_, body, _, _ in sent.refused] == [b"OFF"]
    sent.up = True
    profile.fresh(result({"a": CLEAR}, 1.1))
    assert sent.bodies()[-1] == "ON"
    profile.step([], result({"a": CLEAR}, 1.2), now=2)
    assert sent.bodies()[-1] == "OFF"
    assert profile.last_messages()["a"].message == "OFF"


def test_with_no_hold_clear_goes_out_at_once() -> None:
    profile, sent = active(a_profile(repeat=60))
    profile.fresh(result({"a": OCCUPIED}))
    profile.step(changes(a=CLEAR), result({"a": CLEAR}), now=0)
    assert sent.bodies() == ["ON", "OFF"]
