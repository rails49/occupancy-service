"""The two images, as built. Each test runs only when its environment variable names a built image:
the `images` workflow sets them for a release. They need a docker daemon."""

import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from email.message import Message
from pathlib import Path

import pytest
from fake_l1 import write_archive

SERVICE = os.environ.get("OCCUPANCY_SERVICE_IMAGE")
PAGE = os.environ.get("OCCUPANCY_PAGE_IMAGE")


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
def test_the_service_image_holds_no_detector() -> None:
    assert SERVICE is not None
    script = "import importlib.util; print(importlib.util.find_spec('detector'))"
    assert run("run", "--rm", "--entrypoint", "python", SERVICE, "-c", script).strip() == "None"


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


@pytest.fixture
def page() -> Iterator[str]:
    """The page's container, on a free port of this host."""
    assert PAGE is not None
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
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
