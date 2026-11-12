"""A stand-in for the ``ffmpeg`` executable. A test scripts what each start of it does."""

import io
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

WIDTH, HEIGHT = 64, 48

_SCRIPT = """#!{python}
import json, os, sys, time

here = {here!r}
if "-list_formats" in sys.argv:
    with open(os.path.join(here, "formats.txt")) as formats:
        sys.stderr.write(formats.read())
    sys.exit(1)
with open(os.path.join(here, "starts.jsonl"), "a") as log:
    log.write(json.dumps({{"pid": os.getpid(), "time": time.time(), "argv": sys.argv[1:]}}) + "\\n")
with open(os.path.join(here, "starts.jsonl")) as log:
    start = sum(1 for _ in log) - 1
with open(os.path.join(here, "runs.json")) as runs_file:
    runs = json.load(runs_file)

out = sys.stdout.buffer
for step, *args in runs[min(start, len(runs) - 1)]:
    if step == "send":
        out.write(bytes.fromhex(args[0]))
        out.flush()
    elif step == "stream":
        while True:
            out.write(bytes.fromhex(args[0]))
            out.flush()
            time.sleep(1 / args[1])
    elif step == "hang":
        time.sleep(3600)
    elif step == "exit":
        sys.exit(args[0])
"""


_FILE_INPUT_SCRIPT = """#!{python}
import os, sys

args = sys.argv[1:]
args[args.index("-f") : args.index("-i") + 2] = ["-f", "mjpeg", "-i", {source!r}]
os.execv({ffmpeg!r}, [{ffmpeg!r}, *args])
"""


def noise_jpeg() -> bytes:
    """A JPEG unlike any other this returns, with enough scan data to cut in half."""
    out = io.BytesIO()
    Image.effect_noise((WIDTH, HEIGHT), 50).convert("RGB").save(out, "JPEG")
    return out.getvalue()


def send(*chunks: bytes) -> list[Any]:
    """Write these bytes to the pipe, with no pause."""
    return ["send", b"".join(chunks).hex()]


def stream(jpeg: bytes, fps: float = 20) -> list[Any]:
    """Send this frame at ``fps``, until killed."""
    return ["stream", jpeg.hex(), fps]


HANG: list[Any] = ["hang"]


def exits(code: int = 1) -> list[Any]:
    return ["exit", code]


@dataclass(frozen=True)
class Start:
    """One start of the fake."""

    pid: int
    time: float
    argv: list[str]


@dataclass(frozen=True)
class FakeFfmpeg:
    directory: Path

    @property
    def path(self) -> str:
        return str(self.directory / "ffmpeg")

    def starts(self) -> list[Start]:
        log = self.directory / "starts.jsonl"
        if not log.exists():
            return []
        lines = log.read_text().splitlines()
        return [Start(**json.loads(line)) for line in lines if line.endswith("}")]


def formats(*sizes: str) -> str:
    """What ``ffmpeg -list_formats compressed`` prints for a camera giving MJPEG at ``sizes``, as
    the OBSBOT Meet 2 does. With no sizes, what it prints for no camera."""
    if not sizes:
        return "[in#0 @ 0x1] Error opening input: No such file or directory\n"
    return (
        "[video4linux2,v4l2 @ 0x1] Compressed:       mjpeg :          Motion-JPEG : "
        + " ".join(sizes)
        + "\n[in#0 @ 0x1] Error opening input: Immediate exit requested\n"
    )


def install(
    directory: Path, *runs: list[list[Any]], listed: str = formats(f"{WIDTH}x{HEIGHT}")
) -> FakeFfmpeg:
    """Write a fake into ``directory``. The n-th start does ``runs[n]``; the last run repeats. A
    run with ``-list_formats`` prints ``listed`` and is not a start."""
    (directory / "runs.json").write_text(json.dumps(runs))
    (directory / "formats.txt").write_text(listed)
    script = directory / "ffmpeg"
    script.write_text(_SCRIPT.format(python=sys.executable, here=str(directory)))
    script.chmod(0o755)
    return FakeFfmpeg(directory)


def install_file_input(directory: Path, ffmpeg: str, source: Path) -> FakeFfmpeg:
    """Write a wrapper that runs the real ``ffmpeg`` command line, reading ``source`` in place
    of the camera. All the arguments after the input are the ones capture gave it."""
    script = directory / "ffmpeg"
    script.write_text(
        _FILE_INPUT_SCRIPT.format(python=sys.executable, source=str(source), ffmpeg=ffmpeg)
    )
    script.chmod(0o755)
    return FakeFfmpeg(directory)
