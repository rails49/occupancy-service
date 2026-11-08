"""``python -m occupancy_service``: start the parts from the command line, and stop them on a
signal.

The README's Run section says what refuses the process and what does not.
"""

import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any, get_args

import r49
from classifier.runtime import Engine

from occupancy_service.capture import Capture, CaptureSettings
from occupancy_service.l1 import L1Loop, L1Settings
from occupancy_service.profile import ProfileError
from occupancy_service.publisher import Publisher, PublisherSettings
from occupancy_service.server import LiveServer, ServerSettings

_log = logging.getLogger("occupancy_service")

_PROG = "occupancy-service"
_WAKE = 1.0  # Seconds between looks for a signal that came just as the main thread went to sleep.


class _Refused(Exception):
    """What stops the process from starting. The message is the one line it says about that."""


def _port(text: str) -> int:
    if not text.isdecimal() or int(text) > 65535:
        raise argparse.ArgumentTypeError(f"{text!r} is not a port")
    return int(text)


def _broker(text: str) -> tuple[str, int]:
    """``<host>:<port>``. A bracketed IPv6 host keeps its colons and loses its brackets."""
    host, _, port = text.rpartition(":")
    host = host.removeprefix("[").removesuffix("]")
    if not host or _port(port) == 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not <host>:<port>")
    return host, int(port)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m occupancy_service",
        description="Publish each sensor's occupancy from one archive and one camera.",
    )
    parser.add_argument("--archive", required=True, help="the archive, a folder or a .r49 file")
    parser.add_argument("--camera", required=True, help="the camera's device, e.g. /dev/video0")
    parser.add_argument(
        "--broker",
        required=True,
        type=_broker,
        metavar="HOST:PORT",
        help="the MQTT broker, retried until it answers",
    )
    parser.add_argument(
        "--profile",
        action="append",
        help="an active profile: a shipped one's name, or a file. Repeat it for more than one",
    )
    parser.add_argument("--address", help="the address to serve HTTP on")
    parser.add_argument("--port", type=_port, help="the port to serve HTTP on")
    parser.add_argument("--engine", choices=get_args(Engine), help="what runs the model")
    parser.add_argument("--device", help="the device the engine runs the model on")
    parser.add_argument("--frame-rate", type=float, help="the frames per second to ask the camera")
    parser.add_argument(
        "--detector-model",
        type=Path,
        help="the detector's model, which needs rails49-detector installed. Absent: no detector",
    )
    parser.add_argument("--detector-engine", help="what runs the detector's model")
    parser.add_argument("--detector-device", help="the device that engine runs it on")
    return parser


def _given(**values: Any) -> dict[str, Any]:
    """The flags that were given, so that the rest keep their dataclass's default."""
    return {name: value for name, value in values.items() if value is not None}


def _one_line(error: BaseException) -> str:
    return " ".join(str(error).split()) or type(error).__name__


def _built(args: argparse.Namespace) -> tuple[Capture, L1Loop, Publisher, LiveServer]:
    """Every part, with none started, so a refusal leaves no ``ffmpeg`` and no thread behind. The
    ``LiveServer`` comes last because making it binds the port."""
    try:
        resolution = r49.load(args.archive).manifest.camera.resolution
        capture = Capture(
            CaptureSettings(resolution, device=args.camera, **_given(frame_rate=args.frame_rate))
        )
        detector = _given(
            detector_model=args.detector_model,
            detector_engine=args.detector_engine,
            detector_device=args.detector_device,
        )
        loop = L1Loop(
            L1Settings(args.archive, **_given(engine=args.engine, device=args.device), **detector),
            capture,
        )
    except Exception as error:  # What r49.load raises for a URL is the HTTP library's to choose.
        raise _Refused(f"cannot read the archive {args.archive}: {_one_line(error)}") from error
    host, port = args.broker
    profiles = _given(profiles=tuple(args.profile) if args.profile else None)
    try:
        publisher = Publisher(PublisherSettings(host, port, **profiles), loop)
    except ProfileError as error:
        raise _Refused(f"cannot use a profile: {_one_line(error)}") from error
    settings = ServerSettings(**_given(address=args.address, port=args.port))
    try:
        server = LiveServer(settings, args.archive, loop, publisher.last_messages)
    except (OSError, UnicodeError) as error:  # A host name too long for IDNA is a UnicodeError.
        raise _Refused(
            f"cannot bind {settings.address}:{settings.port}: {_one_line(error)}"
        ) from error
    return capture, loop, publisher, server


def _serve(args: argparse.Namespace) -> None:
    """Runs the parts until a signal raises ``KeyboardInterrupt``."""
    capture, loop, publisher, server = _built(args)
    try:
        capture.start()
        loop.start()
        publisher.start()
        server.start()
        _log.info("serving %s on port %d", args.archive, server.port)
        while True:
            time.sleep(_WAKE)
    finally:
        # A second signal must not cut this short, or it would leave ffmpeg behind.
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, signal.SIG_IGN)
        server.stop()
        publisher.stop()  # While the loop runs, as it reads the sensors for ``service stopped``.
        loop.stop()
        capture.stop()


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # SIGINT too, as a process started in the background has it ignored.
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, signal.default_int_handler)
    try:
        _serve(args)
    except KeyboardInterrupt:
        pass
    except _Refused as refused:
        print(f"{_PROG}: {refused}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
