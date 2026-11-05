"""The server over real HTTP, on the real L1 loop with a fake capture source and scorer."""

import email.parser
import http.client
import io
import json
import socket
import threading
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fake_l1 import HEIGHT, WIDTH, FakeScorer, FakeSource, jpeg, running, wait_for, write_archive
from PIL import Image

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
def live(archive: Path, scores: dict[str, float] | None = None, **settings: Any) -> Iterator[Live]:
    source = FakeSource()
    scorer = FakeScorer(scores if scores is not None else {"s1": 0.9})
    server_settings = ServerSettings(address="127.0.0.1", port=0, **settings)
    with running(L1Loop(L1Settings(archive), source, scorer)) as loop:
        server = LiveServer(server_settings, archive, loop)
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
        server = LiveServer(ServerSettings(port=0), archive, loop)
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
