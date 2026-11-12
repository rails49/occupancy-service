"""Creating an archive where there is none, over real HTTP, on the real L1 loop."""

import http.client
import io
import json
import os
import socket
import threading
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fake_l1 import FakeScorer, FakeSource, running, wait_for, write_archive

from occupancy_service.l1 import L1Loop, L1Settings
from occupancy_service.live_check import check
from occupancy_service.server import LiveServer, ServerSettings

CREATE = "/live/archive.r49"
MANIFEST = "/live/archive/manifest.json"
ONE_SENSOR = {"s1": (160.0, 120.0)}


@dataclass
class Service:
    server: LiveServer
    loop: L1Loop
    source: FakeSource
    folder: Path
    railroad: list[str | None] = field(default_factory=lambda: [None])

    def send(
        self, method: str, path: str, body: bytes | None = None, **headers: str
    ) -> tuple[int, http.client.HTTPMessage, bytes]:
        """One request on its own connection. ``_`` in a header's name stands for ``-``."""
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        try:
            connection.request(
                method, path, body, {key.replace("_", "-"): value for key, value in headers.items()}
            )
            response = connection.getresponse()
            return response.status, response.headers, response.read()
        finally:
            connection.close()

    def create(self, body: bytes, **headers: str) -> tuple[int, http.client.HTTPMessage, bytes]:
        return self.send("PUT", CREATE, body, **{"If_None_Match": "*", **headers})


@contextmanager
def serving(folder: Path, name: str, opened: str | None = None) -> Iterator[Service]:
    """A service whose archive is ``folder / name``, ``name`` perhaps with ``{railroad}`` in it,
    started on the archive at ``folder / opened``."""
    source = FakeSource()
    railroad: list[str | None] = [None]
    settings = L1Settings(None if opened is None else folder / opened)
    with running(L1Loop(settings, source, FakeScorer({"s1": 0.9}))) as loop:
        server = LiveServer(
            ServerSettings(port=0), loop, railroad=lambda: railroad[0], archive=str(folder / name)
        )
        server.start()
        try:
            yield Service(server, loop, source, folder, railroad)
        finally:
            server.stop()


def zipped(tmp_path: Path, files: dict[str, bytes] | None = None, **replaced: object) -> bytes:
    """A ``.r49`` zip of a one-sensor archive, with ``files`` added."""
    folder = write_archive(tmp_path / "source", ONE_SENSOR, **replaced)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.write(folder / "manifest.json", "manifest.json")
        for name, body in (files or {}).items():
            archive.writestr(name, body)
    return out.getvalue()


def files_in(folder: Path) -> set[str]:
    """Every file and folder there is, dot names too."""
    return {path.relative_to(folder).as_posix() for path in folder.rglob("*")}


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    archives = tmp_path / "archives"
    archives.mkdir()
    return archives


def test_a_create_puts_the_archive_in_place_and_the_loop_uses_it(
    tmp_path: Path, folder: Path
) -> None:
    body = zipped(tmp_path, {"photo.jpg": b"photo"}, images=[{"filename": "photo.jpg"}])
    with serving(folder, "layout.r49") as service:
        status, headers, _ = service.create(body)
        assert status == 204
        assert files_in(folder) == {
            "layout.r49",
            "layout.r49/manifest.json",
            "layout.r49/photo.jpg",
        }
        assert headers["ETag"] == service.send("GET", MANIFEST)[1]["ETag"]
        service.source.show()
        wait_for(lambda: set(service.loop.latest().l1) == {"s1"})


def test_a_create_into_a_railroad_with_no_archive_uses_its_name(
    tmp_path: Path, folder: Path
) -> None:
    with serving(folder, "{railroad}.r49") as service:
        service.railroad[0] = "Gleis 49"
        assert service.create(zipped(tmp_path))[0] == 204
        assert service.loop.archive == folder / "Gleis 49.r49"
        assert (folder / "Gleis 49.r49" / "manifest.json").is_file()


def test_with_no_railroad_loaded_a_create_is_409(tmp_path: Path, folder: Path) -> None:
    with serving(folder, "{railroad}.r49") as service:
        assert service.create(zipped(tmp_path))[0] == 409
        assert files_in(folder) == set()


def test_a_second_create_is_412_and_leaves_the_first(tmp_path: Path, folder: Path) -> None:
    with serving(folder, "layout.r49") as service:
        assert service.create(zipped(tmp_path))[0] == 204
        before = (folder / "layout.r49" / "manifest.json").read_bytes()
        assert service.create(zipped(tmp_path, layout_name="other"))[0] == 412
        assert (folder / "layout.r49" / "manifest.json").read_bytes() == before
        assert files_in(folder) == {"layout.r49", "layout.r49/manifest.json"}


def test_a_create_where_an_archive_was_opened_is_412(tmp_path: Path, folder: Path) -> None:
    write_archive(folder / "layout.r49", ONE_SENSOR)
    with serving(folder, "layout.r49", opened="layout.r49") as service:
        assert service.create(zipped(tmp_path))[0] == 412


def test_of_two_creates_at_once_one_succeeds(tmp_path: Path, folder: Path) -> None:
    body = zipped(tmp_path)
    with serving(folder, "layout.r49") as service:
        barrier = threading.Barrier(2)
        statuses: list[int] = []

        def create() -> None:
            barrier.wait()
            statuses.append(service.create(body)[0])

        threads = [threading.Thread(target=create) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(statuses) == [204, 412]
        assert files_in(folder) == {"layout.r49", "layout.r49/manifest.json"}


def test_a_create_with_no_if_none_match_star_is_428(tmp_path: Path, folder: Path) -> None:
    with serving(folder, "layout.r49") as service:
        assert service.send("PUT", CREATE, zipped(tmp_path))[0] == 428
        for other in ('"x"', "W/*", "*, *"):
            assert service.send("PUT", CREATE, zipped(tmp_path), If_None_Match=other)[0] == 428
        assert files_in(folder) == set()


@pytest.mark.parametrize(
    ("origin", "status"),
    [("http://elsewhere.example:8000", 403), ("null", 403), ("http://localhost:5173", 204)],
)
def test_the_origin_rule_holds_for_a_create(
    tmp_path: Path, folder: Path, origin: str, status: int
) -> None:
    with serving(folder, "layout.r49") as service:
        headers = {"Host": "layout.example:8000", "Origin": origin}
        assert service.create(zipped(tmp_path), **headers)[0] == status


@pytest.mark.parametrize(("exists", "status"), [(False, b"411"), (True, b"412")])
def test_a_create_with_no_content_length_is_411_after_the_412(
    folder: Path, exists: bool, status: bytes
) -> None:
    if exists:
        write_archive(folder / "layout.r49", ONE_SENSOR)
    before = files_in(folder)
    head = f"PUT {CREATE} HTTP/1.1\r\nHost: test\r\nIf-None-Match: *\r\n"
    head += "Transfer-Encoding: chunked\r\n\r\n"
    with serving(folder, "layout.r49") as service:
        with socket.create_connection(("127.0.0.1", service.server.port), timeout=10) as sock:
            sock.sendall(head.encode())
            answer = b""
            while chunk := sock.recv(65536):
                answer += chunk
    assert answer.startswith(b"HTTP/1.1 " + status + b" ")
    assert files_in(folder) == before


@pytest.fixture
def read_only(folder: Path) -> Iterator[Path]:
    if os.geteuid() == 0:
        pytest.skip("root writes to a read-only folder")
    folder.chmod(0o555)
    yield folder
    folder.chmod(0o755)  # So that pytest can clean it up.


def test_with_a_folder_that_cannot_be_written_a_create_is_405(
    tmp_path: Path, read_only: Path
) -> None:
    with serving(read_only, "layout.r49") as service:
        status, headers, _ = service.create(zipped(tmp_path), Origin="http://elsewhere.example")
        assert (status, headers["Allow"]) == (405, "GET")


def test_with_no_folder_a_create_is_405(tmp_path: Path) -> None:
    with serving(tmp_path / "absent", "layout.r49") as service:
        assert service.create(zipped(tmp_path))[0] == 405


def test_a_service_given_no_archive_path_offers_no_create(tmp_path: Path) -> None:
    with running(L1Loop(L1Settings(None), FakeSource(), FakeScorer({}))) as loop:
        server = LiveServer(ServerSettings(port=0), loop)
        server.start()
        try:
            service = Service(server, loop, FakeSource(), tmp_path)
            assert service.create(zipped(tmp_path))[0] == 405
        finally:
            server.stop()


def no_manifest(tmp_path: Path) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("other.json", b"{}")
    return out.getvalue()


@pytest.mark.parametrize(
    ("body", "rule"),
    [
        (lambda _: b"not a zip", "archive/not-zip"),
        (lambda _: b"", "archive/empty"),
        (no_manifest, "archive/no-manifest"),
        (lambda tmp: zipped(tmp, version=5), "manifest/version"),
        (lambda tmp: zipped(tmp, surprise=1), "manifest/unknown-key"),
        (lambda tmp: zipped(tmp, images=[{"filename": "photo.jpg"}]), "images/missing"),
        (
            lambda tmp: zipped(tmp, {".hidden.jpg": b"x"}, images=[{"filename": ".hidden.jpg"}]),
            "images/missing",
        ),
    ],
)
def test_a_refused_zip_is_422_with_its_rule_and_leaves_nothing(
    tmp_path: Path, folder: Path, body: Callable[[Path], bytes], rule: str
) -> None:
    with serving(folder, "layout.r49") as service:
        status, headers, answer = service.create(body(tmp_path))
        assert (status, json.loads(answer)) == (422, {"error": rule})
        assert headers["Content-Type"] == "application/json"
        assert files_in(folder) == set()
        assert service.loop.archive is None


def test_the_checks_come_in_their_order(tmp_path: Path, folder: Path) -> None:
    with serving(folder, "{railroad}.r49") as service:
        foreign = "http://elsewhere.example"
        assert service.send("PUT", CREATE, b"x", Origin=foreign)[0] == 403
        assert service.send("PUT", CREATE, b"x")[0] == 428
        assert service.create(b"x")[0] == 409
        service.railroad[0] = "Gleis 49"
        write_archive(folder / "Gleis 49.r49", ONE_SENSOR)
        assert service.create(b"x")[0] == 412


def test_a_zip_entry_that_would_leave_the_folder_is_not_unpacked(
    tmp_path: Path, folder: Path
) -> None:
    body = zipped(tmp_path, {"../outside.jpg": b"x", ".git/config": b"x", "ok.jpg": b"x"})
    with serving(folder, "layout.r49") as service:
        assert service.create(body)[0] == 204
    assert files_in(folder.parent) >= {"archives/layout.r49/ok.jpg"}
    assert not (folder / "outside.jpg").exists()
    assert not (folder / "layout.r49" / ".git").exists()


@pytest.mark.parametrize("target", ["fsync", "rename"])
def test_a_failed_create_is_500_and_leaves_nothing(
    tmp_path: Path, folder: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    def fail(*_: object) -> None:
        raise OSError("no space left")

    with serving(folder, "layout.r49") as service:
        with monkeypatch.context() as patch:
            patch.setattr(os, target, fail)
            assert service.create(zipped(tmp_path))[0] == 500
        assert files_in(folder) == set()
        assert service.loop.archive is None


def test_a_body_cut_short_creates_nothing(tmp_path: Path, folder: Path) -> None:
    with serving(folder, "layout.r49") as service:
        connection = http.client.HTTPConnection("127.0.0.1", service.server.port, timeout=10)
        connection.putrequest("PUT", CREATE)
        connection.putheader("If-None-Match", "*")
        connection.putheader("Content-Length", "100000")
        connection.endheaders(zipped(tmp_path)[:50])
        connection.close()
        wait_for(lambda: service.create(zipped(tmp_path))[0] == 204)
    assert files_in(folder) == {"layout.r49", "layout.r49/manifest.json"}


@pytest.mark.parametrize("name", ["layout.r49", "{railroad}.r49"])
def test_the_checker_with_save_passes_with_no_archive_and_creates_nothing(
    folder: Path, name: str
) -> None:
    with serving(folder, name) as service:
        service.source.show()
        assert check(f"http://127.0.0.1:{service.server.port}/live", timeout=5, save=True) == []
    assert files_in(folder) == set()


def test_the_checker_with_save_passes_on_a_created_archive(tmp_path: Path, folder: Path) -> None:
    with serving(folder, "{railroad}.r49") as service:
        service.railroad[0] = "Gleis 49"
        assert service.create(zipped(tmp_path))[0] == 204
        frame = service.source.show()
        wait_for(lambda: service.loop.latest().frame == frame)
        assert check(f"http://127.0.0.1:{service.server.port}/live", timeout=5, save=True) == []
