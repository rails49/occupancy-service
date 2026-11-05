"""Serve the live contract over HTTP under ``/live``. LIVE.md owns what each route answers."""

import logging
import mimetypes
import os
import threading
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote

import r49

from occupancy_service.l1 import L1Loop

_log = logging.getLogger(__name__)

_BASE = "/live"
_ARCHIVE = f"{_BASE}/archive/"
_MANIFEST = "manifest.json"
_IDLE_TIMEOUT = 30.0  # Seconds an idle connection stays open, and any one write may take.


@dataclass(frozen=True)
class ServerSettings:
    """Where to listen. Port 0 takes a free port."""

    address: str = "127.0.0.1"
    port: int = 8000


class LiveServer:
    """A thread per request, reading from ``loop`` and ``archive``. The caller owns the loop."""

    def __init__(
        self, settings: ServerSettings, archive: str | os.PathLike[str], loop: L1Loop
    ) -> None:
        """Binds the address, and raises the ``OSError`` when it cannot."""
        self.settings = settings
        self.archive = archive
        self.loop = loop
        self.stopping = threading.Event()
        self._http = ThreadingHTTPServer((settings.address, settings.port), partial(_Handler, self))
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._http.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._http.serve_forever, args=(0.05,), name="http", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.stopping.set()
        if self._thread is not None:
            self._http.shutdown()
            self._thread.join()
            self._thread = None
        self._http.server_close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = _IDLE_TIMEOUT
    disable_nagle_algorithm = True

    def __init__(self, live: LiveServer, *args: Any) -> None:
        self.live = live  # Before super(), which handles the request.
        super().__init__(*args)

    def do_GET(self) -> None:
        path = self.path.partition("?")[0]
        try:
            if path.startswith(_ARCHIVE):
                self._archive_file(unquote(path.removeprefix(_ARCHIVE)))
            else:
                self.send_error(404)
        except OSError:
            self.close_connection = True  # The client has gone, or is not reading.

    def log_message(self, format: str, *args: Any) -> None:
        _log.debug(format, *args)

    def _archive_file(self, name: str) -> None:
        # A folder or a zip would not refuse every dot path by itself, and this is a direct
        # request: the server refuses them for every archive.
        if any(segment.startswith(".") for segment in name.split("/")):
            self.send_error(404)
            return
        try:
            body = r49.load(self.live.archive).image(name)
        except (r49.R49Error, OSError):
            _log.exception("reading %r from the archive failed", name)
            self.send_error(500)
            return
        if body is None:
            self.send_error(404)
        elif name == _MANIFEST:
            self._reply(body, "application/json", no_store=True)
        else:
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self._reply(body, content_type)

    def _reply(self, body: bytes, content_type: str, no_store: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
