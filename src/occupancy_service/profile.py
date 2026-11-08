"""Profile files: how L1 goes out over MQTT. The README documents the format."""

import json
import math
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from occupancy_service.l1 import L1Entry

_SHIPPED = Path(__file__).parent / "profiles"
_KEYS = {"topic", "payload", "retain", "qos", "repeat"}
_OPTIONAL_KEYS = {"clear_hold_s", "connection", "will_state", "l0_topic"}
_CONNECTIONS = ("shared", "per_sensor")
_STATES = ("occupied", "clear", "unknown")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_TOPIC_PLACEHOLDERS = {"archive_id", "sensor"}
_L0_TOPIC_PLACEHOLDERS = {"archive_id"}
_PAYLOAD_PLACEHOLDERS = {
    "occupied": {"sensor", "confidence"},
    "clear": {"sensor"},
    "unknown": {"sensor", "reason"},
}
# What a broker calls malformed in a topic: control characters and Unicode non-characters.
_MALFORMED = "\0-\x1f\x7f-\x9f\ufdd0-\ufdef" + "".join(
    chr(plane * 0x10000 + low) for plane in range(17) for low in (0xFFFE, 0xFFFF)
)
_NOT_IN_A_TOPIC = re.compile(f"[+#{_MALFORMED}]")
_NOT_IN_A_LEVEL = re.compile(f"[+#/{_MALFORMED}]")
_TOPIC_BYTES = 65535  # The most a topic can hold in MQTT.
_WILL = "connection lost"  # The reason a will gives, for a payload that holds one.

type Message = str | dict[str, Any]
type State = Literal["occupied", "clear", "unknown"]


class ProfileError(ValueError):
    """A profile that cannot be read, or that says something this service cannot do."""


def encode(message: Message) -> bytes:
    """What goes out: a table as JSON, and a string as text."""
    if isinstance(message, str):
        return message.encode(errors="replace")  # A hand-edited sensor id may not be valid text.
    return json.dumps(message, allow_nan=False).encode()


@dataclass(frozen=True)
class Profile:
    """One loaded profile, named after its file. ``repeat`` and ``clear_hold_s`` are in seconds. A
    ``repeat`` of 0 means no repeat, and a ``clear_hold_s`` of 0 means no hold. ``connection`` is
    ``shared`` or ``per_sensor``, and only ``per_sensor`` may name a ``will_state``. Only a
    ``shared`` profile may have an ``l0_topic``, and None means it publishes no L0."""

    name: str
    topic: str
    payload: Mapping[str, Message]
    retain: bool
    qos: int
    repeat: float
    clear_hold_s: float = 0
    connection: str = "shared"
    will_state: State | None = None
    l0_topic: str | None = None

    @property
    def needs_archive_id(self) -> bool:
        return "{archive_id}" in self.topic

    def topic_for(self, sensor: str, archive_id: str | None) -> str | None:
        """The topic of ``sensor``. None when a topic cannot carry its id or the archive's: a
        broker drops the client that publishes a wildcard, and a ``/`` would add a level."""
        values = {"sensor": sensor, "archive_id": archive_id or ""}
        for name in _placeholders(self.topic):
            if _NOT_IN_A_LEVEL.search(values[name]):
                return None
        return _publishable(_fill(self.topic, values))

    def l0_topic_for(self, archive_id: str | None) -> str | None:
        """The topic of L0. None with no ``l0_topic``, with no archive ``id``, or when the topic
        cannot carry the archive's ``id``."""
        if self.l0_topic is None or archive_id is None or _NOT_IN_A_LEVEL.search(archive_id):
            return None
        return _publishable(_fill(self.l0_topic, {"archive_id": archive_id}))

    def message_for(self, sensor: str, entry: L1Entry) -> Message:
        values: dict[str, Any] = {"sensor": sensor}
        if entry.state == "occupied":
            values["confidence"] = float(entry.confidence or 0)
        elif entry.state == "unknown":
            values["reason"] = entry.reason or ""
        payload = self.payload[entry.state]
        return _fill(payload, values) if isinstance(payload, str) else _render(payload, values)

    def will_for(self, sensor: str) -> Message | None:
        """The last will of ``sensor``'s connection: the ``will_state`` payload, with the reason
        ``connection lost``. None with no ``will_state``."""
        if self.will_state is None:
            return None
        return self.message_for(sensor, L1Entry(self.will_state, reason=_WILL))


def _publishable(topic: str) -> str | None:
    """``topic``, or None when MQTT does not allow it."""
    try:
        too_long = len(topic.encode()) > _TOPIC_BYTES
    except UnicodeEncodeError:
        return None
    return None if too_long or topic.startswith("$") else topic


def load_profile(name: str) -> Profile:
    """A shipped profile by its bare name, or the file at a path.

    Raises ``ProfileError`` for a file that cannot be read or that holds anything wrong, so a
    mistake stops the service at startup rather than going out to a controller.
    """
    is_path = "/" in name or name.endswith(".toml")
    path = Path(name) if is_path else _SHIPPED / f"{name}.toml"
    try:
        with path.open("rb") as file:
            table = tomllib.load(file)
    except OSError as error:
        raise ProfileError(f"cannot read the profile {name}: {error}") from error
    except ValueError as error:  # Includes tomllib's error, and bytes that are not UTF-8.
        raise ProfileError(f"{path} is not TOML: {error}") from error
    try:
        return _parse(path.stem, table)
    except ValueError as error:
        raise ProfileError(f"{path}: {error}") from error


def _parse(name: str, table: dict[str, Any]) -> Profile:
    for key in table.keys() - _KEYS - _OPTIONAL_KEYS:
        raise ValueError(f"unknown key {key}")
    for key in _KEYS - table.keys():
        raise ValueError(f"no {key}")
    connection = _parse_connection(table.get("connection", "shared"))
    payload = _parse_payload(table["payload"])
    return Profile(
        name=name,
        topic=_parse_topic(table["topic"]),
        payload=payload,
        retain=_parse_retain(table["retain"]),
        qos=_parse_qos(table["qos"]),
        repeat=_parse_repeat(table["repeat"]),
        clear_hold_s=_parse_clear_hold(table.get("clear_hold_s", 0)),
        connection=connection,
        will_state=_parse_will_state(table.get("will_state"), connection, payload),
        l0_topic=_parse_l0_topic(table.get("l0_topic"), connection),
    )


def _parse_connection(connection: Any) -> str:
    if connection not in _CONNECTIONS:
        raise ValueError('connection must be "shared" or "per_sensor"')
    return connection


def _parse_will_state(state: Any, connection: str, payload: Mapping[str, Message]) -> State | None:
    if state is None:
        return None
    if connection != "per_sensor":
        raise ValueError('will_state needs connection = "per_sensor"')
    if state not in _STATES:
        raise ValueError("will_state must be occupied, clear or unknown")
    if "confidence" in _placeholders(payload[state]):
        raise ValueError(f"will_state {state} has no {{confidence}} to fill its payload")
    return state


def _parse_l0_topic(topic: Any, connection: str) -> str | None:
    if topic is None:
        return None
    if connection != "shared":
        raise ValueError('l0_topic needs connection = "shared"')
    return _parse_topic(topic, "l0_topic", _L0_TOPIC_PLACEHOLDERS)


def _parse_topic(topic: Any, key: str = "topic", allowed: set[str] = _TOPIC_PLACEHOLDERS) -> str:
    if not isinstance(topic, str) or not topic:
        raise ValueError(f"{key} must be a non-empty string")
    if _NOT_IN_A_TOPIC.search(topic):
        raise ValueError(f"{key} {topic!r} holds a character no topic may publish to")
    for name in _placeholders(topic) - allowed:
        raise ValueError(f"{key} cannot hold {{{name}}}")
    return topic


def _parse_payload(payload: Any) -> dict[str, Message]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a table with one entry per state")
    for key in payload.keys() - set(_STATES):
        raise ValueError(f"unknown payload {key}")
    for state in _STATES:
        if state not in payload:
            raise ValueError(f"no payload for {state}")
        message = payload[state]
        if not isinstance(message, str | dict):
            raise ValueError(f"payload {state} must be a string or a table")
        for name in _placeholders(message) - _PAYLOAD_PLACEHOLDERS[state]:
            raise ValueError(f"payload {state} cannot hold {{{name}}}")
        if not _is_json(message):
            raise ValueError(f"payload {state} holds a value JSON does not have")
    return payload


def _parse_retain(retain: Any) -> bool:
    if not isinstance(retain, bool):
        raise ValueError("retain must be true or false")
    return retain


def _parse_qos(qos: Any) -> int:
    if type(qos) is not int or qos not in (0, 1, 2):
        raise ValueError("qos must be 0, 1 or 2")
    return qos


def _parse_repeat(repeat: Any) -> float:
    if type(repeat) not in (int, float) or not math.isfinite(repeat) or repeat < 0:
        raise ValueError("repeat must be a number of seconds from 0, where 0 means no repeat")
    return repeat


def _parse_clear_hold(seconds: Any) -> float:
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError("clear_hold_s must be a number of seconds from 0")
    return seconds


def _is_json(message: Message) -> bool:
    try:
        encode(message)
    except (TypeError, ValueError):
        return False
    return True


def _placeholders(value: Any) -> set[str]:
    """Every placeholder named anywhere in a string, a table or a list."""
    if isinstance(value, str):
        return set(_PLACEHOLDER.findall(value))
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return set().union(*map(_placeholders, value))
    return set()


def _fill(template: str, values: Mapping[str, Any]) -> str:
    return _PLACEHOLDER.sub(lambda match: str(values[match[1]]), template)


def _render(value: Any, values: Mapping[str, Any]) -> Any:
    """A table with its placeholders filled. One that is a whole string keeps its value's type,
    so ``"{confidence}"`` is a number in the JSON."""
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value)
        return values[whole[1]] if whole else _fill(value, values)
    if isinstance(value, dict):
        return {key: _render(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [_render(item, values) for item in value]
    return value
