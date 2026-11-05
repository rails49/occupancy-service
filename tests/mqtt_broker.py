"""A real ``mosquitto`` for the tests, and a subscriber that records what it delivers."""

import os
import shutil
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

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
    """Subscribed to every topic once it is constructed, and acknowledging what it must."""

    def __init__(self, port: int, qos: int = 0) -> None:
        self.messages: list[Received] = []
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=5)
        client_id = b"test-subscriber"
        variable = b"\x00\x04MQTT\x04\x02\x00\x00" + struct.pack(">H", len(client_id)) + client_id
        self.socket.sendall(bytes([0x10, len(variable)]) + variable)
        assert self._read_packet()[0] == 2  # CONNACK
        topic = b"\x00\x01#" + bytes([qos])
        self.socket.sendall(bytes([0x82, 2 + len(topic)]) + b"\x00\x01" + topic)
        assert self._read_packet()[0] == 9  # SUBACK
        self.socket.settimeout(None)
        self.thread = threading.Thread(target=self._read_messages, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.socket.shutdown(socket.SHUT_RDWR)
        self.socket.close()
        self.thread.join(5)

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

    def _read_exactly(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self.socket.recv(count - len(data))
            if not chunk:
                raise ConnectionError("the broker closed the connection")
            data += chunk
        return data

    def _read_packet(self) -> tuple[int, int, bytes]:
        first = self._read_exactly(1)[0]
        length, shift = 0, 0
        while True:
            byte = self._read_exactly(1)[0]
            length |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return first >> 4, first & 0x0F, self._read_exactly(length)

    def _read_messages(self) -> None:
        try:
            while True:
                kind, flags, body = self._read_packet()
                if kind != 3:
                    continue
                qos = (flags >> 1) & 3
                size = struct.unpack(">H", body[:2])[0]
                topic = body[2 : 2 + size].decode()
                rest = body[2 + size :]
                if qos:
                    self.socket.sendall(b"\x40\x02" + rest[:2])  # PUBACK
                    rest = rest[2:]
                self.messages.append(Received(topic, rest, qos, bool(flags & 1), time.monotonic()))
        except OSError:
            pass  # close(), or a broker that stopped.
