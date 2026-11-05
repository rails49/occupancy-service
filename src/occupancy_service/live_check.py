"""Check a running implementation against LIVE.md. Format and consistency only."""

import argparse
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from email.message import Message
from email.parser import BytesHeaderParser
from http.client import HTTPException
from typing import Any
from urllib.parse import quote

import r49
from PIL import Image, UnidentifiedImageError

_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})")
_PATCH_PART = re.compile(r"patch-\d+")


@dataclass(frozen=True)
class Failure:
    """One broken rule. ``rule`` is the id LIVE.md lists; ``message`` is for people."""

    rule: str
    message: str


@dataclass(frozen=True)
class _Part:
    name: str | None
    filename: str | None
    body: bytes


class _Broken(Exception):
    """A failure that leaves the rest of an answer impossible to check."""

    def __init__(self, rule: str, message: str) -> None:
        self.failure = Failure(rule, message)


def check(base_url: str, timeout: float = 10) -> list[Failure]:
    """Every rule the implementation at ``base_url`` breaks. Empty when it conforms."""
    base = base_url.rstrip("/")
    manifest, failures = _read_manifest(f"{base}/archive/manifest.json", timeout)
    if manifest is None:
        return failures
    sensors = {sensor.id for sensor in manifest.layout.sensors}
    resolution = manifest.camera.resolution
    size = (resolution.width, resolution.height)

    def check_latest(query: str) -> dict[str, Any]:
        """Add what is wrong with this answer to ``failures``, and return its JSON fields."""
        try:
            fields, parts = _fetch_latest(f"{base}/latest{query}", timeout)
        except _Broken as broken:
            failures.append(broken.failure)
            return {}
        failures.extend(_latest_failures(fields, parts, sensors, size))
        return fields

    frame_id = check_latest("").get("frame_id")
    with_patches = check_latest("?patches=1")
    if isinstance(frame_id, str):
        after = check_latest(f"?after={quote(frame_id, safe='')}&patches=1")
        if "patches" in with_patches and after.get("frame_id") and "patches" not in after:
            failures.append(Failure("patches/after", "patches=1 stops working alongside after"))
    failures.extend(_events_failures(f"{base}/events", sensors, timeout))
    return list(dict.fromkeys(failures))


def _read_manifest(url: str, timeout: float) -> tuple[r49.Manifest | None, list[Failure]]:
    """The manifest, or None when it cannot be read, and what is wrong with the answer."""
    try:
        status, headers, body = _get(url, timeout)
    except _Broken as broken:
        return None, [broken.failure]
    if status != 200:
        return None, [Failure("archive/status", f"GET {url} answered {status}")]
    failures = []
    if headers.get_content_type() != "application/json":
        failures.append(
            Failure("archive/content-type", f"manifest.json is {headers.get_content_type()}")
        )
    try:
        return r49.parse_manifest(body), failures
    except r49.R49Error as error:
        return None, [*failures, Failure("archive/manifest", f"{error.rule}: {error}")]


def _fetch_latest(url: str, timeout: float) -> tuple[dict[str, Any], dict[str, _Part]]:
    """The JSON part's fields and the parts by name. A reader takes a name's first part."""
    status, headers, body = _get(url, timeout)
    if status != 200:
        raise _Broken("latest/status", f"latest answered {status}")
    boundary = headers.get_boundary()
    if headers.get_content_type() != "multipart/form-data" or not boundary:
        raise _Broken("latest/content-type", "the answer is not multipart/form-data")
    parts: dict[str, _Part] = {}
    for part in _parse_parts(body, boundary):
        if part.name is not None:
            parts.setdefault(part.name, part)
    json_part = parts.get("json")
    if json_part is None:
        raise _Broken("latest/json-part", "no part named json")
    if json_part.filename is not None:
        raise _Broken("latest/json-part", "the json part has a filename")
    try:
        fields = _parse_json(json_part.body)
    except ValueError as error:
        raise _Broken("latest/json-part", f"the json part is not JSON: {error}") from None
    if not isinstance(fields, dict):
        raise _Broken("latest/json-part", "the json part is not an object")
    return fields, parts


def _latest_failures(
    fields: dict[str, Any], parts: dict[str, _Part], sensors: set[str], size: tuple[int, int]
) -> Iterator[Failure]:
    version = fields.get("version")
    if type(version) is not int or version != 1:
        yield Failure("latest/version", f"version is {version!r}, not the integer 1")
    frame_id, captured_at = fields.get("frame_id"), fields.get("captured_at")
    l1 = fields.get("l1")
    frame = parts.get("frame")
    if frame is None:
        if fields.get("frame_id", "absent") is not None:
            yield Failure("latest/frame-id", "with no frame, frame_id is not null")
        if fields.get("captured_at", "absent") is not None:
            yield Failure("latest/captured-at", "with no frame, captured_at is not null")
        if parts.keys() != {"json"}:
            yield Failure("latest/no-frame-parts", "with no frame, there is a part other than json")
        if isinstance(l1, dict) and any(
            not (isinstance(entry, dict) and entry.get("state") == "unknown")
            for entry in l1.values()
        ):
            yield Failure("latest/no-frame-state", "no frame part while a sensor is not unknown")
    else:
        if not _is_text(frame_id):
            yield Failure("latest/frame-id", f"frame_id is {frame_id!r}, not a non-empty string")
        if not _is_time(captured_at):
            yield Failure(
                "latest/captured-at", f"captured_at {captured_at!r} is not an RFC 3339 time"
            )
        yield from _frame_failures(frame, size)
    yield from _l1_map_failures(l1, sensors)
    for field, valid in _OPTIONAL.items():
        if field in fields and not valid(fields[field], sensors):
            yield Failure(f"optional/{field}", f"{field} is malformed: {fields[field]!r}")
    yield from _patches_failures(fields, parts)


def _frame_failures(frame: _Part, size: tuple[int, int]) -> Iterator[Failure]:
    if not frame.filename:
        yield Failure("frame/no-filename", "the frame part has no filename")
    try:
        with Image.open(io.BytesIO(frame.body)) as image:
            image.load()
            found, jpeg_format = image.size, image.format == "JPEG"
    except UnidentifiedImageError:
        # Not Pillow's message: it has an address in it, which defeats check()'s dedupe.
        yield Failure("frame/jpeg", "the frame part is not an image")
        return
    except (OSError, Image.DecompressionBombError) as error:
        yield Failure("frame/jpeg", f"the frame part does not decode: {error}")
        return
    if not jpeg_format:
        yield Failure("frame/jpeg", "the frame part is not a JPEG")
    if found != size:
        yield Failure("frame/size", f"the frame is {found}, and camera.resolution is {size}")


def _l1_map_failures(l1: Any, sensors: set[str]) -> Iterator[Failure]:
    if not isinstance(l1, dict):
        yield Failure("l1/shape", "l1 is not an object of sensor id to entry")
        return
    for sensor in sorted(sensors - l1.keys()):
        yield Failure("l1/missing-sensor", f"no L1 entry for {sensor!r}")
    for sensor in sorted(l1.keys() - sensors):
        yield Failure("l1/extra-sensor", f"{sensor!r} is not a sensor in the manifest")
    for sensor, entry in l1.items():
        yield from _l1_entry_failures(sensor, entry)


def _l1_entry_failures(sensor: str, entry: Any) -> Iterator[Failure]:
    state = entry.get("state") if isinstance(entry, dict) else None
    if state == "occupied":
        if not _is_number(entry.get("confidence"), high=1):
            yield Failure("l1/occupied-confidence", f"{sensor!r} has no confidence from 0 to 1")
    elif state == "clear":
        if "confidence" in entry:
            yield Failure("l1/clear-confidence", f"{sensor!r} is clear with a confidence")
    elif state == "unknown":
        if not _is_text(entry.get("reason")):
            yield Failure("l1/unknown-reason", f"{sensor!r} is unknown with no reason")
        if "confidence" in entry:
            yield Failure("l1/unknown-confidence", f"{sensor!r} is unknown with a confidence")
    else:
        yield Failure("l1/state", f"{sensor!r} has no state of occupied, clear or unknown")


def _patches_failures(fields: dict[str, Any], parts: dict[str, _Part]) -> Iterator[Failure]:
    patches = fields.get("patches")
    if not isinstance(patches, dict):
        return
    for sensor, name in patches.items():
        if not isinstance(name, str):
            continue
        part = parts.get(name)
        if part is None:
            yield Failure("patches/missing-part", f"{sensor!r} names part {name!r}: no such part")
        elif not part.filename:
            yield Failure("patches/no-filename", f"part {name!r} has no filename")


def _events_failures(url: str, sensors: set[str], timeout: float) -> Iterator[Failure]:
    seen: set[str] = set()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            if content_type != "text/event-stream":
                yield Failure("events/content-type", f"the stream is {content_type}")
            for data in _event_data(response, time.monotonic() + timeout):
                try:
                    event = _parse_json(data)
                except ValueError:
                    event = None
                sensor = event.get("sensor") if isinstance(event, dict) else None
                if not _is_text(sensor):
                    yield Failure("events/data", f"an event is not an L1 entry: {data!r}")
                elif sensor not in sensors:
                    yield Failure("events/extra-sensor", f"{sensor!r} is not in the manifest")
                else:
                    seen.add(sensor)
                    yield from _l1_entry_failures(sensor, event)
                if seen >= sensors:
                    return
    except urllib.error.HTTPError as error:
        yield Failure("events/status", f"GET {url} answered {error.code}")
        return
    except urllib.error.URLError as error:
        yield Failure("http/unreachable", f"GET {url} failed: {error.reason}")
        return
    except (OSError, HTTPException):
        pass  # A silent or dropped stream ends the reading. What it did not send is below.
    for sensor in sorted(sensors - seen):
        yield Failure("events/missing-sensor", f"no state for {sensor!r} on connecting")


def _event_data(lines: Iterable[bytes], deadline: float) -> Iterator[bytes]:
    """The data of each event ``EventSource.onmessage`` would see, until ``deadline``."""
    data: list[bytes] = []
    named = False
    for raw in lines:
        if time.monotonic() > deadline:
            return
        line = raw.rstrip(b"\r\n")
        if not line:
            if data and not named:
                yield b"\n".join(data)
            data, named = [], False
        elif line.startswith(b"data:"):
            data.append(line.removeprefix(b"data:").removeprefix(b" "))
        elif line.startswith(b"event:"):
            named = True


def _by_sensor(value: Any, sensors: set[str], valid: Callable[[Any], bool]) -> bool:
    return isinstance(value, dict) and all(
        sensor in sensors and valid(entry) for sensor, entry in value.items()
    )


def _has_numbers(value: Any, keys: tuple[str, ...]) -> bool:
    return isinstance(value, dict) and all(_is_number(value[key]) for key in keys if key in value)


def _is_count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _is_camera(value: Any) -> bool:
    return _has_numbers(value, ("rate_fps", "frame_age_ms")) and value.get("state", "ok") in (
        "ok",
        "lost",
    )


def _is_patch_part(value: Any) -> bool:
    return isinstance(value, str) and _PATCH_PART.fullmatch(value) is not None


def _is_l0(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and _is_text(value.get("frame_id"))
        and _is_time(value.get("captured_at"))
    )


_OPTIONAL: dict[str, Callable[[Any, set[str]], bool]] = {
    "l0": lambda value, _: _is_l0(value),
    "scores": lambda value, sensors: _by_sensor(value, sensors, lambda v: _is_number(v, high=1)),
    "threshold": lambda value, _: _is_number(value, high=1),
    "patches": lambda value, sensors: _by_sensor(value, sensors, _is_patch_part)
    and len(set(value.values())) == len(value),
    "changes": lambda value, sensors: _by_sensor(value, sensors, _is_count),
    "timings": lambda value, _: _has_numbers(
        value, ("capture_ms", "decode_ms", "cut_ms", "classify_ms", "rate_fps")
    ),
    "camera": lambda value, _: _is_camera(value),
}


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _is_time(value: Any) -> bool:
    """An RFC 3339 time with an offset, which ``Date.parse`` and Python both read."""
    if not (isinstance(value, str) and _TIME.fullmatch(value)):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_number(value: Any, high: float = sys.float_info.max) -> bool:
    """A finite JSON number from 0 to ``high``. Every number in the contract is one."""
    return isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value <= high


def _get(url: str, timeout: float) -> tuple[int, Message, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, b""
    except (OSError, HTTPException) as error:
        raise _Broken("http/unreachable", f"GET {url} failed: {error}") from None


def _parse_json(data: bytes) -> Any:
    """JSON as ``JSON.parse`` reads it, which refuses NaN and Infinity."""

    def refuse(constant: str) -> None:
        raise ValueError(f"{constant} is not JSON")

    try:
        return json.loads(data.decode(), parse_constant=refuse)
    except RecursionError:
        raise ValueError("nested too deep") from None


def _parse_parts(body: bytes, boundary: str) -> list[_Part]:
    """Split a multipart body as a browser does: CRLF only, and a closing delimiter."""
    parts = []
    for segment in (b"\r\n" + body).split(b"\r\n--" + boundary.encode())[1:]:
        if segment.startswith(b"--"):
            return parts
        head, _, payload = segment.partition(b"\r\n\r\n")
        headers = BytesHeaderParser().parsebytes(head.lstrip(b"\r\n"))
        parts.append(_Part(_param(headers, "name"), _param(headers, "filename"), payload))
    raise _Broken("latest/multipart", "the multipart body has no closing delimiter")


def _param(headers: Message, key: str) -> str | None:
    value = headers.get_param(key, header="content-disposition")
    return value if isinstance(value, str) else None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="live-check", description="Check a running implementation against LIVE.md."
    )
    parser.add_argument("base_url", help="the URL that latest, events and archive/ sit under")
    failures = check(parser.parse_args(argv).base_url)
    for failure in failures:
        print(f"{failure.rule}: {failure.message}")
    if failures:
        sys.exit(1)
    print("conforms to LIVE.md")
