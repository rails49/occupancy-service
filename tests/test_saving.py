"""Saving back to a folder archive, over real HTTP, on the real L1 loop with a fake source."""

import hashlib
import http.client
import json
import os
import socket
import threading
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import r49
from fake_l1 import FakeScorer, FakeSource, running, wait_for, write_archive

from occupancy_service.l1 import L1Loop, L1Settings
from occupancy_service.live_check import check
from occupancy_service.server import LiveServer, ServerSettings

ONE_SENSOR = {"s1": (160.0, 120.0)}
MANIFEST = "/live/archive/manifest.json"
PHOTO = "/live/archive/photo.jpg"


@dataclass
class Service:
    server: LiveServer
    loop: L1Loop
    source: FakeSource
    folder: Path

    def send(
        self, method: str, path: str, body: bytes | None = None, **headers: str
    ) -> tuple[int, http.client.HTTPMessage, bytes]:
        """One request on its own connection. ``If_Match`` stands for the ``If-Match`` header."""
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        try:
            connection.request(
                method,
                path,
                body,
                {key.replace("_", "-"): value for key, value in headers.items()},
            )
            response = connection.getresponse()
            return response.status, response.headers, response.read()
        finally:
            connection.close()

    def etag(self) -> str:
        status, headers, _ = self.send("GET", MANIFEST)
        assert status == 200
        return headers["ETag"]

    def manifest(self) -> dict[str, Any]:
        return json.loads((self.folder / "manifest.json").read_bytes())

    def put_manifest(self, manifest: dict[str, Any]) -> tuple[int, http.client.HTTPMessage, bytes]:
        return self.send("PUT", MANIFEST, json.dumps(manifest).encode(), If_Match=self.etag())

    def refusal(self, manifest: dict[str, Any]) -> tuple[int, Any]:
        status, _, body = self.put_manifest(manifest)
        return status, json.loads(body)["error"] if body else None

    def served(self) -> Any:
        """Show a new frame, and wait for the result of it."""
        frame = self.source.show()
        wait_for(lambda: self.loop.latest().frame == frame)
        return self.loop.latest()


@contextmanager
def serving(archive: Path, scores: dict[str, float] | None = None) -> Iterator[Service]:
    source = FakeSource()
    scorer = FakeScorer(scores if scores is not None else {"s1": 0.9})
    with running(L1Loop(L1Settings(archive), source, scorer)) as loop:
        server = LiveServer(ServerSettings(port=0), archive, loop)
        server.start()
        try:
            yield Service(server, loop, source, archive)
        finally:
            server.stop()


@pytest.fixture
def service(tmp_path: Path) -> Iterator[Service]:
    folder = write_archive(tmp_path / "archive", ONE_SENSOR)
    (folder / "photo.jpg").write_bytes(b"photo bytes")
    with serving(folder) as running_service:
        yield running_service


def files_in(folder: Path) -> set[str]:
    """Every file there is, dot names too, as a reader of the archive would not show them."""
    return {path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file()}


def test_the_manifest_has_a_strong_etag_of_its_bytes(service: Service) -> None:
    digest = hashlib.sha256((service.folder / "manifest.json").read_bytes()).hexdigest()
    assert service.etag() == f'"{digest}"'


def test_only_the_manifest_has_an_etag(service: Service) -> None:
    assert service.send("GET", PHOTO)[1]["ETag"] is None


def test_a_pages_sequence_leaves_a_folder_with_no_image_mismatches(service: Service) -> None:
    # The fixture's photo.jpg starts as an orphan. The page names it, then replaces it.
    new_image = "/live/archive/new%20folder/deep/photo%202.jpg"
    assert service.send("PUT", new_image, b"new", If_Match=service.etag())[0] == 204
    assert (service.folder / "new folder/deep/photo 2.jpg").read_bytes() == b"new"

    manifest = service.manifest()
    manifest["images"] = [{"filename": "new folder/deep/photo 2.jpg"}]
    status, headers, _ = service.put_manifest(manifest)
    assert status == 204
    assert headers["ETag"] == service.etag()

    assert service.send("DELETE", PHOTO, If_Match=service.etag())[0] == 204
    assert not (service.folder / "photo.jpg").exists()

    assert r49.load(service.folder).image_mismatches() == []
    assert files_in(service.folder) == {"manifest.json", "new folder/deep/photo 2.jpg"}


def test_an_image_already_there_is_replaced(service: Service) -> None:
    assert service.send("PUT", PHOTO, b"again", If_Match=service.etag())[0] == 204
    assert (service.folder / "photo.jpg").read_bytes() == b"again"


def test_a_saved_image_is_served(service: Service) -> None:
    service.send("PUT", "/live/archive/nested/new.jpg", b"new", If_Match=service.etag())
    status, _, body = service.send("GET", "/live/archive/nested/new.jpg")
    assert (status, body) == (200, b"new")


def test_a_refused_write_answers_on_a_connection_that_stays_usable(service: Service) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", service.server.port, timeout=10)
    connection.request("PUT", PHOTO, b"x" * 200_000, {"If-Match": '"stale"'})
    response = connection.getresponse()
    assert response.status == 412
    response.read()
    connection.request("GET", PHOTO)
    assert connection.getresponse().read() == b"photo bytes"
    connection.close()


@pytest.mark.parametrize(
    ("method", "path"),
    [("PUT", PHOTO), ("PUT", MANIFEST), ("DELETE", PHOTO), ("DELETE", MANIFEST)],
)
def test_a_write_with_no_if_match_is_428_and_any_other_value_412(
    service: Service, method: str, path: str
) -> None:
    before = files_in(service.folder)
    body = b"x" if method == "PUT" else None
    assert service.send(method, path, body)[0] == 428
    for stale in ('"stale"', "*", f"W/{service.etag()}", service.etag().strip('"')):
        assert service.send(method, path, body, If_Match=stale)[0] == 412
    assert files_in(service.folder) == before
    assert (service.folder / "photo.jpg").read_bytes() == b"photo bytes"


def test_a_save_based_on_an_older_copy_is_refused(service: Service) -> None:
    old = service.etag()
    assert service.put_manifest(service.manifest() | {"images": []})[0] == 204
    assert service.send("PUT", PHOTO, b"x", If_Match=old)[0] == 412
    assert service.send("DELETE", PHOTO, If_Match=old)[0] == 412


def test_a_hand_edit_refuses_a_page_holding_the_earlier_copy_and_waits_for_a_restart(
    service: Service,
) -> None:
    old = service.etag()
    write_archive(service.folder, {"hand": (160.0, 120.0)})
    assert service.send("PUT", PHOTO, b"x", If_Match=old)[0] == 412
    assert set(service.served().l1) == {"s1"}


@pytest.mark.parametrize(
    "path",
    [
        "/live/archive/../outside.jpg",
        "/live/archive/%2e%2e/outside.jpg",
        "/live/archive/nested/%2E%2E%2F%2E%2E%2Foutside.jpg",
        "/live/archive//abs.jpg",
        "/live/archive/nested//x.jpg",
        "/live/archive/",
        "/live/archive/nested/",
        "/live/archive/.hidden",
        "/live/archive/%2ehidden",
        "/live/archive/.git/config",
        "/live/archive/nested/.hidden/x.jpg",
        "/live/archive/photo.jpg%00",
    ],
)
@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_a_name_refused_or_with_a_dot_segment_is_404(
    service: Service, method: str, path: str
) -> None:
    before = files_in(service.folder.parent)
    body = b"x" if method == "PUT" else None
    assert service.send(method, path, body, If_Match=service.etag())[0] == 404
    assert files_in(service.folder.parent) == before


def test_outside_the_archive_a_write_is_404(service: Service) -> None:
    for path in ("/live/latest", "/live/nothing", "/archive/photo.jpg"):
        assert service.send("PUT", path, b"x", If_Match=service.etag())[0] == 404


@pytest.mark.parametrize("path", [PHOTO, MANIFEST])
def test_a_body_with_no_content_length_is_411(service: Service, path: str) -> None:
    head = f"PUT {path} HTTP/1.1\r\nHost: test\r\nIf-Match: {service.etag()}\r\n"
    head += "Transfer-Encoding: chunked\r\n\r\n"
    # Only the head: the answer closes the connection, as the end of such a body cannot be told.
    with socket.create_connection(("127.0.0.1", service.server.port), timeout=10) as connection:
        connection.sendall(head.encode())
        answer = b""
        while chunk := connection.recv(65536):
            answer += chunk
    assert answer.startswith(b"HTTP/1.1 411 ")
    assert b"\r\nConnection: close\r\n" in answer
    assert (service.folder / "photo.jpg").read_bytes() == b"photo bytes"


@pytest.mark.parametrize("length", ["-1", "many", "1_0"])
def test_a_content_length_that_is_no_length_is_411(service: Service, length: str) -> None:
    status, _, _ = service.send("PUT", PHOTO, None, If_Match=service.etag(), Content_Length=length)
    assert status == 411


def test_a_folder_in_the_way_of_an_image_is_409(service: Service) -> None:
    (service.folder / "nested").mkdir()
    (service.folder / "nested" / "photo.jpg").write_bytes(b"nested")
    before = files_in(service.folder)
    assert service.send("PUT", "/live/archive/nested", b"x", If_Match=service.etag())[0] == 409
    assert service.send("PUT", "/live/archive/photo.jpg/x", b"x", If_Match=service.etag())[0] == 409
    assert files_in(service.folder) == before


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ({"version": 3}, "manifest/version"),
        ({"surprise": 1}, "manifest/unknown-key"),
        ({"layout": "no"}, "manifest/schema"),
        ({"images": [{"filename": "../up.jpg"}]}, "manifest/filename"),
        ({"images": [{"filename": "not-there.jpg"}]}, "images/missing"),
        ({"images": [{"filename": ".hidden.jpg"}]}, "images/missing"),
        ({"id": "another"}, "manifest/id"),
    ],
)
def test_a_manifest_that_is_refused_is_422_with_its_rule(
    service: Service, change: dict[str, Any], rule: str
) -> None:
    (service.folder / ".hidden.jpg").write_bytes(b"a reader ignores this")
    before = (service.folder / "manifest.json").read_bytes()
    status, error = service.refusal(service.manifest() | change)
    assert (status, error) == (422, rule)
    assert (service.folder / "manifest.json").read_bytes() == before


def test_a_manifest_that_is_not_json_is_422(service: Service) -> None:
    status, headers, body = service.send("PUT", MANIFEST, b"not json", If_Match=service.etag())
    assert (status, json.loads(body)) == (422, {"error": "manifest/not-json"})
    assert headers["Content-Type"] == "application/json"


def test_a_manifest_that_drops_its_id_is_422(service: Service) -> None:
    manifest = service.manifest()
    del manifest["id"]
    assert service.refusal(manifest) == (422, "manifest/id")


def test_a_named_file_or_the_manifest_cannot_be_deleted_and_an_absent_file_is_404(
    service: Service,
) -> None:
    manifest = service.manifest() | {"images": [{"filename": "photo.jpg"}]}
    assert service.put_manifest(manifest)[0] == 204
    assert service.send("DELETE", PHOTO, If_Match=service.etag())[0] == 409
    assert service.send("DELETE", MANIFEST, If_Match=service.etag())[0] == 409
    assert (service.folder / "photo.jpg").exists()
    assert (service.folder / "manifest.json").exists()

    absent = "/live/archive/not-there.jpg"
    assert service.send("DELETE", absent, If_Match=service.etag())[0] == 404


def test_a_folder_is_not_a_file_to_delete(service: Service) -> None:
    (service.folder / "nested").mkdir()
    assert service.send("DELETE", "/live/archive/nested", If_Match=service.etag())[0] == 404
    assert (service.folder / "nested").is_dir()


def save_at_once(service: Service, etag: str, bodies: list[bytes]) -> list[int]:
    """The statuses of saving each of ``bodies`` from ``etag``, all at one moment."""
    barrier = threading.Barrier(len(bodies))
    statuses: list[int] = []

    def save(body: bytes) -> None:
        barrier.wait()
        statuses.append(service.send("PUT", MANIFEST, body, If_Match=etag)[0])

    threads = [threading.Thread(target=save, args=(body,)) for body in bodies]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return statuses


def test_of_two_saves_from_one_copy_one_succeeds(service: Service) -> None:
    for round in range(5):
        bodies = [
            json.dumps(
                service.manifest()
                | {"layout": service.manifest()["layout"] | {"name": f"{who} {round}"}}
            ).encode()
            for who in ("one", "two")
        ]
        assert sorted(save_at_once(service, service.etag(), bodies)) == [204, 412]


@pytest.mark.parametrize("target", ["fsync", "replace"])
def test_a_failed_manifest_write_leaves_the_old_manifest_and_no_orphan(
    service: Service, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    before = (service.folder / "manifest.json").read_bytes()
    manifest = service.manifest() | {"images": [{"filename": "photo.jpg"}]}
    etag = service.etag()

    def fail(*_: Any) -> None:
        raise OSError("no space left")

    with monkeypatch.context() as patch:
        patch.setattr(os, target, fail)
        status, _, _ = service.send("PUT", MANIFEST, json.dumps(manifest).encode(), If_Match=etag)

    assert status == 500
    assert (service.folder / "manifest.json").read_bytes() == before
    assert files_in(service.folder) == {"manifest.json", "photo.jpg"}
    assert service.etag() == etag


@pytest.mark.parametrize("target", ["fsync", "replace"])
def test_a_failed_image_write_leaves_the_old_image(
    service: Service, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    def fail(*_: Any) -> None:
        raise OSError("no space left")

    etag = service.etag()
    with monkeypatch.context() as patch:
        patch.setattr(os, target, fail)
        status, _, _ = service.send("PUT", PHOTO, b"new", If_Match=etag)

    assert status == 500
    assert (service.folder / "photo.jpg").read_bytes() == b"photo bytes"
    assert files_in(service.folder) == {"manifest.json", "photo.jpg"}


def test_a_body_cut_short_stores_nothing(service: Service) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", service.server.port, timeout=10)
    connection.putrequest("PUT", PHOTO)
    connection.putheader("If-Match", service.etag())
    connection.putheader("Content-Length", "100")
    connection.endheaders(b"short")
    connection.close()
    wait_for(lambda: files_in(service.folder) == {"manifest.json", "photo.jpg"})
    assert (service.folder / "photo.jpg").read_bytes() == b"photo bytes"


NAMED = "layout.example:8000"


@pytest.mark.parametrize(
    ("origin", "status"),
    [
        (f"http://{NAMED}", 204),
        (f"https://{NAMED}", 204),
        ("http://elsewhere.example:8000", 403),
        ("http://layout.example:9000", 403),
        ("http://layout.example.elsewhere.example:8000", 403),
        ("null", 403),
        ("http://[", 403),
        ("http://localhost:5173", 204),
        ("http://127.0.0.1:5173", 204),
        ("http://[::1]:5173", 204),
        (None, 204),
    ],
)
@pytest.mark.parametrize(("method", "path"), [("PUT", PHOTO), ("PUT", MANIFEST), ("DELETE", PHOTO)])
def test_the_origin_rule_on_a_named_host(
    service: Service, method: str, path: str, origin: str | None, status: int
) -> None:
    body = {
        ("PUT", PHOTO): b"new",
        ("PUT", MANIFEST): (service.folder / "manifest.json").read_bytes(),
    }.get((method, path))
    headers = {"Host": NAMED, "If_Match": service.etag()}
    if origin is not None:
        headers["Origin"] = origin
    before = (service.folder / "photo.jpg").read_bytes()
    assert service.send(method, path, body, **headers)[0] == status
    if status == 403:
        assert (service.folder / "photo.jpg").read_bytes() == before


@pytest.fixture
def read_only_folder(tmp_path: Path) -> Iterator[Path]:
    if os.geteuid() == 0:
        pytest.skip("root writes to a read-only folder")
    folder = write_archive(tmp_path / "archive", ONE_SENSOR)
    folder.chmod(0o555)
    yield folder
    folder.chmod(0o755)  # So that pytest can clean it up.


@pytest.fixture
def zipped(tmp_path: Path) -> Path:
    folder = write_archive(tmp_path / "folder", ONE_SENSOR)
    archive = tmp_path / "archive.r49"
    with zipfile.ZipFile(archive, "w") as out:
        out.write(folder / "manifest.json", "manifest.json")
    return archive


@pytest.fixture(params=["read_only_folder", "zipped"])
def unwritable(request: pytest.FixtureRequest) -> Path:
    return request.getfixturevalue(request.param)


def test_with_no_way_to_write_there_is_no_etag_and_a_write_is_405(unwritable: Path) -> None:
    with serving(unwritable) as service:
        assert service.send("GET", MANIFEST)[1]["ETag"] is None
        for method, path in (("PUT", PHOTO), ("PUT", MANIFEST), ("DELETE", PHOTO)):
            status, headers, _ = service.send(method, path, b"x" if method == "PUT" else None)
            assert status == 405
            assert headers["Allow"] == "GET"


def test_after_a_save_the_next_result_has_the_new_sensors(tmp_path: Path) -> None:
    folder = write_archive(tmp_path / "archive", {"kept": (160.0, 120.0), "old": (170.0, 120.0)})
    with serving(folder, {"kept": 0.9, "old": 0.9, "new": 0.9}) as service:
        assert service.served().l1["kept"].state == "occupied"
        changes = service.loop.subscribe()

        manifest = service.manifest()
        manifest["layout"]["sensors"] = [
            {"id": "kept", "x": 160.0, "y": 120.0},
            {"id": "new", "x": 180.0, "y": 120.0},
        ]
        assert service.put_manifest(manifest)[0] == 204

        result = service.served()
        assert set(result.l1) == {"kept", "new"}
        assert result.l1["kept"].state == "occupied"
        change = changes.get(5)
        assert change is not None and change.sensor == "new"
        assert changes.get(0.05) is None  # So no unknown for the sensor that stayed.


def test_a_saved_manifest_that_gains_an_id_ends_archive_has_no_id(tmp_path: Path) -> None:
    folder = write_archive(tmp_path / "archive", ONE_SENSOR, id=None)
    with serving(folder) as service:
        assert service.served().l1["s1"].reason == "archive has no id"
        assert service.put_manifest(service.manifest() | {"id": "minted"})[0] == 204
        result = service.served()
        assert result.l1["s1"].state == "occupied"
        assert result.archive_id == "minted"


def test_the_checker_with_save_passes_on_a_folder_and_leaves_it_as_it_was(
    service: Service,
) -> None:
    service.served()
    before = (service.folder / "manifest.json").read_bytes()
    base = f"http://127.0.0.1:{service.server.port}/live"
    assert check(base, timeout=5, save=True) == []
    assert (service.folder / "manifest.json").read_bytes() == before


def test_the_checker_with_save_passes_with_no_way_to_write(unwritable: Path) -> None:
    with serving(unwritable) as service:
        service.served()
        base = f"http://127.0.0.1:{service.server.port}/live"
        assert check(base, timeout=5, save=True) == []
