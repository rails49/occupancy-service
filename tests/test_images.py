"""The two images, as built. Each test runs only when its environment variable names a built image:
the `images` workflow sets them for a release, with the commit it built them from. They need a
docker daemon."""

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
from fake_l1 import write_archive
from rails49_testing.ports import free_port

SERVICE = os.environ.get("OCCUPANCY_SERVICE_IMAGE")
PAGE = os.environ.get("OCCUPANCY_PAGE_IMAGE")
COMMIT = os.environ.get("OCCUPANCY_SERVICE_COMMIT")
ROOT = Path(__file__).resolve().parent.parent
CHECK_ARCHIVE = ROOT / "deploy" / "check.r49"
CHECK_WAIT = 60  # Seconds the release check waits before it looks: rails49/.github#71 decision 12.


def pinned_detector() -> str:
    """The rails49-detector version the service's Dockerfile pins."""
    text = (ROOT / "deploy" / "service.Dockerfile").read_text()
    found = re.search(r"^ARG DETECTOR_VERSION=(\S+)$", text, re.MULTILINE)
    assert found, "service.Dockerfile pins no detector"
    return found.group(1)


def run(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout


def rules(text: str) -> dict[str, dict[str, str]]:
    """Each path pattern of a `_headers` file, with the headers it sets."""
    found: dict[str, dict[str, str]] = {}
    pattern = ""
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            pattern = line.strip()
            found[pattern] = {}
        else:
            name, value = line.split(":", 1)
            found[pattern][name.strip()] = value.strip()
    return found


def test_rules_reads_a_headers_file() -> None:
    text = "# A comment.\n/*\n  A: one\n  B: two: three\n\n/x\n  C: four\n"
    assert rules(text) == {"/*": {"A": "one", "B": "two: three"}, "/x": {"C": "four"}}


@pytest.mark.skipif(SERVICE is None, reason="OCCUPANCY_SERVICE_IMAGE names no image")
def test_the_service_image_holds_the_pinned_detector() -> None:
    assert SERVICE is not None
    script = "import detector, importlib.metadata as m; print(m.version('rails49-detector'))"
    found = run("run", "--rm", "--entrypoint", "python", SERVICE, "-c", script).strip()
    assert found == pinned_detector()


@pytest.mark.skipif(SERVICE is None, reason="OCCUPANCY_SERVICE_IMAGE names no image")
def test_the_service_image_carries_the_detectors_license_and_source() -> None:
    assert SERVICE is not None
    doc = "/usr/share/doc/rails49-detector"
    text = run("run", "--rm", "--entrypoint", "cat", SERVICE, f"{doc}/LICENSE")
    assert text.split()[:4] == ["GNU", "AFFERO", "GENERAL", "PUBLIC"]
    source = run("run", "--rm", "--entrypoint", "cat", SERVICE, f"{doc}/SOURCE").strip()
    assert source == f"https://github.com/rails49/detector/tree/v{pinned_detector()}"


@pytest.mark.skipif(COMMIT is None, reason="OCCUPANCY_SERVICE_COMMIT names no commit")
@pytest.mark.parametrize("image", [SERVICE, PAGE], ids=["service", "page"])
def test_each_image_carries_its_commit(image: str | None) -> None:
    if image is None:
        pytest.skip("no image of this kind was built")
    labels = json.loads(run("inspect", "--format", "{{json .Config.Labels}}", image))
    assert labels["org.opencontainers.image.revision"] == COMMIT


@pytest.mark.skipif(SERVICE is None, reason="OCCUPANCY_SERVICE_IMAGE names no image")
def test_the_service_image_runs_the_command() -> None:
    assert SERVICE is not None
    assert "--archive" in run("run", "--rm", SERVICE, "--help")


@pytest.mark.skipif(SERVICE is None, reason="OCCUPANCY_SERVICE_IMAGE names no image")
def test_the_service_image_with_no_gpu_runs_openvino_on_the_cpu(tmp_path: Path) -> None:
    assert SERVICE is not None
    archive = write_archive(tmp_path / "archive", {"s1": (160, 120)})
    mount = f"type=bind,source={archive},target=/archive,readonly"
    flags = ("--archive=/archive", "--camera=/dev/null", "--broker=127.0.0.1:1")
    container = run("run", "--rm", "-d", "--mount", mount, SERVICE, *flags).strip()
    try:
        logs = ""
        for _ in range(600):
            logs = subprocess.run(
                ["docker", "logs", container], capture_output=True, text=True
            ).stderr
            if "the classifier runs on" in logs:
                break
            time.sleep(0.1)
        assert "the classifier runs on openvino CPU\n" in logs, logs
    finally:
        run("stop", container)


def latest(base: str) -> dict[str, Any]:
    """The JSON part of ``latest``, which is the first part."""
    with urllib.request.urlopen(f"{base}/latest", timeout=5) as response:
        body = response.read()
        boundary = response.headers.get_boundary()
    first = body.split(f"--{boundary}".encode())[1]
    return json.loads(first.partition(b"\r\n\r\n")[2])


@pytest.mark.skipif(SERVICE is None, reason="OCCUPANCY_SERVICE_IMAGE names no image")
def test_the_service_with_no_camera_and_the_check_archive_keeps_running() -> None:
    assert SERVICE is not None
    port = free_port()
    mount = f"type=bind,source={CHECK_ARCHIVE},target=/archive.r49"
    flags = (
        "--archive=/archive.r49",
        "--camera=/dev/v4l/by-id/no-camera",
        "--broker=127.0.0.1:1",
        "--address=0.0.0.0",
    )
    published = ("-p", f"127.0.0.1:{port}:8000")
    container = run("run", "--rm", "-d", "--mount", mount, *published, SERVICE, *flags).strip()
    try:
        time.sleep(CHECK_WAIT)
        state = json.loads(run("inspect", "--format", "{{json .State}}", container))
        assert state["Running"], run("logs", container)
        answer = latest(f"http://127.0.0.1:{port}/live")
        assert answer["camera"]["state"] == "lost"
        assert answer["l1"]
        assert all(e == {"state": "unknown", "reason": "no frames"} for e in answer["l1"].values())
    finally:
        run("stop", container)


@pytest.fixture
def page() -> Iterator[str]:
    """The page's container, on a free port of this host."""
    assert PAGE is not None
    port = free_port()
    container = run("run", "--rm", "-d", "-p", f"127.0.0.1:{port}:80", PAGE).strip()
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(base).close()
                break
            except OSError:
                time.sleep(0.1)
        yield base
    finally:
        run("stop", container)


def headers(url: str) -> Message:
    try:
        with urllib.request.urlopen(url) as answer:
            return answer.headers
    except urllib.error.HTTPError as error:
        return error.headers


@pytest.mark.skipif(PAGE is None, reason="OCCUPANCY_PAGE_IMAGE names no image")
def test_nginx_sets_the_headers_of_the_built_page(page: str) -> None:
    assert PAGE is not None
    built = rules(run("run", "--rm", "--entrypoint", "cat", PAGE, "/usr/share/nginx/html/_headers"))
    assert list(built) == ["/*"]
    for path in ("/", "/index.html", "/no-such-file.js"):
        answer = headers(page + path)
        assert {name: answer[name] for name in built["/*"]} == built["/*"], path
