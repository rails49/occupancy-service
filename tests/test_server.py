"""The server over real HTTP, on the real L1 loop with a fake capture source and scorer."""

import http.client
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fake_l1 import FakeScorer, FakeSource, running, write_archive

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
