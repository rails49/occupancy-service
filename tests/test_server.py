"""The server over real HTTP, on the real L1 loop with a fake capture source and scorer."""

import email.parser
import http.client
import io
import json
import socket
import struct
import sys
import threading
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fake_l1 import (
    HEIGHT,
    WIDTH,
    FakeDetector,
    FakeScorer,
    FakeSource,
    detection,
    jpeg,
    picture,
    running,
    wait_for,
    write_archive,
)
from PIL import Image

from occupancy_service.active_profile import LastMessage
from occupancy_service.capture import Frame
from occupancy_service.l1 import L1Loop, L1Settings
from occupancy_service.live_check import check
from occupancy_service.server import LiveServer, ServerSettings

ONE_SENSOR = {"s1": (160.0, 120.0)}


@dataclass
class Live:
    """A running server, and what a test drives it through."""

    server: LiveServer
    loop: L1Loop
    source: FakeSource
    scorer: FakeScorer

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.port}/live"

    def get(self, path: str) -> http.client.HTTPResponse:
        """GET ``path`` as written, with nothing normalizing it on the way."""
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        connection.request("GET", path)
        return connection.getresponse()


@dataclass
class Part:
    name: str
    filename: str | None
    content_type: str
    body: bytes


@dataclass
class Latest:
    """One answer from ``latest``, split as a browser's ``response.formData()`` does."""

    headers: http.client.HTTPMessage
    fields: dict[str, Any]
    parts: dict[str, Part]


@contextmanager
def live(
    archive: Path,
    scores: dict[str, float] | None = None,
    last_messages: Callable[[], Mapping[str, Mapping[str, LastMessage]]] | None = None,
    l1_settings: Mapping[str, Any] | None = None,
    detector: FakeDetector | None = None,
    railroad: Callable[[], str | None] | None = None,
    pattern: str | None = None,
    **settings: Any,
) -> Iterator[Live]:
    source = FakeSource()
    scorer = FakeScorer(scores if scores is not None else {"s1": 0.9})
    server_settings = ServerSettings(address="127.0.0.1", port=0, **settings)
    l1 = L1Settings(archive, **(l1_settings or {}))
    with running(L1Loop(l1, source, scorer, detector=detector)) as loop:
        server = LiveServer(server_settings, loop, last_messages, railroad, pattern)
        server.start()
        try:
            yield Live(server, loop, source, scorer)
        finally:
            server.stop()


def read_latest(service: Live, query: str = "") -> Latest:
    response = service.get(f"/live/latest{query}")
    assert response.status == 200
    content_type = response.headers.get_content_type()
    assert content_type == "multipart/form-data"
    boundary = response.headers.get_boundary()
    assert boundary is not None
    parts: dict[str, Part] = {}
    body = response.read()
    assert body.endswith(f"--{boundary}--\r\n".encode())
    for segment in (b"\r\n" + body).split(f"\r\n--{boundary}".encode())[1:-1]:
        head, _, payload = segment.lstrip(b"\r\n").partition(b"\r\n\r\n")
        message = email.parser.BytesHeaderParser().parsebytes(head)
        name = message.get_param("name", header="content-disposition")
        filename = message.get_param("filename", header="content-disposition")
        assert isinstance(name, str)
        assert filename is None or isinstance(filename, str)
        parts[name] = Part(name, filename, message.get_content_type(), payload)
    return Latest(response.headers, json.loads(parts["json"].body), parts)


def wait_until_served(service: Live, frame: Frame) -> None:
    wait_for(lambda: service.loop.latest().frame == frame)


class EventStream:
    """A reader of ``events`` that never blocks a test for long."""

    def __init__(self, service: Live) -> None:
        self.socket = socket.create_connection(("127.0.0.1", service.server.port), timeout=5)
        self.socket.sendall(b"GET /live/events HTTP/1.1\r\nHost: test\r\n\r\n")
        self.buffer = b""
        while b"\r\n\r\n" not in self.buffer:
            self._read()
        head, _, self.buffer = self.buffer.partition(b"\r\n\r\n")
        self.head = head.decode()

    def _read(self) -> None:
        data = self.socket.recv(65536)
        assert data, "the stream ended"
        self.buffer += data

    def next_block(self) -> bytes:
        """The next run of lines up to a blank line."""
        while b"\n\n" not in self.buffer:
            self._read()
        block, _, self.buffer = self.buffer.partition(b"\n\n")
        return block

    def next_event(self) -> dict[str, Any]:
        """The next event, passing over comment lines."""
        while True:
            block = self.next_block()
            if block.startswith(b"data: "):
                return json.loads(block.removeprefix(b"data: "))

    def is_silent_for(self, seconds: float) -> bool:
        """Whether no event, or anything else, comes in that time."""
        self.socket.settimeout(seconds)
        try:
            self._read()
        except TimeoutError:
            return True
        finally:
            self.socket.settimeout(5)
        return False

    def close(self) -> None:
        self.socket.close()


@contextmanager
def event_stream(service: Live) -> Iterator[EventStream]:
    stream = EventStream(service)
    try:
        yield stream
    finally:
        stream.close()


def folder_archive(path: Path, sensors: dict[str, tuple[float, float]] = ONE_SENSOR) -> Path:
    """An archive in a folder, with a photograph, a nested file and files a folder ignores."""
    folder = write_archive(path / "folder", sensors)
    (folder / "photo.jpg").write_bytes(b"photo bytes")
    (folder / "nested").mkdir()
    (folder / "nested" / "photo 2.jpg").write_bytes(b"nested bytes")
    (folder / ".git").mkdir()
    (folder / ".git" / "config").write_text("secret")
    (path / "outside.txt").write_text("outside the archive")
    return folder


def zip_archive(path: Path) -> Path:
    """The same archive as a zip, which also holds a dot path a folder would ignore."""
    folder = folder_archive(path)
    zipped = path / "archive.r49"
    with zipfile.ZipFile(zipped, "w") as out:
        out.write(folder / "manifest.json", "manifest.json")
        out.write(folder / "photo.jpg", "photo.jpg")
        out.write(folder / "nested" / "photo 2.jpg", "nested/photo 2.jpg")
        out.write(folder / ".git" / "config", ".git/config")
    return zipped


@pytest.fixture(params=[folder_archive, zip_archive], ids=["folder", "zip"])
def any_archive(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    return request.param(tmp_path)


def test_serves_each_file_of_the_archive(any_archive: Path) -> None:
    with live(any_archive) as service:
        photo = service.get("/live/archive/photo.jpg")
        assert (photo.status, photo.read()) == (200, b"photo bytes")
        assert photo.headers["Content-Type"] == "image/jpeg"
        nested = service.get("/live/archive/nested/photo%202.jpg")
        assert (nested.status, nested.read()) == (200, b"nested bytes")


def test_the_manifest_is_json_and_not_stored(any_archive: Path) -> None:
    with live(any_archive) as service:
        manifest = service.get("/live/archive/manifest.json")
        assert manifest.status == 200
        assert b'"archive-1"' in manifest.read()
        assert manifest.headers["Content-Type"] == "application/json"
        assert manifest.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "path",
    [
        "/live/archive/not-there.jpg",
        "/live/archive/",
        "/live/archive/nested",
        "/live/archive/../x",
        "/live/archive/../outside.txt",
        "/live/archive/%2e%2e/outside.txt",
        "/live/archive/%2E%2E%2Foutside.txt",
        "/live/archive/nested/../photo.jpg",
        "/live/archive//etc/passwd",
        "/live/archive/.git/config",
        "/live/archive/%2egit/config",
        "/live/archive/nested/.hidden",
        "/live/archive/photo.jpg%00",
        "/live/nothing",
        "/archive/photo.jpg",
        "/latest",
    ],
)
def test_anything_else_is_a_404(any_archive: Path, path: str) -> None:
    with live(any_archive) as service:
        response = service.get(path)
        assert response.status == 404, response.read()


def test_a_client_that_resets_an_idle_connection_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def requests() -> set[threading.Thread]:
        return {t for t in threading.enumerate() if "process_request_thread" in t.name}

    before = requests()
    with live(folder_archive(tmp_path)) as service:
        client = socket.create_connection(("127.0.0.1", service.server.port), timeout=10)
        client.sendall(b"GET /live/archive/manifest.json HTTP/1.1\r\nHost: x\r\n\r\n")
        assert client.recv(12) == b"HTTP/1.1 200"
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        client.close()  # A reset, not a close.
        wait_for(lambda: True if not requests() - before else None)
    assert capsys.readouterr().err == ""


def test_no_access_control_headers_and_an_origin_changes_nothing(tmp_path: Path) -> None:
    with live(folder_archive(tmp_path)) as service:
        connection = http.client.HTTPConnection("127.0.0.1", service.server.port, timeout=10)
        connection.request(
            "GET", "/live/archive/photo.jpg", headers={"Origin": "http://elsewhere.example"}
        )
        response = connection.getresponse()
        assert response.status == 200
        assert not [key for key in response.headers if key.lower().startswith("access-control-")]


def test_latest_is_the_newest_scored_frame_with_its_l1(tmp_path: Path) -> None:
    with live(folder_archive(tmp_path), {"s1": 0.9}) as service:
        frame = service.source.show()
        wait_until_served(service, frame)
        answer = read_latest(service)

    fields = answer.fields
    assert fields["version"] == 1
    assert isinstance(fields["frame_id"], str) and fields["frame_id"]
    assert fields["captured_at"] == frame.arrived_at.isoformat()
    assert fields["l1"] == {"s1": {"state": "occupied", "confidence": 0.9}}
    assert answer.parts["frame"].body == frame.jpeg
    assert answer.parts["frame"].filename == "frame.jpg"
    assert answer.parts["frame"].content_type == "image/jpeg"
    assert answer.parts["json"].filename is None
    assert answer.headers["Cache-Control"] == "no-store"
    assert not [key for key in answer.headers if key.lower().startswith("access-control-")]


def test_latest_serves_the_scored_frame_and_not_a_newer_captured_one(tmp_path: Path) -> None:
    gate = threading.Event()
    with live(folder_archive(tmp_path), {"s1": 0.9}) as service:
        scored = service.source.show()
        wait_until_served(service, scored)
        service.scorer.gate = gate
        service.source.show()
        service.scorer.started.acquire()
        service.scorer.started.acquire()  # The second frame is now being scored.
        answer = read_latest(service)
        gate.set()

    assert answer.parts["frame"].body == scored.jpeg
    assert answer.fields["frame_id"].endswith(f"-{scored.id}")


def test_latest_carries_the_optional_fields(tmp_path: Path) -> None:
    sensors = {"s1": (160.0, 120.0), "s2": (170.0, 120.0)}
    with live(folder_archive(tmp_path, sensors), {"s1": 0.9, "s2": 0.1}) as service:
        frame = service.source.show()
        wait_until_served(service, frame)
        fields = read_latest(service).fields

    assert fields["l1"] == {
        "s1": {"state": "occupied", "confidence": 0.9},
        "s2": {"state": "clear"},
    }
    assert fields["scores"] == {"s1": 0.9, "s2": 0.1}
    assert fields["threshold"] == 0.5
    assert fields["changes"] == {"s1": 1, "s2": 1}
    assert fields["timings"]["cut_ms"] == 1.0
    assert fields["timings"]["classify_ms"] == 2.0
    assert fields["timings"]["capture_ms"] >= 0
    assert fields["timings"]["decode_ms"] >= 0
    assert fields["timings"]["rate_fps"] >= 0
    assert fields["camera"] == {"state": "ok", "rate_fps": 0.0, "frame_age_ms": 0.0}
    assert "patches" not in fields


def test_latest_names_l0s_own_frame_when_the_detector_is_slower(tmp_path: Path) -> None:
    detector = FakeDetector([detection(160, 120, length=48, angle=0.25, confidence=0.7)])
    settings = {"detect_every": 60}
    with live(folder_archive(tmp_path), {"s1": 0.1}, None, settings, detector) as service:
        first = service.source.show()
        wait_for(lambda: "l0" in read_latest(service).fields)
        later = service.source.show()
        wait_until_served(service, later)
        fields = read_latest(service).fields
        assert check(service.base) == []

    assert fields["frame_id"].endswith(f"-{later.id}")
    assert fields["l0"] == {
        "frame_id": fields["frame_id"].removesuffix(f"-{later.id}") + f"-{first.id}",
        "captured_at": first.arrived_at.isoformat(),
        "detections": [
            {
                "centre": {"x": 160, "y": 120},
                "length": 48,
                "width": 30,
                "angle": 0.25,
                "class": "passenger",
                "confidence": 0.7,
            }
        ],
    }
    assert fields["l1"] == {"s1": {"state": "occupied", "confidence": 0.7}}
    assert fields["detector"]["state"] == "ok"
    assert fields["timings"]["detect_ms"] >= 0
    assert fields["timings"]["l0_rate_fps"] == 0
    assert fields["timings"]["l0_age_ms"] >= 0


def test_latest_has_an_empty_l0_when_the_detector_finds_nothing(tmp_path: Path) -> None:
    with live(folder_archive(tmp_path), {"s1": 0.1}, detector=FakeDetector()) as service:
        service.source.show()
        fields = wait_for(lambda: (f := read_latest(service).fields).get("l0") and f)

    assert fields["l0"]["detections"] == []
    assert fields["l1"] == {"s1": {"state": "clear"}}


def test_latest_drops_l0_at_the_limit(tmp_path: Path) -> None:
    detector = FakeDetector([detection(160, 120)])
    settings = {"detect_every": 60, "l0_limit": 0.3}
    with live(folder_archive(tmp_path), {"s1": 0.1}, None, settings, detector) as service:
        service.source.show()
        wait_for(lambda: "l0" in read_latest(service).fields)

        def stale() -> dict[str, Any] | None:
            wait_until_served(service, service.source.show())
            fields = read_latest(service).fields
            return None if "l0" in fields else fields

        fields = wait_for(stale)
        assert check(service.base) == []

    assert fields["l1"] == {"s1": {"state": "clear"}}
    assert fields["timings"]["l0_age_ms"] > 300


def test_latest_has_no_detector_and_no_l0_with_no_model_path(tmp_path: Path) -> None:
    with live(folder_archive(tmp_path)) as service:
        wait_until_served(service, service.source.show())
        fields = read_latest(service).fields

    assert "detector" not in fields
    assert "l0" not in fields
    assert not {"detect_ms", "l0_rate_fps", "l0_age_ms"} & fields["timings"].keys()


def test_latest_names_the_cpu_for_a_detector_on_onnxruntime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "openvino", None)  # import openvino raises ImportError.
    with live(folder_archive(tmp_path), {"s1": 0.1}, detector=FakeDetector()) as service:
        wait_until_served(service, service.source.show())
        fields = read_latest(service).fields
        assert check(service.base) == []

    assert fields["detector"] == {"state": "ok", "engine": "onnxruntime", "device": "CPU"}


def test_latest_says_the_detector_failed_with_the_package_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "detector", None)
    settings = {"detector_model": tmp_path / "model.onnx"}
    with live(folder_archive(tmp_path), {"s1": 0.9}, None, settings) as service:
        wait_until_served(service, service.source.show())
        fields = read_latest(service).fields
        assert check(service.base) == []

    assert fields["detector"] == {"state": "failed", "reason": "rails49-detector is not installed"}
    assert "l0" not in fields
    assert fields["l1"] == {"s1": {"state": "occupied", "confidence": 0.9}}


def test_latest_says_why_drift_is_unmeasured_with_a_frame_and_with_none(tmp_path: Path) -> None:
    with live(folder_archive(tmp_path)) as service:
        assert read_latest(service).fields["drift"] == {"unmeasured": "no first image"}
        wait_until_served(service, service.source.show())
        fields = read_latest(service).fields
        assert check(service.base) == []

    assert fields["drift"] == {"unmeasured": "no first image"}
    assert fields["l1"] == {"s1": {"state": "occupied", "confidence": 0.9}}


def test_latest_carries_the_reading_and_every_sensor_refuses_past_the_tolerance(
    tmp_path: Path,
) -> None:
    archive = folder_archive(tmp_path)
    (archive / "first.png").write_bytes(picture())
    write_archive(archive, ONE_SENSOR, images=[{"filename": "first.png"}])
    with live(archive, l1_settings={"drift_every": 0.01}) as service:

        def drift_is(px: float) -> dict[str, Any] | None:
            fields = read_latest(service).fields
            return fields if fields["drift"].get("px") == px else None

        service.source.show(picture(format="JPEG"))
        within = wait_for(lambda: drift_is(0.0))
        assert within["drift"] == {
            "px": 0.0,
            "tolerance_px": pytest.approx(8.247, abs=1e-3),
            "past": False,
        }

        service.source.show(picture(dx=20, format="JPEG"))
        past = wait_for(lambda: drift_is(20.0))
        assert past["drift"]["past"] is True
        assert past["l1"] == {"s1": {"state": "unknown", "reason": "drift"}}
        assert past["scores"] == {"s1": 0.9}
        assert check(service.base) == []


def test_latest_has_no_drift_with_the_check_turned_off(tmp_path: Path) -> None:
    with live(folder_archive(tmp_path), l1_settings={"drift_tolerance_g": None}) as service:
        wait_until_served(service, service.source.show())
        assert "drift" not in read_latest(service).fields
        assert check(service.base) == []


def test_with_the_camera_lost_only_the_json_part_and_with_frames_again_a_full_answer(
    tmp_path: Path,
) -> None:
    with live(folder_archive(tmp_path)) as service:
        wait_until_served(service, service.source.show())
        service.source.state = "lost"
        lost = read_latest(service, "?patches=1")
        service.source.state = "ok"
        frame = service.source.show()
        wait_until_served(service, frame)
        back = read_latest(service)

    assert lost.parts.keys() == {"json"}
    assert lost.fields["frame_id"] is None
    assert lost.fields["captured_at"] is None
    assert lost.fields["l1"] == {"s1": {"state": "unknown", "reason": "no frames"}}
    assert lost.fields["camera"]["state"] == "lost"
    assert lost.fields["scores"] == {}
    assert lost.fields["patches"] == {}
    assert lost.fields["timings"].keys() == {"rate_fps"}  # No stage times of a frame not served.
    assert back.parts["frame"].body == frame.jpeg
    assert back.fields["camera"]["state"] == "ok"
    assert back.fields["l1"]["s1"]["state"] == "occupied"


def test_a_camera_that_never_gave_a_frame_has_no_frame_age(tmp_path: Path) -> None:
    with live(folder_archive(tmp_path)) as service:
        service.source.state = "lost"
        fields = read_latest(service).fields

    assert fields["camera"] == {"state": "lost", "rate_fps": 0.0}


def test_a_frame_with_every_sensor_unknown_keeps_its_frame_when_scoring_stalls(
    tmp_path: Path,
) -> None:
    with live(folder_archive(tmp_path)) as service:
        frame = service.source.show()
        wait_until_served(service, frame)
        wait_for(lambda: service.loop.latest().l1["s1"].reason == "scoring stalled")
        answer = read_latest(service)

    assert answer.parts["frame"].body == frame.jpeg
    assert answer.fields["l1"] == {"s1": {"state": "unknown", "reason": "scoring stalled"}}


def test_a_frame_not_at_the_cameras_resolution_is_withheld(tmp_path: Path) -> None:
    with live(folder_archive(tmp_path)) as service:
        wait_until_served(service, service.source.show(jpeg(WIDTH // 2, HEIGHT)))
        answer = read_latest(service)

    assert answer.parts.keys() == {"json"}
    assert answer.fields["frame_id"] is None
    assert answer.fields["captured_at"] is None
    assert answer.fields["l1"] == {"s1": {"state": "unknown", "reason": "wrong frame size"}}


def png_saves(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Log each PNG that Pillow encodes from now on."""
    saves: list[object] = []
    save = Image.Image.save

    def logged(image: Image.Image, fp: Any, format: Any = None, **params: Any) -> None:
        if format == "PNG":
            saves.append(image)
        save(image, fp, format, **params)

    monkeypatch.setattr(Image.Image, "save", logged)
    return saves


def test_patches_are_one_png_part_per_sensor_with_a_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sensors = {'say "hi"': (160.0, 120.0), "s2": (170.0, 120.0), "no patch": (180.0, 120.0)}
    saves = png_saves(monkeypatch)
    archive = folder_archive(tmp_path, sensors)
    with live(archive, {'say "hi"': 0.9, "s2": 0.1}) as service:
        wait_until_served(service, service.source.show())
        answer = read_latest(service, "?patches=1")

    names = answer.fields["patches"]
    assert names.keys() == {'say "hi"', "s2"}
    assert len(set(names.values())) == 2
    assert len(saves) == 2
    assert answer.parts.keys() == {"json", "frame", *names.values()}
    for name in names.values():
        part = answer.parts[name]
        assert part.content_type == "image/png"
        assert part.filename == f"{name}.png"
        with Image.open(io.BytesIO(part.body)) as image:
            assert image.format == "PNG"
            assert image.size == (2, 2)


def test_without_the_option_no_patch_is_encoded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saves = png_saves(monkeypatch)
    with live(folder_archive(tmp_path)) as service:
        wait_until_served(service, service.source.show())
        answer = read_latest(service)

    assert saves == []
    assert "patches" not in answer.fields
    assert answer.parts.keys() == {"json", "frame"}


def test_after_returns_as_soon_as_a_newer_frame_exists(tmp_path: Path) -> None:
    with live(folder_archive(tmp_path)) as service:
        wait_until_served(service, service.source.show())
        seen = read_latest(service).fields["frame_id"]
        newer: list[Frame] = []
        timer = threading.Timer(0.2, lambda: newer.append(service.source.show()))
        timer.start()
        start = time.monotonic()
        answer = read_latest(service, f"?after={seen}")
        took = time.monotonic() - start
        timer.join()

    assert answer.parts["frame"].body == newer[0].jpeg
    assert answer.fields["frame_id"] != seen
    assert 0.15 <= took < 0.8


def test_after_answers_with_what_it_has_after_about_a_second(tmp_path: Path) -> None:
    with live(folder_archive(tmp_path)) as service:
        frame = service.source.show()
        wait_until_served(service, frame)
        seen = read_latest(service).fields["frame_id"]
        start = time.monotonic()
        answer = read_latest(service, f"?after={seen}&patches=1")
        took = time.monotonic() - start

    assert 0.9 <= took < 1.5
    assert answer.fields["frame_id"] == seen
    assert answer.parts["frame"].body == frame.jpeg
    assert answer.fields["patches"].keys() == {"s1"}


def test_after_an_older_frame_answers_at_once(tmp_path: Path) -> None:
    with live(folder_archive(tmp_path)) as service:
        wait_until_served(service, service.source.show())
        seen = read_latest(service).fields["frame_id"]
        newest = service.source.show()
        wait_until_served(service, newest)
        start = time.monotonic()
        answer = read_latest(service, f"?after={seen}")
        took = time.monotonic() - start

    assert answer.parts["frame"].body == newest.jpeg
    assert took < 0.5


@pytest.mark.parametrize("seen", ["from-another-run-1", "garbage", "1" * 5000])
def test_after_an_id_this_run_did_not_give_answers_at_once(tmp_path: Path, seen: str) -> None:
    with live(folder_archive(tmp_path)) as service:
        frame = service.source.show()
        wait_until_served(service, frame)
        start = time.monotonic()
        answer = read_latest(service, f"?after={seen}")
        took = time.monotonic() - start

    assert answer.parts["frame"].body == frame.jpeg
    assert took < 0.5


def test_after_with_the_camera_lost_is_only_the_json_part_after_about_a_second(
    tmp_path: Path,
) -> None:
    with live(folder_archive(tmp_path)) as service:
        wait_until_served(service, service.source.show())
        seen = read_latest(service).fields["frame_id"]
        service.source.state = "lost"
        start = time.monotonic()
        answer = read_latest(service, f"?after={seen}")
        took = time.monotonic() - start

    assert 0.9 <= took < 1.5
    assert answer.parts.keys() == {"json"}
    assert answer.fields["frame_id"] is None


def test_after_a_number_too_long_to_read_answers_at_once(tmp_path: Path) -> None:
    with live(folder_archive(tmp_path)) as service:
        wait_until_served(service, service.source.show())
        run = read_latest(service).fields["frame_id"].rpartition("-")[0]
        start = time.monotonic()
        answer = read_latest(service, f"?after={run}-{'1' * 5000}")
        took = time.monotonic() - start

    assert "frame" in answer.parts
    assert took < 0.5


def test_events_sends_every_sensor_on_connect_as_messages(tmp_path: Path) -> None:
    sensors = {'say "hi"': (160.0, 120.0), "s2": (170.0, 120.0), "s3": (180.0, 120.0)}
    archive = folder_archive(tmp_path, sensors)
    with live(archive, {'say "hi"': 0.9, "s2": 0.1}) as service:
        wait_until_served(service, service.source.show())
        with event_stream(service) as stream:
            assert "200 OK" in stream.head
            assert "Content-Type: text/event-stream" in stream.head
            assert "Cache-Control: no-store" in stream.head
            assert "access-control-" not in stream.head.lower()
            events = {event["sensor"]: event for event in (stream.next_event() for _ in range(3))}

    assert events == {
        'say "hi"': {"sensor": 'say "hi"', "state": "occupied", "confidence": 0.9},
        "s2": {"sensor": "s2", "state": "clear"},
        "s3": {"sensor": "s3", "state": "unknown", "reason": "outside the frame"},
    }


def test_events_then_sends_only_changes_and_none_for_a_new_confidence(tmp_path: Path) -> None:
    with live(folder_archive(tmp_path), {"s1": 0.9}) as service:
        wait_until_served(service, service.source.show())
        with event_stream(service) as stream:
            assert stream.next_event()["state"] == "occupied"

            service.scorer.scores = {"s1": 0.8}  # A new confidence, and the same state.
            frame = service.source.show()
            wait_until_served(service, frame)
            assert read_latest(service).fields["l1"]["s1"]["confidence"] == 0.8
            assert stream.is_silent_for(0.2)

            service.scorer.scores = {"s1": 0.1}  # A new state.
            service.source.show()
            assert stream.next_event() == {"sensor": "s1", "state": "clear"}

            service.source.state = "lost"  # A new reason.
            assert stream.next_event() == {
                "sensor": "s1",
                "state": "unknown",
                "reason": "no frames",
            }


def test_events_sends_a_comment_line_every_so_often(tmp_path: Path) -> None:
    with live(folder_archive(tmp_path), comment_every=0.05) as service:
        wait_until_served(service, service.source.show())
        with event_stream(service) as stream:
            stream.next_event()
            assert stream.next_block().startswith(b":")
            assert stream.next_block().startswith(b":")


def test_comments_come_every_15_seconds_by_default() -> None:
    assert ServerSettings().comment_every == 15


def test_a_stalled_client_is_dropped_without_holding_up_others_or_the_loop(tmp_path: Path) -> None:
    # Long ids make each event big, so a client that does not read soon fills its socket.
    ids = [f"{n}-{'x' * 4000}" for n in range(20)]
    sensors = {id: (160.0, 120.0) for id in ids}
    occupied = dict.fromkeys(ids, 0.9)
    clear = dict.fromkeys(ids, 0.1)
    archive = folder_archive(tmp_path, sensors)
    with live(archive, occupied, write_timeout=0.2) as service:
        wait_until_served(service, service.source.show())
        stalled = socket.socket()
        stalled.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        stalled.connect(("127.0.0.1", service.server.port))
        stalled.sendall(b"GET /live/events HTTP/1.1\r\nHost: test\r\n\r\n")
        with event_stream(service) as reader:
            for _ in ids:
                reader.next_event()
            # Each flip is 20 events of 4 kB, so 200 flips are far more than any socket holds.
            for flip in range(200):
                service.scorer.scores = clear if flip % 2 == 0 else occupied
                start = time.monotonic()
                wait_until_served(service, service.source.show())
                assert time.monotonic() - start < 1, "the scoring loop was held up"
                expected = "clear" if flip % 2 == 0 else "occupied"
                states = [reader.next_event()["state"] for _ in ids]
                assert states == [expected] * len(ids), "the other client was held up"

        # What the server had sent is still there to read, and then the stream ends.
        stalled.settimeout(5)
        received = 0
        while chunk := read_or_reset(stalled):
            received += len(chunk)
        stalled.close()

    assert received < 200 * 20 * 4000


def read_or_reset(connection: socket.socket) -> bytes:
    """What ``recv`` gives, with a connection reset read as the stream's end."""
    try:
        return connection.recv(65536)
    except ConnectionResetError:
        return b""


def test_a_client_with_too_many_changes_waiting_is_dropped(tmp_path: Path) -> None:
    ids = [f"s{n}" for n in range(20)]
    sensors = {id: (160.0, 120.0) for id in ids}
    occupied = dict.fromkeys(ids, 0.9)
    archive = folder_archive(tmp_path, sensors)
    with live(archive, occupied, max_backlog=0) as service:
        wait_until_served(service, service.source.show())
        with event_stream(service) as stream:
            service.scorer.scores = dict.fromkeys(ids, 0.1)
            wait_until_served(service, service.source.show())  # 20 changes at one moment.
            with pytest.raises(AssertionError, match="the stream ended"):
                while True:
                    stream.next_block()


def test_the_checker_passes_with_the_camera_on_and_off(tmp_path: Path) -> None:
    sensors = {'say "hi"': (160.0, 120.0), "s2": (170.0, 120.0), "no patch": (180.0, 120.0)}
    archive = folder_archive(tmp_path, sensors)
    with live(archive, {'say "hi"': 0.9, "s2": 0.1}) as service:
        assert check(service.base, timeout=5) == []  # Before any frame.

        wait_until_served(service, service.source.show())
        assert check(service.base, timeout=5) == []

        service.source.state = "lost"
        assert check(service.base, timeout=5) == []

        service.source.state = "ok"
        wait_until_served(service, service.source.show(jpeg(WIDTH // 2, HEIGHT)))
        assert check(service.base, timeout=5) == []  # A frame it cannot serve.


def test_the_checker_passes_on_a_zip_with_the_packaged_model(tmp_path: Path) -> None:
    archive = zip_archive(tmp_path)
    source = FakeSource()
    with running(L1Loop(L1Settings(archive), source)) as loop:
        server = LiveServer(ServerSettings(port=0), loop)
        server.start()
        try:
            frame = source.show()
            wait_for(lambda: loop.latest().frame == frame)
            assert check(f"http://127.0.0.1:{server.port}/live", timeout=5) == []
        finally:
            server.stop()


def test_several_answers_share_one_connection(tmp_path: Path) -> None:
    with live(folder_archive(tmp_path)) as service:
        wait_until_served(service, service.source.show())
        connection = http.client.HTTPConnection("127.0.0.1", service.server.port, timeout=10)
        for path in ("/live/latest", "/live/archive/manifest.json", "/live/latest?patches=1"):
            connection.request("GET", path)
            response = connection.getresponse()
            assert response.status == 200
            response.read()
        connection.close()


SENT_AT = datetime(2026, 11, 5, 10, 15, 30, 125000, tzinfo=UTC)


def test_latest_has_no_last_messages_when_nothing_publishes(tmp_path: Path) -> None:
    with live(write_archive(tmp_path / "archive", ONE_SENSOR)) as service:
        assert "last_messages" not in read_latest(service).fields


def test_latest_reports_the_last_message_per_sensor_and_profile(tmp_path: Path) -> None:
    sent = {
        "s1": {
            "table": LastMessage({"state": "clear"}, SENT_AT),
            "text": LastMessage("OFF", SENT_AT),
        },
    }
    archive = write_archive(tmp_path / "archive", ONE_SENSOR)
    with live(archive, last_messages=lambda: sent) as service:
        fields = read_latest(service).fields
        assert fields["last_messages"] == {
            "s1": {
                "table": {
                    "message": {"state": "clear"},
                    "time": "2026-11-05T10:15:30.125000+00:00",
                },
                "text": {"message": "OFF", "time": "2026-11-05T10:15:30.125000+00:00"},
            }
        }
        assert check(service.base, timeout=5) == []


def test_last_messages_name_only_sensors_in_the_answer(tmp_path: Path) -> None:
    """The publisher forgets a removed sensor on its next repeat, and the answer holds to one
    manifest until then."""
    sent = {"gone": {"p": LastMessage("OFF", SENT_AT)}, "s1": {"p": LastMessage("OFF", SENT_AT)}}
    archive = write_archive(tmp_path / "archive", ONE_SENSOR)
    with live(archive, last_messages=lambda: sent) as service:
        assert set(read_latest(service).fields["last_messages"]) == {"s1"}


def test_events_clients_are_closed_at_the_first_result_under_a_new_manifest(
    tmp_path: Path,
) -> None:
    folder = folder_archive(tmp_path, {"kept": (160.0, 120.0), "old": (170.0, 120.0)})
    with live(folder, {"kept": 0.9, "old": 0.9, "new": 0.9}) as service:
        wait_until_served(service, service.source.show())
        with event_stream(service) as stream:
            for _ in range(2):
                stream.next_event()

            write_archive(folder, {"kept": (160.0, 120.0), "new": (180.0, 120.0)})
            service.loop.reload()
            assert stream.is_silent_for(0.2)  # The frame in progress is under the old manifest.

            wait_until_served(service, service.source.show())
            with pytest.raises(AssertionError, match="the stream ended"):
                while True:
                    stream.next_block()

        with event_stream(service) as stream:
            events = {stream.next_event()["sensor"] for _ in range(2)}
            assert events == {"kept", "new"}


def test_with_no_archive_the_manifest_is_404_with_the_railroad_and_frames_are_served(
    tmp_path: Path,
) -> None:
    pattern = str(tmp_path / "{railroad}.r49")
    with live(
        tmp_path / "Gleis 49.r49", {}, railroad=lambda: "Gleis 49", pattern=pattern
    ) as service:
        response = service.get("/live/archive/manifest.json")
        assert response.status == 404
        assert response.headers.get_content_type() == "application/json"
        assert json.loads(response.read()) == {"error": "archive/none", "railroad": "Gleis 49"}
        assert service.get("/live/archive/first.png").status == 404

        frame = service.source.show(jpeg(WIDTH * 2, HEIGHT * 2))
        wait_until_served(service, frame)
        latest = read_latest(service)
        assert latest.fields["l1"] == {}
        assert latest.parts["frame"].body == frame.jpeg


@pytest.mark.parametrize("railroad", [None, lambda: None], ids=["no follower", "none loaded"])
def test_with_no_archive_and_no_railroad_known_the_railroad_is_null(
    tmp_path: Path, railroad: Callable[[], str | None] | None
) -> None:
    pattern = str(tmp_path / "{railroad}.r49")
    with live(tmp_path / "nothing-here", {}, railroad=railroad, pattern=pattern) as service:
        response = service.get("/live/archive/manifest.json")
        assert response.status == 404
        assert json.loads(response.read()) == {"error": "archive/none", "railroad": None}


@pytest.mark.parametrize("pattern", [None, "layout.r49"], ids=["no path", "no placeholder"])
def test_with_no_archive_and_a_path_without_the_railroad_the_answer_names_none(
    tmp_path: Path, pattern: str | None
) -> None:
    path = None if pattern is None else str(tmp_path / pattern)
    with live(tmp_path / "nothing-here", {}, railroad=lambda: "Gleis 49", pattern=path) as service:
        response = service.get("/live/archive/manifest.json")
        assert response.status == 404
        assert json.loads(response.read()) == {"error": "archive/none"}


def test_the_archive_served_is_the_one_the_loop_opened(tmp_path: Path) -> None:
    archive = folder_archive(tmp_path)
    with live(tmp_path / "nothing-here") as service:
        service.loop.open(archive)
        response = service.get("/live/archive/manifest.json")
        assert response.status == 200
        assert json.loads(response.read())["id"] == "archive-1"


def test_the_checker_passes_with_no_archive_with_the_camera_on_and_off(tmp_path: Path) -> None:
    with live(tmp_path / "nothing-here", {}, railroad=lambda: "Gleis 49") as service:
        assert check(service.base, timeout=5) == []  # Before any frame.

        wait_until_served(service, service.source.show(jpeg(WIDTH * 2, HEIGHT * 2)))
        assert check(service.base, timeout=5, save=True) == []

        service.source.state = "lost"
        assert check(service.base, timeout=5) == []
