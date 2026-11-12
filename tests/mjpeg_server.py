"""A local HTTP server that sends images as a multipart stream, as an MJPEG camera or mediamtx
does. Stopping the stream ends the connection, and the server refuses until it starts again."""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_BOUNDARY = "frame"


class MjpegServer:
    def __init__(self, image: bytes, content_type: str = "image/jpeg", fps: float = 20) -> None:
        self.image, self.content_type, self.fps = image, content_type, fps
        self.serving = threading.Event()
        self.serving.set()
        self.url = ""
        self.requests = 0


def _handler(server: MjpegServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            server.requests += 1
            if not server.serving.is_set():
                self.send_error(503)
                return
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace;boundary={_BOUNDARY}")
            self.end_headers()
            try:
                while server.serving.is_set():
                    part = (
                        f"--{_BOUNDARY}\r\nContent-Type: {server.content_type}\r\n"
                        f"Content-Length: {len(server.image)}\r\n\r\n"
                    ).encode()
                    self.wfile.write(part + server.image + b"\r\n")
                    self.wfile.flush()
                    time.sleep(1 / server.fps)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


@contextmanager
def serving(server: MjpegServer) -> Iterator[MjpegServer]:
    """Serve at ``server.url`` until the block ends."""
    http = ThreadingHTTPServer(("127.0.0.1", 0), _handler(server))
    http.daemon_threads = True
    server.url = f"http://127.0.0.1:{http.server_address[1]}/stream"
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.serving.clear()
        http.shutdown()
        http.server_close()
