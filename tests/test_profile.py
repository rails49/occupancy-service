"""Profile files: what a shipped profile sends, and what a bad file is refused for."""

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from occupancy_service.l1 import L1Entry
from occupancy_service.profile import (
    Profile,
    ProfileError,
    RailroadTopic,
    encode,
    load_profile,
)

OCCUPIED = L1Entry("occupied", confidence=0.93)
CLEAR = L1Entry("clear")
UNKNOWN = L1Entry("unknown", reason="no frames")

VALID = """
topic = "t/{sensor}"
retain = false
qos = 0
repeat = 1
[payload]
occupied = "ON"
clear = "OFF"
unknown = "?"
"""


def write(folder: Path, text: str, name: str = "mine.toml") -> Path:
    path = folder / name
    path.write_text(text)
    return path


def test_the_own_vocabulary_profile_says_what_the_issue_asks() -> None:
    profile = load_profile("occupancy")
    assert profile.name == "occupancy"
    assert (profile.retain, profile.qos, profile.repeat) == (False, 0, 1)
    assert profile.topic_for("C3.A", "archive-1") == "occupancy/archive-1/sensor/C3.A"
    assert profile.message_for("C3.A", OCCUPIED) == {"state": "occupied", "confidence": 0.93}
    assert profile.message_for("C3.A", CLEAR) == {"state": "clear"}
    assert profile.message_for("C3.A", UNKNOWN) == {"state": "unknown", "reason": "no frames"}


def test_the_control_profile_says_what_the_contract_asks() -> None:
    profile = load_profile("control")
    assert (profile.retain, profile.qos) == (False, 0)
    assert profile.topic_for("C3.A", "archive-1") == "tc49/layout/sensor"
    assert profile.message_for("C3.A", OCCUPIED) == {"addr": "C3.A", "occupancy": "occupied"}
    assert profile.message_for("C3.A", CLEAR) == {"addr": "C3.A", "occupancy": "clear"}
    assert profile.message_for("C3.A", UNKNOWN) == {
        "addr": "C3.A",
        "occupancy": "unknown",
        "reason": "no frames",
    }


def test_only_the_own_vocabulary_profile_has_an_l0_topic() -> None:
    assert load_profile("occupancy").l0_topic_for("archive-1") == "occupancy/archive-1/l0"
    for name in ("control", "jmri", "rocrail"):
        assert load_profile(name).l0_topic is None
        assert load_profile(name).l0_topic_for("archive-1") is None


def test_an_l0_topic_has_no_topic_with_an_archive_id_it_cannot_carry(
    tmp_path: Path,
) -> None:
    profile = load_profile("occupancy")
    assert profile.l0_topic_for("a/b") is None
    assert profile.l0_topic_for("a+") is None
    fixed = load_profile(str(write(tmp_path, 'l0_topic = "detections"\n' + VALID)))
    assert fixed.l0_topic_for("archive-1") == "detections"


@pytest.mark.parametrize(
    ("line", "complaint"),
    [
        ('l0_topic = ""', "l0_topic"),
        ("l0_topic = 1", "l0_topic"),
        ('l0_topic = "l0/{sensor}"', "sensor"),
        ('l0_topic = "l0/#"', "l0_topic"),
        ('l0_topic = "$SYS/l0"', "l0_topic.*\\$"),
        (f'l0_topic = "{"x" * 65536}"', "l0_topic.*65535"),
        ('l0_topic = "l0/{archive-id}"', "l0_topic.*archive-id"),
    ],
)
def test_refuses_an_l0_topic_it_cannot_use(tmp_path: Path, line: str, complaint: str) -> None:
    path = write(tmp_path, VALID.replace("[payload]", f"{line}\n[payload]"))
    with pytest.raises(ProfileError, match=complaint):
        load_profile(str(path))


def test_only_the_control_profile_names_the_railroad() -> None:
    control = load_profile("control")
    assert control.railroad == RailroadTopic("tc49/layout/state/railroad", "name", "at")
    for name in ("occupancy", "jmri", "rocrail"):
        assert load_profile(name).railroad is None


T, F, S = 'railroad_topic = "r"', 'railroad_field = "name"', 'railroad_stamp = "at"'


@pytest.mark.parametrize(
    ("lines", "complaint"),
    [
        (T, "go together"),
        (f"{T}\n{F}", "go together"),
        (f"{F}\n{S}", "go together"),
        (f'railroad_topic = "r/#"\n{F}\n{S}', "railroad_topic"),
        (f'railroad_topic = "r/{{sensor}}"\n{F}\n{S}', "sensor"),
        (f'{T}\nrailroad_field = ""\n{S}', "railroad_field"),
        (f"{T}\nrailroad_field = 1\n{S}", "railroad_field"),
        (f'{T}\n{F}\nrailroad_stamp = ""', "railroad_stamp"),
    ],
)
def test_refuses_a_railroad_topic_it_cannot_use(tmp_path: Path, lines: str, complaint: str) -> None:
    path = write(tmp_path, VALID.replace("[payload]", f"{lines}\n[payload]"))
    with pytest.raises(ProfileError, match=complaint):
        load_profile(str(path))


def test_the_control_profile_repeats_within_the_second_its_contract_allows() -> None:
    assert load_profile("control").repeat < 1


def test_only_the_control_profile_holds_clear() -> None:
    assert load_profile("control").clear_hold_s == 1
    assert load_profile("occupancy").clear_hold_s == 0


@pytest.mark.parametrize(
    ("line", "seconds"), [("", 0), ("clear_hold_s = 0", 0), ("clear_hold_s = 0.25", 0.25)]
)
def test_a_profile_may_set_a_hold_of_any_time_from_0(
    tmp_path: Path, line: str, seconds: float
) -> None:
    path = write(tmp_path, VALID.replace("[payload]", f"{line}\n[payload]"))
    assert load_profile(str(path)).clear_hold_s == seconds


def test_a_path_loads_a_profile_named_after_its_file(tmp_path: Path) -> None:
    profile = load_profile(str(write(tmp_path, VALID)))
    assert profile.name == "mine"
    assert profile.topic_for("s1", "archive-1") == "t/s1"


def test_a_string_is_sent_as_text_and_a_table_as_json(tmp_path: Path) -> None:
    text: Profile = load_profile(str(write(tmp_path, VALID)))
    assert encode(text.message_for("s1", CLEAR)) == b"OFF"
    table = load_profile("occupancy").message_for("s1", OCCUPIED)
    assert encode(table) == b'{"state": "occupied", "confidence": 0.93}'


def test_a_placeholder_alone_keeps_a_number_and_inside_text_becomes_text(tmp_path: Path) -> None:
    profile = load_profile(
        str(
            write(
                tmp_path,
                """
topic = "t"
retain = false
qos = 0
repeat = 1
[payload]
clear = "{sensor} is clear"
unknown = "{sensor}: {reason}"
[payload.occupied]
number = "{confidence}"
text = "at {confidence}"
nested = { list = ["{sensor}", 1] }
""",
            )
        )
    )
    assert profile.message_for("s1", OCCUPIED) == {
        "number": 0.93,
        "text": "at 0.93",
        "nested": {"list": ["s1", 1]},
    }
    assert profile.message_for("s1", UNKNOWN) == "s1: no frames"


def test_a_payload_may_hold_json_text_and_a_topic_may_fill_up_to_its_limit(
    tmp_path: Path,
) -> None:
    text = VALID.replace("t/{sensor}", "x" * 65535 + "{archive_id}{sensor}")
    text = text.replace('"ON"', '\'{"state": "on", "for": "{sensor}"}\'')
    profile = load_profile(str(write(tmp_path, text)))
    assert profile.message_for("s1", OCCUPIED) == '{"state": "on", "for": "s1"}'
    assert profile.topic_for("s1", "archive-1") is None


@pytest.mark.parametrize(
    "sensor",
    [
        "a+b",
        "a#",
        "a/b",
        "a\0b",
        "a\tb",
        "a\x7fb",
        "a\x9fb",
        "a\ufdd0b",
        "a\ufdefb",
        "a\ufffeb",
        "a\U0010ffffb",
        "\ud800",
        "x" * 70000,
    ],
    ids=repr,
)
def test_a_topic_cannot_carry_this_sensor_id(sensor: str) -> None:
    assert load_profile("occupancy").topic_for(sensor, "archive-1") is None


@pytest.mark.parametrize(
    "sensor", ["C3.A", "gleis é", "a\xa0b", "a\ufffdb", "a\U0001f682b"], ids=repr
)
def test_a_topic_carries_this_sensor_id(sensor: str) -> None:
    topic = load_profile("occupancy").topic_for(sensor, "archive-1")
    assert topic == f"occupancy/archive-1/sensor/{sensor}"


def test_refuses_a_topic_with_a_character_a_broker_calls_malformed(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="no topic may publish to"):
        load_profile(str(write(tmp_path, VALID.replace("t/{sensor}", "t\\t/{sensor}"))))


def test_a_topic_that_does_not_hold_the_id_carries_any_sensor() -> None:
    assert load_profile("control").topic_for("a+b/#", "archive-1") == "tc49/layout/sensor"


def test_a_topic_cannot_carry_this_archive_id() -> None:
    assert load_profile("occupancy").topic_for("s1", "a+b") is None


def test_a_topic_cannot_start_a_system_topic(tmp_path: Path) -> None:
    profile = load_profile(str(write(tmp_path, VALID.replace("t/{sensor}", "{sensor}/t"))))
    assert profile.topic_for("$SYS", "archive-1") is None


def test_a_sensor_id_with_a_lone_surrogate_still_goes_out_as_text(tmp_path: Path) -> None:
    profile = load_profile(
        str(write(tmp_path, VALID.replace("t/{sensor}", "t").replace('"OFF"', '"{sensor}"')))
    )
    assert encode(profile.message_for("\ud800", CLEAR)) == b"?"


@pytest.mark.parametrize(
    ("change", "complaint"),
    [
        (lambda text: text.replace('topic = "t/{sensor}"\n', ""), "topic"),
        (lambda text: text.replace("retain = false\n", ""), "retain"),
        (lambda text: text.replace("qos = 0\n", ""), "qos"),
        (lambda text: text.replace("repeat = 1\n", ""), "repeat"),
        (lambda text: text.replace('clear = "OFF"\n', ""), "clear"),
        (lambda text: text.replace("[payload]", "surprise = 1\n[payload]"), "surprise"),
        (lambda text: text + 'dazed = "x"\n', "dazed"),
        (lambda text: text.replace("retain = false", 'retain = "no"'), "retain"),
        (lambda text: text.replace("qos = 0", "qos = 3"), "qos"),
        (lambda text: text.replace("qos = 0", "qos = true"), "qos"),
        (lambda text: text.replace("repeat = 1", "repeat = -1"), "repeat"),
        (lambda text: text.replace("repeat = 1", "repeat = nan"), "repeat"),
        (lambda text: text.replace("repeat = 1", "repeat = true"), "repeat"),
        (lambda text: text.replace("[payload]", "clear_hold_s = -1\n[payload]"), "clear_hold_s"),
        (lambda text: text.replace("[payload]", "clear_hold_s = true\n[payload]"), "clear_hold_s"),
        (lambda text: text.replace("[payload]", 'clear_hold_s = "1"\n[payload]'), "clear_hold_s"),
        (lambda text: text.replace("[payload]", "clear_hold_s = nan\n[payload]"), "clear_hold_s"),
        (lambda text: text.replace("[payload]", "clear_hold_s = inf\n[payload]"), "clear_hold_s"),
        (lambda text: text.replace("t/{sensor}", "t/+/{sensor}"), "topic"),
        (lambda text: text.replace("t/{sensor}", "t/#"), "topic"),
        (lambda text: text.replace("t/{sensor}", ""), "topic"),
        (lambda text: text.replace("t/{sensor}", "t/{reason}"), "reason"),
        (lambda text: text.replace('"OFF"', '"{confidence}"'), "confidence"),
        (lambda text: text.replace('"ON"', '"{reason}"'), "reason"),
        (lambda text: text.replace('"ON"', '"{sensro}"'), "sensro"),
        (lambda text: text.replace('"ON"', "1"), "occupied"),
        (lambda text: text.replace('"ON"', "{ when = 1979-05-27 }"), "occupied"),
        (lambda text: text.replace('"ON"', "{ number = nan }"), "occupied"),
        (lambda text: "not toml", "toml"),
        (lambda text: text.replace("t/{sensor}", "$SYS/{sensor}"), "topic.*\\$"),
        (lambda text: text.replace("t/{sensor}", "x" * 65536 + "/{sensor}"), "topic.*65535"),
        (lambda text: text.replace("t/{sensor}", "t/{archive-id}"), "topic.*archive-id"),
        (lambda text: text.replace("t/{sensor}", "t/{ sensor }"), "topic.*{ sensor }"),
        (lambda text: text.replace('"OFF"', '"{ sensor } is clear"'), "clear.*{ sensor }"),
        (lambda text: text.replace('"ON"', '{ a = ["{archive-id}"] }'), "occupied.*archive-id"),
        (lambda text: text.replace('"ON"', '{ "{sensor}" = 1 }'), "occupied.*key.*{sensor}"),
        (lambda text: text.replace('"ON"', '{ "{ sensor }" = 1 }'), "occupied.*{ sensor }"),
        (lambda text: text.replace('"ON"', '{ a = { "{sensor}" = 1 } }'), "occupied.*{sensor}"),
    ],
)
def test_refuses_a_bad_profile_and_says_why(tmp_path: Path, change: Any, complaint: str) -> None:
    path = write(tmp_path, change(VALID))
    with pytest.raises(ProfileError, match=complaint) as caught:
        load_profile(str(path))
    assert str(path) in str(caught.value)


def test_the_readme_shows_the_shipped_profiles_as_they_are() -> None:
    root = Path(__file__).parent.parent
    readme = (root / "README.md").read_text()
    examples = [tomllib.loads(block) for block in re.findall(r"```toml\n(.*?)```", readme, re.S)]
    shipped = root / "src" / "occupancy_service" / "profiles"
    for name in ("occupancy", "control", "jmri"):
        assert tomllib.loads((shipped / f"{name}.toml").read_text()) in examples


def test_refuses_a_profile_that_does_not_exist(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="nothing-here"):
        load_profile("nothing-here")
    with pytest.raises(ProfileError, match="gone.toml"):
        load_profile(str(tmp_path / "gone.toml"))


@pytest.mark.parametrize(
    "line", ['connection = "per_sensor"', 'connection = "shared"', 'will_state = "unknown"']
)
def test_refuses_a_connection_or_a_will_state_as_an_unknown_key(tmp_path: Path, line: str) -> None:
    """Every profile sends on the one shared connection, and none has a last will."""
    path = write(tmp_path, VALID.replace("[payload]", f"{line}\n[payload]"))
    key = line.split(" ")[0]
    with pytest.raises(ProfileError, match=f"unknown key {key}"):
        load_profile(str(path))


def test_a_repeat_of_0_means_no_repeat(tmp_path: Path) -> None:
    assert load_profile(str(write(tmp_path, VALID.replace("repeat = 1", "repeat = 0")))).repeat == 0


def test_the_jmri_profile_says_what_jmri_reads() -> None:
    profile = load_profile("jmri")
    assert (profile.retain, profile.qos, profile.repeat) == (True, 0, 1)
    assert profile.topic_for("C3.A", "archive-1") == "track/sensor/C3.A"
    assert profile.message_for("C3.A", OCCUPIED) == "ACTIVE"
    assert profile.message_for("C3.A", CLEAR) == "INACTIVE"
    assert profile.message_for("C3.A", UNKNOWN) == "ACTIVE"
    assert profile.clear_hold_s == 0


def test_the_rocrail_profile_says_what_rocrail_reads() -> None:
    profile = load_profile("rocrail")
    assert (profile.retain, profile.qos, profile.repeat) == (False, 1, 0)
    assert profile.topic_for("C3.A", "archive-1") == "rocrail/service/field"
    assert profile.message_for("C3.A", OCCUPIED) == '<fb id="C3.A" state="true"/>'
    assert profile.message_for("C3.A", CLEAR) == '<fb id="C3.A" state="false"/>'
    assert profile.message_for("C3.A", UNKNOWN) == '<fb id="C3.A" state="true"/>'
    assert profile.clear_hold_s == 0
