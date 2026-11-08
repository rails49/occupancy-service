"""``python -m occupancy_service`` as a real child process, with a fake ``ffmpeg`` on the ``PATH``
and a real ``mosquitto`` where a broker is named."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fake_ffmpeg import HANG, FakeFfmpeg, exits, install, stream
from fake_l1 import HEIGHT, WIDTH, jpeg, wait_for, write_archive
from mqtt_broker import Broker, Received, Subscriber, mosquitto

from occupancy_service.live_check import check

SENSORS = {"s1": (160.0, 120.0), "s2": (200.0, 120.0)}
OWN = "occupancy/archive-1/sensor/"
CONTROL = "tc49/layout/sensor"
START_WAIT = 60  # Seconds for a child to answer: it imports the engine and loads the model.
STOP_WAIT = 30  # Seconds for a child to exit once signalled.


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@dataclass
class Service:
    """The child process, and what a test reads from it."""

    process: subprocess.Popen[bytes]
    port: int
    stderr: Path

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/live"

    def latest(self) -> dict[str, Any]:
        """The JSON part of ``latest``, which is the first part."""
        with urllib.request.urlopen(f"{self.base}/latest", timeout=5) as response:
            body = response.read()
            boundary = response.headers.get_boundary()
        first = body.split(f"--{boundary}".encode())[1]
        return json.loads(first.partition(b"\r\n\r\n")[2])

    def answers(self) -> bool:
        """Whether ``latest`` answers. A child that has exited fails the test."""
        code = self.process.poll()
        if code is not None:
            raise AssertionError(f"exited {code}:\n{self.stderr.read_text()}")
        try:
            self.latest()
        except OSError:
            return False
        return True


def command(archive: Path | str, broker_port: int, port: int, *extra: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "occupancy_service",
        "--archive",
        str(archive),
        "--camera",
        "/dev/video7",
        "--broker",
        f"127.0.0.1:{broker_port}",
        "--port",
        str(port),
        *extra,
    ]


def child_env(fake: FakeFfmpeg) -> dict[str, str]:
    return os.environ | {"PATH": f"{fake.directory}{os.pathsep}{os.environ['PATH']}"}


@contextmanager
def started(
    tmp_path: Path, fake: FakeFfmpeg, archive: Path, broker_port: int | None = None, *extra: str
) -> Iterator[Service]:
    """The child, once ``latest`` answers. With no ``broker_port``, no broker is listening."""
    port = free_port()
    stderr = tmp_path / "stderr.txt"
    with stderr.open("wb") as log:
        process = subprocess.Popen(
            command(archive, broker_port or free_port(), port, *extra),
            env=child_env(fake),
            stderr=log,
        )
    service = Service(process, port, stderr)
    try:
        wait_for(service.answers, START_WAIT)
        yield service
    finally:
        # A clean stop: a kill would leave behind an ffmpeg that is not writing to its pipe.
        process.terminate()
        try:
            process.wait(STOP_WAIT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(STOP_WAIT)


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    return write_archive(tmp_path / "archive", SENSORS)


@pytest.fixture
def camera(tmp_path: Path) -> FakeFfmpeg:
    """A camera that sends frames at the archive's size."""
    folder = tmp_path / "bin"
    folder.mkdir()
    return install(folder, [stream(jpeg(WIDTH, HEIGHT))])


def test_conforms_to_the_live_contract(tmp_path: Path, camera: FakeFfmpeg, archive: Path) -> None:
    with started(tmp_path, camera, archive) as service:
        wait_for(lambda: service.latest().get("frame_id"), 30)
        assert check(service.base, timeout=5) == []


def test_ffmpeg_is_given_the_size_in_the_manifest_and_the_flags(
    tmp_path: Path, camera: FakeFfmpeg
) -> None:
    resolution = {"width": 800, "height": 600}
    archive = write_archive(tmp_path / "archive", SENSORS, camera={"resolution": resolution})
    with started(tmp_path, camera, archive, None, "--frame-rate", "12.5"):
        argv = wait_for(camera.starts)[0].argv
    assert argv[argv.index("-video_size") + 1] == "800x600"
    assert argv[argv.index("-i") + 1] == "/dev/video7"
    assert argv[argv.index("-framerate") + 1] == "12.5"


def stop(service: Service, number: signal.Signals = signal.SIGTERM) -> int:
    service.process.send_signal(number)
    return service.process.wait(STOP_WAIT)


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_no_frames_from_a_camera_that_never_gives_one(tmp_path: Path, archive: Path) -> None:
    (tmp_path / "bin").mkdir()
    fake = install(tmp_path / "bin", [HANG])
    with started(tmp_path, fake, archive) as service:
        wait_for(fake.starts)
        answer = service.latest()
    assert answer["frame_id"] is None
    assert answer["l1"] == {id: {"state": "unknown", "reason": "no frames"} for id in SENSORS}


def test_an_absent_camera_does_not_refuse_the_process(tmp_path: Path, archive: Path) -> None:
    (tmp_path / "bin").mkdir()
    fake = install(tmp_path / "bin", [exits(1)])
    with started(tmp_path, fake, archive) as service:
        wait_for(lambda: len(fake.starts()) >= 2)  # It goes on trying.
        assert service.latest()["l1"]["s1"] == {"state": "unknown", "reason": "no frames"}


def test_a_model_that_does_not_load_does_not_refuse_the_process(
    tmp_path: Path, camera: FakeFfmpeg, archive: Path
) -> None:
    with started(tmp_path, camera, archive, None, "--device", "NPU") as service:
        assert service.latest()["l1"]["s1"] == {"state": "unknown", "reason": "no model"}


FAKE_PACKAGE = """
from types import SimpleNamespace


class Detector:
    def __init__(self, model, engine, device):
        if not model.exists():
            raise FileNotFoundError(model)
        self.engine = engine

    def detect(self, frame, dpt):
        height, width = frame.shape[:2]
        found = dict(length=50, width=30, angle=0, cls=self.engine, confidence=0.75)
        return [SimpleNamespace(x=width / 2, y=height / 2, **found)]
"""


def with_fake_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a ``detector`` module on the child's path, as rails49-detector would be."""
    folder = tmp_path / "site"
    folder.mkdir()
    (folder / "detector.py").write_text(FAKE_PACKAGE)
    monkeypatch.setenv("PYTHONPATH", str(folder))


def test_the_detector_model_flags_give_l0(
    tmp_path: Path, camera: FakeFfmpeg, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_fake_package(tmp_path, monkeypatch)
    model = tmp_path / "model.onnx"
    model.write_bytes(b"")
    flags = ("--detector-model", str(model), "--detector-engine", "fake", "--detector-device", "X")
    with started(tmp_path, camera, archive, None, *flags) as service:
        answer = wait_for(lambda: (latest := service.latest()).get("l0") and latest, 30)
        assert check(service.base, timeout=5) == []
    assert answer["detector"] == {"state": "ok"}
    assert answer["l0"]["detections"][0]["class"] == "fake"


def test_with_no_detector_model_the_installed_package_is_not_used(
    tmp_path: Path, camera: FakeFfmpeg, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_fake_package(tmp_path, monkeypatch)
    with started(tmp_path, camera, archive) as service:
        answer = wait_for(lambda: (latest := service.latest()).get("frame_id") and latest, 30)
    assert "detector" not in answer
    assert "l0" not in answer


def test_a_detector_model_that_does_not_load_does_not_refuse_the_process(
    tmp_path: Path, camera: FakeFfmpeg, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_fake_package(tmp_path, monkeypatch)
    flags = ("--detector-model", str(tmp_path / "missing.onnx"))
    with started(tmp_path, camera, archive, None, *flags) as service:
        answer = wait_for(lambda: (latest := service.latest()).get("frame_id") and latest, 30)
        assert check(service.base, timeout=5) == []
    assert answer["detector"]["state"] == "failed"
    assert "missing.onnx" in answer["detector"]["reason"]
    assert "l0" not in answer


def test_a_detector_model_with_no_package_does_not_refuse_the_process(
    tmp_path: Path, camera: FakeFfmpeg, archive: Path
) -> None:
    flags = ("--detector-model", str(tmp_path / "model.onnx"))
    with started(tmp_path, camera, archive, None, *flags) as service:
        answer = wait_for(lambda: (latest := service.latest()).get("frame_id") and latest, 30)
    assert answer["detector"] == {"state": "failed", "reason": "rails49-detector is not installed"}


def test_a_manifest_with_no_id_does_not_refuse_the_process(
    tmp_path: Path, camera: FakeFfmpeg
) -> None:
    archive = write_archive(tmp_path / "archive", SENSORS, id=None)
    with started(tmp_path, camera, archive) as service:
        assert service.latest()["l1"]["s1"] == {"state": "unknown", "reason": "archive has no id"}


def assert_refused(tmp_path: Path, archive: Path | str, *extra: str, port: int = 0) -> str:
    """Runs the child to its end, and checks all a refusal promises. Its one line of stderr."""
    (tmp_path / "bin").mkdir()
    fake = install(tmp_path / "bin", [stream(jpeg(WIDTH, HEIGHT))])
    port = port or free_port()
    done = subprocess.run(
        command(archive, free_port(), port, *extra),
        env=child_env(fake),
        capture_output=True,
        text=True,
        timeout=START_WAIT,
    )
    assert done.returncode == 1
    lines = done.stderr.splitlines()
    assert len(lines) == 1, done.stderr
    assert "Traceback" not in done.stderr
    assert fake.starts() == []
    return lines[0]


def test_a_missing_path_refuses_the_process(tmp_path: Path) -> None:
    path = tmp_path / "nothing-here"
    line = assert_refused(tmp_path, path)
    assert str(path) in line


@pytest.mark.parametrize(
    "manifest",
    ["{ not json", json.dumps({"version": 4, "layout": {"sensors": "many"}})],
    ids=["not JSON", "against the schema"],
)
def test_a_broken_manifest_refuses_the_process(tmp_path: Path, manifest: str) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "manifest.json").write_text(manifest)
    assert str(archive) in assert_refused(tmp_path, archive)


def test_an_unknown_profile_refuses_the_process_with_no_port_left_bound(
    tmp_path: Path, archive: Path
) -> None:
    port = free_port()
    assert "nonsense" in assert_refused(tmp_path, archive, "--profile", "nonsense", port=port)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def test_a_refusal_comes_before_anything_the_model_writes(tmp_path: Path, archive: Path) -> None:
    line = assert_refused(tmp_path, archive, "--device", "NPU", "--profile", "nonsense")
    assert "nonsense" in line


def test_an_address_in_use_refuses_the_process(tmp_path: Path, archive: Path) -> None:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        assert str(port) in assert_refused(tmp_path, archive, port=port)


def test_an_address_that_is_not_a_host_refuses_the_process(tmp_path: Path, archive: Path) -> None:
    assert_refused(tmp_path, archive, "--address", "x" * 70)


@pytest.mark.parametrize("broker", ["nocolon", ":1883", "host:0", "host:70000", "host:port"])
def test_a_malformed_broker_is_a_usage_error(tmp_path: Path, archive: Path, broker: str) -> None:
    done = subprocess.run(
        [*command(archive, 1, free_port()), "--broker", broker],
        capture_output=True,
        text=True,
        timeout=START_WAIT,
    )
    assert done.returncode == 2
    assert "--broker" in done.stderr
    assert "Traceback" not in done.stderr


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_exits_0_with_no_ffmpeg_left(
    tmp_path: Path, camera: FakeFfmpeg, archive: Path, number: signal.Signals
) -> None:
    with started(tmp_path, camera, archive) as service:
        pids = [start.pid for start in wait_for(camera.starts)]
        assert stop(service, number) == 0
    assert not any(is_alive(pid) for pid in pids)
    assert "Traceback" not in service.stderr.read_text()


def test_a_second_signal_does_not_cut_the_stop_short(
    tmp_path: Path, camera: FakeFfmpeg, archive: Path
) -> None:
    with started(tmp_path, camera, archive) as service:
        pids = [start.pid for start in wait_for(camera.starts)]
        service.process.send_signal(signal.SIGTERM)
        time.sleep(0.05)  # Into the stop, which takes a look at each part's thread.
        service.process.send_signal(signal.SIGTERM)
        assert service.process.wait(STOP_WAIT) == 0
    assert not any(is_alive(pid) for pid in pids)


@pytest.fixture
def broker(tmp_path: Path) -> Iterator[Broker]:
    """A real broker, not yet started."""
    binary = mosquitto()
    if binary is None:
        pytest.skip("mosquitto is not installed")
    broker = Broker(binary, tmp_path)
    yield broker
    broker.stop()


def entry(message: Received) -> dict[str, Any]:
    return json.loads(message.payload)


def test_a_subscriber_sees_the_messages_that_latest_reports(
    tmp_path: Path, camera: FakeFfmpeg, archive: Path, broker: Broker
) -> None:
    broker.start()
    subscriber = Subscriber(broker.port)
    try:
        with started(tmp_path, camera, archive, broker.port) as service:
            for id in SENSORS:
                subscriber.wait_for(lambda m, id=id: m.topic == f"{OWN}{id}", 10)
            last = wait_for(lambda: service.latest().get("last_messages"), 10)
            sent = wait_for(lambda: last.get("s1", {}).get("occupancy"), 10)
            assert sent["message"] in [entry(m) for m in subscriber.on(f"{OWN}s1")]
    finally:
        subscriber.close()


def test_two_profiles_both_publish(
    tmp_path: Path, camera: FakeFfmpeg, archive: Path, broker: Broker
) -> None:
    broker.start()
    subscriber = Subscriber(broker.port)
    extra = ("--profile", "occupancy", "--profile", "control")
    try:
        with started(tmp_path, camera, archive, broker.port, *extra):
            subscriber.wait_for(lambda m: m.topic == f"{OWN}s1", 10)
            found = subscriber.wait_for(lambda m: m.topic == CONTROL, 10)
    finally:
        subscriber.close()
    assert entry(found)["addr"] in SENSORS


def test_messages_arrive_once_a_broker_comes_up(
    tmp_path: Path, camera: FakeFfmpeg, archive: Path, broker: Broker
) -> None:
    with started(tmp_path, camera, archive, broker.port) as service:
        assert service.latest()["l1"].keys() == SENSORS.keys()
        broker.start()
        subscriber = Subscriber(broker.port)
        try:
            subscriber.wait_for(lambda m: m.topic == f"{OWN}s1", 10)
        finally:
            subscriber.close()


def test_sigterm_says_service_stopped_for_every_sensor(
    tmp_path: Path, camera: FakeFfmpeg, archive: Path, broker: Broker
) -> None:
    broker.start()
    subscriber = Subscriber(broker.port)
    stopped = {"state": "unknown", "reason": "service stopped"}
    try:
        with started(tmp_path, camera, archive, broker.port) as service:
            subscriber.wait_for(lambda m: m.topic == f"{OWN}s1", 10)
            pids = [start.pid for start in wait_for(camera.starts)]
            assert stop(service) == 0
        for id in SENSORS:
            subscriber.wait_for(lambda m, id=id: m.topic == f"{OWN}{id}" and entry(m) == stopped)
    finally:
        subscriber.close()
    assert not any(is_alive(pid) for pid in pids)


def killed(service: Service, camera: FakeFfmpeg) -> float:
    """Kills the child with SIGKILL, and the ``ffmpeg`` it can no longer stop. It is frozen first
    until what it sent has arrived, so that what arrives after the returned time is the broker's,
    which may come before ``wait`` returns. The broker waits 1.5 keepalives, 7.5 s, to notice a
    frozen one."""
    pids = [start.pid for start in wait_for(camera.starts)]
    service.process.send_signal(signal.SIGSTOP)
    time.sleep(0.5)
    died_at = time.monotonic()
    assert stop(service, signal.SIGKILL) == -signal.SIGKILL
    for pid in pids:
        if is_alive(pid):
            os.kill(pid, signal.SIGKILL)
    return died_at


def test_sigkill_leaves_active_retained_on_every_jmri_sensor(
    tmp_path: Path, camera: FakeFfmpeg, archive: Path, broker: Broker
) -> None:
    broker.start()
    subscriber = Subscriber(broker.port)
    topics = [f"track/sensor/{id}" for id in SENSORS]
    try:
        with started(tmp_path, camera, archive, broker.port, "--profile", "jmri") as service:
            wait_for(lambda: all(subscriber.on(topic) for topic in topics), 10)
            died_at = killed(service, camera)
        # Nothing of the service's sends after it died: these are the broker's, from each will.
        for topic in topics:
            will = subscriber.wait_for(lambda m, t=topic: m.topic == t and m.at > died_at)
            assert will.payload == b"ACTIVE"
    finally:
        subscriber.close()
    late = Subscriber(broker.port)
    try:
        for topic in topics:
            kept = late.wait_for(lambda m, t=topic: m.topic == t)
            assert (kept.payload, kept.retain) == (b"ACTIVE", True)
    finally:
        late.close()


def test_sigkill_on_a_shared_profile_sends_no_will(
    tmp_path: Path, camera: FakeFfmpeg, archive: Path, broker: Broker
) -> None:
    broker.start()
    subscriber = Subscriber(broker.port)
    try:
        with started(tmp_path, camera, archive, broker.port) as service:
            subscriber.wait_for(lambda m: m.topic == f"{OWN}s1", 10)
            died_at = killed(service, camera)
        time.sleep(1)
        assert [m for m in subscriber.messages if m.at > died_at] == []
    finally:
        subscriber.close()


def test_sigkill_sends_one_true_per_rocrail_sensor_and_retains_nothing(
    tmp_path: Path, camera: FakeFfmpeg, archive: Path, broker: Broker
) -> None:
    broker.start()
    subscriber = Subscriber(broker.port, qos=1)
    topic = "rocrail/service/field"
    true = {f'<fb id="{id}" state="true"/>'.encode() for id in SENSORS}
    try:
        with started(tmp_path, camera, archive, broker.port, "--profile", "rocrail") as service:
            wait_for(lambda: len(subscriber.on(topic)) >= len(SENSORS), 10)
            died_at = killed(service, camera)
        # Nothing of the service's sends after it died: these are the broker's, from each will.
        wait_for(lambda: len([m for m in subscriber.on(topic) if m.at > died_at]) >= len(SENSORS))
        time.sleep(0.5)
        wills = [m for m in subscriber.on(topic) if m.at > died_at]
        assert sorted(m.payload for m in wills) == sorted(true)
        assert {(m.qos, m.retain) for m in wills} == {(1, False)}
    finally:
        subscriber.close()
    late = Subscriber(broker.port)
    try:
        time.sleep(0.5)
        assert late.messages == []
    finally:
        late.close()
