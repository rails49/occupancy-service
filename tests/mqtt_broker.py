"""A real ``mosquitto`` for the tests, and a subscriber that records what it delivers."""

import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from paho.mqtt import client as paho
from paho.mqtt.enums import CallbackAPIVersion

_SYSTEM_PATHS = "/usr/sbin:/usr/local/sbin"  # Where packages put it, off many users' PATH.


def mosquitto() -> str | None:
    return shutil.which("mosquitto", path=f"{os.environ.get('PATH', '')}:{_SYSTEM_PATHS}")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Broker:
    """``mosquitto`` on a free port, keeping nothing across a restart."""

    def __init__(self, binary: str, folder: Path) -> None:
        self.binary = binary
        self.port = _free_port()
        self.config = folder / "mosquitto.conf"
        self.config.write_text(
            f"listener {self.port} 127.0.0.1\nallow_anonymous true\npersistence false\n"
        )
        self.log = folder / "mosquitto.log"
        self.process: subprocess.Popen[bytes] | None = None
        self.log_file: IO[bytes] | None = None

    def start(self) -> None:
        self.log_file = self.log.open("ab")
        self.process = subprocess.Popen(
            [self.binary, "-c", str(self.config)], stdout=self.log_file, stderr=self.log_file
        )
        deadline = time.monotonic() + 10
        while True:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.01)

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            self.process.wait(10)
            self.process = None
        if self.log_file is not None:
            self.log_file.close()
            self.log_file = None


@dataclass(frozen=True)
class Received:
    topic: str
    payload: bytes
    qos: int
    retain: bool
    at: float


class Subscriber:
    """Subscribed to every topic once it is constructed."""

    def __init__(self, port: int, qos: int = 0) -> None:
        self.messages: list[Received] = []
        subscribed = threading.Event()
        self.client = paho.Client(CallbackAPIVersion.VERSION2, protocol=paho.MQTTv311)
        self.client.on_message = self._on_message
        self.client.on_subscribe = lambda *_: subscribed.set()
        self.client.connect("127.0.0.1", port)
        self.client.subscribe("#", qos)
        self.client.loop_start()
        assert subscribed.wait(5)

    def close(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()

    def on(self, topic: str) -> list[Received]:
        return [message for message in list(self.messages) if message.topic == topic]

    def wait_for(self, found: Callable[[Received], bool], timeout: float = 5) -> Received:
        """The first message ``found`` accepts, from those already here or the next to come."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in list(self.messages):
                if found(message):
                    return message
            time.sleep(0.005)
        raise AssertionError(f"not within {timeout} s; have {self.messages[-5:]}")

    def _on_message(self, client: paho.Client, userdata: Any, message: paho.MQTTMessage) -> None:
        self.messages.append(
            Received(
                message.topic, bytes(message.payload), message.qos, message.retain, time.monotonic()
            )
        )
