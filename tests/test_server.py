"""The server over real HTTP, on the real L1 loop with a fake capture source and scorer."""

import email.parser
import http.client
import io
import json
import threading
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
