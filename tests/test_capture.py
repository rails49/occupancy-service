"""Capture against a fake ``ffmpeg``: frames, and a camera that goes away and comes back. The
real ``ffmpeg``, where there is one, against a file and a local HTTP stream."""

import io
import os
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import r49
from fake_ffmpeg import (
    HANG,
    HEIGHT,
    WIDTH,
    FakeFfmpeg,
    exits,
    formats,
    install,
    install_file_input,
    noise_jpeg,
    send,
    stream,
)
from mjpeg_server import MjpegServer, serving
from PIL import Image

from occupancy_service.capture import Capture, CaptureSettings, CaptureStatus, Frame

DHT, SOS = 0xC4, 0xDA


@contextmanager
def capturing(fake: FakeFfmpeg, **settings: Any) -> Iterator[Capture]:
    capture = Capture(
        CaptureSettings(
            resolution=r49.Resolution(width=WIDTH, height=HEIGHT),
            ffmpeg=fake.path,
            **settings,
        )
    )
    capture.start()
    try:
        yield capture
    finally:
        capture.stop()


def wait_for[T](value: Callable[[], T | None], timeout: float = 5) -> T:
    """The first truthy ``value()``. A timeout fails the test."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = value()
        if found:
            return found
        time.sleep(0.005)
    raise AssertionError(f"not within {timeout} s")


def frame_of(capture: Capture, jpeg: bytes) -> Frame:
    """Wait until ``jpeg`` is the latest frame."""

    def found() -> Frame | None:
        frame = capture.latest()
        return frame if frame is not None and frame.jpeg == jpeg else None

    return wait_for(found)


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_frame_from_the_pipe_is_the_latest_frame(tmp_path: Path) -> None:
    first = noise_jpeg()
    fake = install(tmp_path, [send(first), HANG])
    with capturing(fake) as capture:
        frame = wait_for(capture.latest)
    assert frame.jpeg == first
    assert frame.arrived_at.tzinfo is not None


def test_a_frame_cut_in_its_scan_is_dropped_and_does_not_count(tmp_path: Path) -> None:
    whole, cut, after = noise_jpeg(), noise_jpeg(), noise_jpeg()
    fake = install(tmp_path, [send(whole, cut[:-100], after), HANG])
    with capturing(fake) as capture:
        frame = frame_of(capture, after)
    assert frame.id == 2


def test_a_frame_cut_in_its_headers_is_dropped(tmp_path: Path) -> None:
    cut, after = noise_jpeg(), noise_jpeg()
    fake = install(tmp_path, [send(cut[:50], after), HANG])
    with capturing(fake) as capture:
        frame = wait_for(capture.latest)
    assert frame.jpeg == after


def test_a_frame_cut_off_when_the_pipe_ends_is_dropped(tmp_path: Path) -> None:
    whole, cut = noise_jpeg(), noise_jpeg()
    fake = install(tmp_path, [send(whole, cut[:-100]), exits()], [HANG])
    with capturing(fake) as capture:
        wait_for(lambda: len(fake.starts()) == 2)
        frame = capture.latest()
    assert frame is not None and frame.jpeg == whole


def test_garbage_before_a_frame_is_skipped(tmp_path: Path) -> None:
    jpeg = noise_jpeg()
    fake = install(tmp_path, [send(b"not a jpeg\xff", jpeg), HANG])
    with capturing(fake) as capture:
        frame = wait_for(capture.latest)
    assert frame.jpeg == jpeg


def test_a_slow_reader_gets_the_newest_frame_and_no_older_one_is_queued(tmp_path: Path) -> None:
    jpegs = [noise_jpeg() for _ in range(50)]
    fake = install(tmp_path, [send(*jpegs), HANG])
    with capturing(fake) as capture:
        time.sleep(1)  # The reader is away while all of them arrive.
        frame = capture.latest()
        again = capture.latest()
    assert frame is not None
    assert frame.jpeg == jpegs[-1]
    assert frame.id == 50
    assert again == frame


def test_status_is_lost_before_the_first_frame(tmp_path: Path) -> None:
    fake = install(tmp_path, [HANG])
    with capturing(fake) as capture:
        status = capture.status()
    assert status == CaptureStatus(state="lost", frame_age_ms=None, rate_fps=0)


def test_status_reports_state_rate_and_frame_age_while_frames_arrive(tmp_path: Path) -> None:
    fake = install(tmp_path, [stream(noise_jpeg(), fps=20)])
    with capturing(fake) as capture:
        wait_for(capture.latest)
        time.sleep(1)
        status = capture.status()
    assert status.state == "ok"
    assert status.frame_age_ms is not None and 0 <= status.frame_age_ms < 500
    assert 10 < status.rate_fps <= 21


def test_status_is_lost_about_1_s_after_frames_stop_while_the_fake_is_hung(tmp_path: Path) -> None:
    fake = install(tmp_path, [send(noise_jpeg()), HANG])
    with capturing(fake, hang_limit=60) as capture:
        wait_for(capture.latest)
        arrived = time.monotonic()
        assert capture.status().state == "ok"
        wait_for(lambda: capture.status().state == "lost")
        waited = time.monotonic() - arrived
        status = capture.status()
    assert 0.8 < waited < 1.5
    assert len(fake.starts()) == 1
    assert status.frame_age_ms is not None and status.frame_age_ms >= 1000
    assert status.rate_fps == 0


def test_the_time_until_lost_is_a_setting(tmp_path: Path) -> None:
    fake = install(tmp_path, [send(noise_jpeg()), HANG])
    with capturing(fake, hang_limit=60, lost_after=0.2) as capture:
        wait_for(capture.latest)
        arrived = time.monotonic()
        wait_for(lambda: capture.status().state == "lost")
        waited = time.monotonic() - arrived
    assert waited < 0.7


def test_a_hung_fake_is_killed_at_the_hang_limit_and_frames_resume(tmp_path: Path) -> None:
    before, after = noise_jpeg(), noise_jpeg()
    fake = install(tmp_path, [send(before), HANG], [stream(after)])
    with capturing(fake, hang_limit=1.5) as capture:
        first = wait_for(capture.latest)
        arrived = time.time()
        wait_for(lambda: capture.status().state == "lost")
        frame = frame_of(capture, after)
        status = capture.status()
    hung, restarted = fake.starts()[:2]
    assert not is_alive(hung.pid)
    assert 1.4 < restarted.time - arrived < 3.5
    assert frame.id > first.id
    assert status.state == "ok"


def test_a_fake_that_hangs_with_no_frame_at_all_is_killed_at_the_hang_limit(tmp_path: Path) -> None:
    fake = install(tmp_path, [HANG])
    with capturing(fake, hang_limit=0.5):
        wait_for(lambda: len(fake.starts()) == 2)


def test_a_fake_that_exits_at_once_is_retried_until_the_camera_returns(tmp_path: Path) -> None:
    jpeg = noise_jpeg()
    fake = install(tmp_path, [exits()], [exits()], [stream(jpeg)])
    with capturing(fake) as capture:
        wait_for(lambda: len(fake.starts()) == 2)
        assert capture.status().state == "lost"
        frame = wait_for(capture.latest)
        status = capture.status()
    assert frame.jpeg == jpeg
    assert status.state == "ok"


def test_a_missing_ffmpeg_is_retried_until_it_is_there(tmp_path: Path) -> None:
    jpeg = noise_jpeg()
    with capturing(FakeFfmpeg(tmp_path)) as capture:  # Nothing is at its path yet.
        time.sleep(0.1)
        assert capture.status().state == "lost"
        install(tmp_path, [stream(jpeg)])
        frame = wait_for(capture.latest)
    assert frame.jpeg == jpeg


def test_stop_ends_the_child(tmp_path: Path) -> None:
    fake = install(tmp_path, [stream(noise_jpeg())])
    with capturing(fake) as capture:
        wait_for(capture.latest)
    assert not is_alive(fake.starts()[0].pid)


def test_the_command_copies_mjpeg_from_the_configured_camera(tmp_path: Path) -> None:
    fake = install(tmp_path, [HANG])
    with capturing(fake, device="/dev/video7", frame_rate=15):
        start = wait_for(fake.starts)[0]
    command = " ".join(start.argv)
    assert "-f v4l2 -input_format mjpeg -framerate 15 -video_size 64x48 -i /dev/video7" in command
    assert "-c:v copy -bsf:v mjpeg2jpeg" in command


def test_an_http_url_is_the_input_with_no_v4l2_option(tmp_path: Path) -> None:
    fake = install(tmp_path, [HANG])
    with capturing(fake, device="http://camera.local:8080/stream"):
        argv = wait_for(fake.starts)[0].argv
    input_at = argv.index("-i")
    assert argv[input_at + 1] == "http://camera.local:8080/stream"
    for option in ("v4l2", "-input_format", "-framerate", "-video_size", "-rtsp_transport"):
        assert option not in argv
    assert " ".join(argv[input_at + 2 :]) == (
        "-c:v copy -bsf:v mjpeg2jpeg -flush_packets 1 -f mjpeg pipe:1"
    )


def test_an_rtsp_url_is_read_over_tcp(tmp_path: Path) -> None:
    fake = install(tmp_path, [HANG])
    with capturing(fake, device="rtsp://host.docker.internal:8554/cam"):
        argv = wait_for(fake.starts)[0].argv
    assert "-rtsp_transport tcp -i rtsp://host.docker.internal:8554/cam" in " ".join(argv)
    assert "v4l2" not in argv


def test_with_no_resolution_a_url_is_read_with_no_size_listed(tmp_path: Path) -> None:
    fake = install(tmp_path, [stream(noise_jpeg())], listed=formats())
    capture = Capture(CaptureSettings(resolution=None, device="http://cam/s", ffmpeg=fake.path))
    capture.start()
    try:
        wait_for(capture.latest)
    finally:
        capture.stop()
    assert "-video_size" not in fake.starts()[0].argv


def segments(jpeg: bytes) -> list[tuple[int, int]]:
    """The marker and offset of each segment up to the scan."""
    found = []
    position = 2
    while True:
        marker = jpeg[position + 1]
        found.append((marker, position))
        if marker == SOS:
            return found
        position += 2 + int.from_bytes(jpeg[position + 2 : position + 4])


def without_huffman_tables(jpeg: bytes) -> bytes:
    kept = jpeg
    for marker, position in reversed(segments(jpeg)):
        if marker == DHT:
            length = 2 + int.from_bytes(jpeg[position + 2 : position + 4])
            kept = kept[:position] + kept[position + length :]
    return kept


def pixels(jpeg: bytes) -> bytes:
    with Image.open(io.BytesIO(jpeg)) as image:
        assert image.format == "JPEG"
        assert image.size == (WIDTH, HEIGHT)
        return image.tobytes()


def test_the_real_ffmpeg_turns_a_jpeg_with_no_huffman_tables_into_one_that_decodes(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("no ffmpeg on PATH. CI installs it")
    original = noise_jpeg()
    source = tmp_path / "camera.mjpeg"
    source.write_bytes(without_huffman_tables(original))
    assert DHT not in [marker for marker, _ in segments(source.read_bytes())]

    fake = install_file_input(tmp_path, ffmpeg, source)
    with capturing(fake) as capture:
        frame = wait_for(capture.latest)

    # Pillow's decoder fills in missing tables itself, so decoding alone would not show this.
    assert DHT in [marker for marker, _ in segments(frame.jpeg)]
    assert pixels(frame.jpeg) == pixels(original)


def test_with_no_resolution_the_camera_opens_at_its_largest_mjpeg_size(tmp_path: Path) -> None:
    listed = formats("1920x1080", "3840x2160", "1280x720")
    fake = install(tmp_path, [stream(noise_jpeg())], listed=listed)
    capture = Capture(CaptureSettings(resolution=None, ffmpeg=fake.path))
    capture.start()
    try:
        wait_for(capture.latest)
    finally:
        capture.stop()

    argv = fake.starts()[0].argv
    assert argv[argv.index("-video_size") + 1] == "3840x2160"


def test_with_no_resolution_and_no_size_listed_capture_waits_for_the_camera(
    tmp_path: Path,
) -> None:
    fake = install(tmp_path, [stream(noise_jpeg())], listed=formats())
    capture = Capture(CaptureSettings(resolution=None, ffmpeg=fake.path))
    capture.start()
    try:
        time.sleep(1.5)
        assert fake.starts() == []
        assert capture.status().state == "lost"

        (tmp_path / "formats.txt").write_text(formats(f"{WIDTH}x{HEIGHT}"))
        wait_for(capture.latest)
    finally:
        capture.stop()


def test_a_new_size_starts_ffmpeg_again_at_it_and_the_same_size_does_not(tmp_path: Path) -> None:
    listed = formats("1920x1080", "3840x2160")
    fake = install(tmp_path, [stream(noise_jpeg())], listed=listed)
    capture = Capture(CaptureSettings(resolution=None, ffmpeg=fake.path))
    capture.use(r49.Resolution(width=1920, height=1080))  # Before the start, as L1Loop does.
    capture.start()
    try:
        wait_for(capture.latest)
        capture.use(r49.Resolution(width=1920, height=1080))
        capture.use(None)
        wait_for(lambda: len(fake.starts()) == 2)
        time.sleep(0.2)
    finally:
        capture.stop()

    sizes = [start.argv[start.argv.index("-video_size") + 1] for start in fake.starts()]
    assert sizes == ["1920x1080", "3840x2160"]


def real_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("no ffmpeg on PATH. CI installs it")
    return ffmpeg


@contextmanager
def reading(url: str) -> Iterator[Capture]:
    """Capture from ``url`` with the real ``ffmpeg``."""
    capture = Capture(CaptureSettings(resolution=None, device=url, ffmpeg=real_ffmpeg()))
    capture.start()
    try:
        yield capture
    finally:
        capture.stop()


def png() -> bytes:
    out = io.BytesIO()
    Image.effect_noise((WIDTH, HEIGHT), 50).convert("RGB").save(out, "PNG")
    return out.getvalue()


def test_the_real_ffmpeg_reads_mjpeg_over_http_at_the_served_size() -> None:
    real_ffmpeg()
    with serving(MjpegServer(noise_jpeg())) as server, reading(server.url) as capture:
        frame = wait_for(capture.latest)
    with Image.open(io.BytesIO(frame.jpeg)) as image:
        assert (image.format, image.size) == ("JPEG", (WIDTH, HEIGHT))


def test_the_real_ffmpeg_gives_no_frames_from_a_stream_that_is_not_mjpeg() -> None:
    real_ffmpeg()
    with serving(MjpegServer(png(), content_type="image/png")) as server:
        with reading(server.url) as capture:
            wait_for(lambda: server.requests >= 3)
            assert capture.latest() is None
            assert capture.status().state == "lost"


def test_the_real_ffmpeg_gives_frames_again_when_a_stream_comes_back() -> None:
    real_ffmpeg()
    with serving(MjpegServer(noise_jpeg())) as server, reading(server.url) as capture:
        first = wait_for(capture.latest)
        server.serving.clear()
        wait_for(lambda: capture.status().state == "lost")
        server.serving.set()
        wait_for(lambda: capture.status().state == "ok")
        frame = capture.latest()
    assert frame is not None and frame.id > first.id
