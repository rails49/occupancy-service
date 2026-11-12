"""Open the loaded railroad's archive, from the railroad a topic on the broker names."""

import json
import logging
import os
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple, Protocol

from paho.mqtt import client as paho
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from occupancy_service.profile import Profile, ProfileError, RailroadTopic

_log = logging.getLogger(__name__)

PLACEHOLDER = "{railroad}"
_RETRY_EVERY = 1  # Seconds between tries to reach a lost broker.
_KEEPALIVE = 5
_TIMEOUT = 2.0  # Seconds connecting may take.


class Build(NamedTuple):
    """A railroad's name, and the stamp of its build."""

    name: str
    stamp: Any


def build_of(payload: bytes | bytearray, field: str, stamp: str) -> Build | None:
    """The build a JSON object names: the non-empty string in ``field``, and the value in
    ``stamp``. None for a message that names none, and for one with ``built: false``, which names
    a railroad nothing was built from."""
    try:
        message = json.loads(payload)
    except ValueError:  # Text that is not UTF-8 is a ValueError too.
        return None
    if not isinstance(message, dict) or message.get("built") is False:
        return None
    name = message.get(field)
    if not isinstance(name, str) or not name:
        return None
    return Build(name, message.get(stamp))


def archive_for(pattern: str, name: str) -> str | None:
    """``pattern`` with the railroad's name in it. None for a name that would leave the folder."""
    if "/" in name:
        return None
    return pattern.replace(PLACEHOLDER, name)


def railroad_source(profiles: Sequence[Profile]) -> RailroadTopic | None:
    """Where the active profiles read the railroad. None when none names it. Raises
    ``ProfileError`` when two name different topics."""
    sources = {profile.railroad for profile in profiles if profile.railroad is not None}
    if len(sources) > 1:
        raise ProfileError("two active profiles name different railroad topics")
    return next(iter(sources), None)


class Opener(Protocol):
    @property
    def archive(self) -> str | os.PathLike[str] | None: ...

    def open(self, archive: str | os.PathLike[str] | None) -> None: ...


@dataclass(frozen=True)
class RailroadSettings:
    """``archive`` is the path with ``{railroad}`` in it, and ``source`` the profile's railroad
    topic, on the broker at ``host`` and ``port``, with no authentication."""

    archive: str
    source: RailroadTopic
    host: str = "localhost"
    port: int = 1883


class RailroadFollower:
    """Follows ``topic`` on a connection of its own, and opens on ``loop`` the archive of each new
    build it names: a new name, or a new stamp on the same one. paho's thread does that. A lost
    connection changes nothing, and the broker hands the retained row over again on the next.
    With no ``{railroad}`` in the archive's path it only keeps the name."""

    def __init__(self, settings: RailroadSettings, loop: Opener) -> None:
        self._settings = settings
        self._loop = loop
        self._build: Build | None = None
        self._client = client = paho.Client(
            CallbackAPIVersion.VERSION2,
            client_id=f"occupancy-service-railroad-{secrets.token_hex(4)}",
            protocol=paho.MQTTv311,
        )
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.connect_timeout = _TIMEOUT
        client.reconnect_delay_set(_RETRY_EVERY, _RETRY_EVERY)

    def name(self) -> str | None:
        """The railroad's name. None before a row names one."""
        build = self._build
        return None if build is None else build.name

    def start(self) -> None:
        settings = self._settings
        self._client.connect_async(settings.host, settings.port, _KEEPALIVE)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()

    def take(self, payload: bytes | bytearray) -> None:
        """Opens the archive of the build ``payload`` names, if that is a new one. An archive
        that cannot be read is logged, and leaves the service with none."""
        settings = self._settings
        build = build_of(payload, settings.source.field, settings.source.stamp)
        if build is None or build == self._build:
            return
        self._build = build
        name = build.name
        if PLACEHOLDER not in settings.archive:
            return
        path = archive_for(settings.archive, name)
        try:
            self._loop.open(path)
        except Exception:  # What r49.load raises is its own to choose.
            _log.exception("cannot read the archive of railroad %r at %s", name, path)
            self._loop.open(None)
        if self._loop.archive is None:
            _log.info("railroad %r has no archive at %s", name, path)
        else:
            _log.info("railroad %r: opened the archive at %s", name, path)

    def _on_connect(
        self,
        client: paho.Client,
        userdata: Any,
        flags: paho.ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        if reason_code.is_failure:
            _log.warning("the broker refused the railroad connection: %s", reason_code)
            return
        client.subscribe(self._settings.source.topic, qos=1)

    def _on_message(self, client: paho.Client, userdata: Any, message: paho.MQTTMessage) -> None:
        self.take(message.payload)
