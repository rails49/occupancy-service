"""Pause the L1 loop while control's track power reads ``off``."""

import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Protocol

from paho.mqtt import client as paho
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

_log = logging.getLogger(__name__)

_RETRY_EVERY = 1  # Seconds between tries to reach a lost broker.
_KEEPALIVE = 5
_TIMEOUT = 2.0  # Seconds connecting may take.


def is_off(payload: bytes | bytearray) -> bool:
    """Whether a message in control's form, ``{"power": "on" | "draining" | "off", ...}``, reads
    ``off``. One that cannot be read does not."""
    try:
        message = json.loads(payload)
    except ValueError:  # Text that is not UTF-8 is a ValueError too.
        return False
    return isinstance(message, dict) and message.get("power") == "off"


class Pausable(Protocol):
    def pause(self, paused: bool) -> None: ...


@dataclass(frozen=True)
class PowerSettings:
    """The topic of control's track power, and the broker, with no authentication."""

    topic: str
    host: str = "localhost"
    port: int = 1883


class PowerGate:
    """Follows ``topic`` on a connection of its own, and pauses ``loop`` while the newest message
    there reads ``off``. Any other message resumes it, and so does a lost connection. paho's
    thread keeps the connection up and calls ``pause``."""

    def __init__(self, settings: PowerSettings, loop: Pausable) -> None:
        self._settings = settings
        self._loop = loop
        self._off = False
        self._client = client = paho.Client(
            CallbackAPIVersion.VERSION2,
            client_id=f"occupancy-service-power-{secrets.token_hex(4)}",
            protocol=paho.MQTTv311,
        )
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.connect_timeout = _TIMEOUT
        client.reconnect_delay_set(_RETRY_EVERY, _RETRY_EVERY)

    def start(self) -> None:
        settings = self._settings
        self._client.connect_async(settings.host, settings.port, _KEEPALIVE)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()

    def _set(self, off: bool) -> None:
        if off != self._off:
            self._off = off
            _log.info("scoring paused: track power off" if off else "scoring resumed")
            self._loop.pause(off)

    def _on_connect(
        self,
        client: paho.Client,
        userdata: Any,
        flags: paho.ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        if reason_code.is_failure:
            _log.warning("the broker refused the power connection: %s", reason_code)
            return
        client.subscribe(self._settings.topic, qos=1)

    def _on_message(self, client: paho.Client, userdata: Any, message: paho.MQTTMessage) -> None:
        self._set(is_off(message.payload))

    def _on_disconnect(
        self,
        client: paho.Client,
        userdata: Any,
        flags: paho.DisconnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        self._set(False)
