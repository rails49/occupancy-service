"""The publishing half of an MQTT 3.1.1 client, on one socket, for one thread to call."""

import secrets
import select
import socket
import struct
import time

_CONNACK, _PUBREC = 2, 5
_MAX_REMAINING_BYTES = 4  # The most bytes MQTT's remaining length runs to.
_PINGREQ = b"\xc0\x00"
_DISCONNECT = b"\xe0\x00"


class MqttError(OSError):
    """The broker refused us, went quiet, or broke the protocol. Like a socket's own errors, an
    ``OSError`` means the connection is finished."""


def _remaining_length(count: int) -> bytes:
    encoded = bytearray()
    while True:
        count, digit = count // 128, count % 128
        encoded.append(digit | (0x80 if count else 0))
        if not count:
            return bytes(encoded)


def _string(text: str) -> bytes:
    data = text.encode()
    return struct.pack(">H", len(data)) + data


class MqttClient:
    """A clean-session connection, made by the constructor.

    Nothing here retransmits: a QoS 1 or 2 message unanswered when the connection ends is gone, as
    the caller sends everything again on the next one. A call raises ``OSError`` when the
    connection is finished, and then only ``close()`` is left to call.
    """

    def __init__(self, host: str, port: int, keepalive: int, timeout: float) -> None:
        """``keepalive`` seconds is what the broker is promised; ``timeout`` seconds bounds the
        connecting and each write."""
        self._keepalive = keepalive
        self._buffer = b""
        self._packet_id = 0
        self._socket = socket.create_connection((host, port), timeout=timeout)
        try:
            self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            client_id = f"occupancy-service-{secrets.token_hex(4)}"
            flags_clean_session = 0x02
            body = (
                _string("MQTT")
                + bytes([4, flags_clean_session])
                + struct.pack(">H", keepalive)
                + _string(client_id)
            )
            self._send(0x10, body)
            self._await_connack(timeout)
        except OSError:
            self._socket.close()
            raise
        self._received_at = self._pinged_at = time.monotonic()

    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None:
        """Sends one message. ``topic`` must be one MQTT allows to be published to."""
        packet_id = b""
        if qos:
            self._packet_id = self._packet_id % 0xFFFF + 1
            packet_id = struct.pack(">H", self._packet_id)
        self._send(0x30 | qos << 1 | int(retain), _string(topic) + packet_id + payload)

    def poll(self) -> None:
        """Takes what the broker has sent, without waiting, and keeps the connection alive."""
        while select.select([self._socket], [], [], 0)[0]:
            self._receive()
        for kind, body in self._packets():
            if kind == _PUBREC:
                self._send(0x62, body[:2])  # PUBREL, the part of QoS 2 this side must answer.
        now = time.monotonic()
        if now - self._received_at > self._keepalive:
            raise MqttError(f"nothing from the broker for {self._keepalive} s")
        if now - self._pinged_at >= self._keepalive / 2:
            self._socket.sendall(_PINGREQ)
            self._pinged_at = now

    def disconnect(self) -> None:
        """Ends the connection as MQTT asks, after everything already published."""
        self._socket.sendall(_DISCONNECT)

    def close(self) -> None:
        self._socket.close()

    def _send(self, first_byte: int, body: bytes) -> None:
        self._socket.sendall(bytes([first_byte]) + _remaining_length(len(body)) + body)

    def _await_connack(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            for kind, body in self._packets():
                if kind != _CONNACK or len(body) < 2:
                    raise MqttError(f"the broker sent packet type {kind} before CONNACK")
                if body[1] != 0:
                    raise MqttError(f"the broker refused the connection with code {body[1]}")
                return
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self._socket], [], [], left)[0]:
                raise MqttError(f"no CONNACK within {timeout} s")
            self._receive()

    def _receive(self) -> None:
        data = self._socket.recv(4096)
        if not data:
            raise MqttError("the broker closed the connection")
        self._received_at = time.monotonic()
        self._buffer += data

    def _packets(self) -> list[tuple[int, bytes]]:
        """Each whole packet that has come in, as its type and everything after its header."""
        packets = []
        while True:
            parsed = self._next_packet()
            if parsed is None:
                return packets
            packets.append(parsed)

    def _next_packet(self) -> tuple[int, bytes] | None:
        length, shift = 0, 0
        for at in range(1, _MAX_REMAINING_BYTES + 1):
            if at >= len(self._buffer):
                return None
            byte = self._buffer[at]
            length |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                end = at + 1 + length
                if len(self._buffer) < end:
                    return None
                kind, body = self._buffer[0] >> 4, self._buffer[at + 1 : end]
                self._buffer = self._buffer[end:]
                return kind, body
        raise MqttError("the broker sent a malformed packet")
