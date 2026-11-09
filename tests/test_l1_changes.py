"""What counts as an L1 change, and who hears of it, with no loop and no thread."""

from occupancy_service.l1 import L1Entry
from occupancy_service.l1_changes import L1Change, L1Changes, L1Subscription

OCCUPIED = L1Entry("occupied", confidence=0.9)
CLEAR = L1Entry("clear")
NO_FRAMES = L1Entry("unknown", reason="no frames")
STALLED = L1Entry("unknown", reason="scoring stalled")
MANIFEST = object()


def changes_after(l1: dict[str, L1Entry]) -> L1Changes:
    changes = L1Changes()
    changes.update(l1, MANIFEST)
    return changes


def drained(subscription: L1Subscription) -> list[L1Change]:
    heard: list[L1Change] = []
    while (change := subscription.get(0)) is not None:
        heard.append(change)
    return heard


def test_a_subscriber_gets_the_state_at_subscribing_and_then_each_change() -> None:
    changes = changes_after({"s1": OCCUPIED})
    subscription = changes.subscribe()
    assert subscription.state == {"s1": OCCUPIED}

    changes.update({"s1": CLEAR}, MANIFEST)
    changes.update({"s1": NO_FRAMES}, MANIFEST)

    assert drained(subscription) == [L1Change("s1", CLEAR), L1Change("s1", NO_FRAMES)]
    assert changes.counts() == {"s1": 2}


def test_a_new_confidence_is_not_a_change() -> None:
    changes = changes_after({"s1": NO_FRAMES})
    changes.update({"s1": OCCUPIED}, MANIFEST)
    subscription = changes.subscribe()

    changes.update({"s1": L1Entry("occupied", confidence=0.8)}, MANIFEST)

    assert drained(subscription) == []
    assert changes.counts() == {"s1": 1}  # Its first result; not this one.
    assert changes.subscribe().state["s1"].confidence == 0.8


def test_a_new_reason_is_a_change() -> None:
    changes = changes_after({"s1": NO_FRAMES})
    subscription = changes.subscribe()

    changes.update({"s1": STALLED}, MANIFEST)

    assert drained(subscription) == [L1Change("s1", STALLED)]
    assert changes.counts() == {"s1": 1}


def test_a_closed_subscription_gets_nothing_more() -> None:
    changes = changes_after({"s1": OCCUPIED})
    subscription = changes.subscribe()
    subscription.close()

    changes.update({"s1": NO_FRAMES}, MANIFEST)

    assert subscription.get(0) is None
    assert subscription.backlog() == 0


def test_the_backlog_is_the_count_of_changes_not_yet_read() -> None:
    changes = changes_after({"s1": OCCUPIED})
    subscription = changes.subscribe()
    changes.update({"s1": CLEAR}, MANIFEST)
    changes.update({"s1": NO_FRAMES}, MANIFEST)
    assert subscription.backlog() == 2
    subscription.get(0)
    assert subscription.backlog() == 1


def test_a_reload_tells_a_subscriber_of_a_new_sensor_without_counting_a_change() -> None:
    changes = changes_after({"kept": NO_FRAMES, "old": NO_FRAMES})
    changes.update({"kept": OCCUPIED, "old": OCCUPIED}, MANIFEST)
    subscription = changes.subscribe()

    changes.update({"kept": OCCUPIED, "new": OCCUPIED}, object())

    assert drained(subscription) == [L1Change("new", OCCUPIED)]
    assert changes.counts() == {"kept": 1, "new": 0}


def test_a_subscription_is_outdated_from_the_first_result_under_a_new_manifest() -> None:
    changes = changes_after({"s1": OCCUPIED})
    subscription = changes.subscribe()
    changes.update({"s1": CLEAR}, MANIFEST)
    assert not subscription.outdated

    changes.update({"s1": CLEAR, "s2": CLEAR}, object())

    assert subscription.outdated
    assert not changes.subscribe().outdated


def test_a_subscription_goes_on_after_it_is_outdated() -> None:
    """The publisher keeps one subscription across a reload, and reads each new sensor from it."""
    changes = changes_after({"s1": OCCUPIED})
    subscription = changes.subscribe()
    reloaded = object()
    changes.update({"s1": OCCUPIED, "s2": OCCUPIED}, reloaded)
    changes.update({"s1": CLEAR, "s2": OCCUPIED}, reloaded)

    assert subscription.outdated
    assert drained(subscription) == [L1Change("s2", OCCUPIED), L1Change("s1", CLEAR)]


def test_a_sensor_that_stays_gets_no_unknown_from_a_reload() -> None:
    changes = changes_after({"kept": OCCUPIED, "old": OCCUPIED})
    subscription = changes.subscribe()

    changes.update({"kept": OCCUPIED, "new": OCCUPIED}, object())

    assert [change.sensor for change in drained(subscription)] == ["new"]
    assert subscription.state["kept"] == OCCUPIED
