"""A real HTTP server on 127.0.0.1 standing in for the app's media server.

Each path serves set bytes with a status and content type; `hold(path)` keeps
a reply back until `release(path)`, to test replies that arrive late, and
`truncate=True` closes the connection after half the body.

`framing` says how the end of the body is marked: "length" sends Content-Length
(the full length, even when truncated); "chunked" uses HTTP/1.1 chunked transfer
coding, as core's blob server does without a Range header, and a truncated reply
stops without the final empty chunk; "close" sends neither, so only the closed
connection ends the body and a truncated reply looks complete.
"""

from __future__ import annotations

import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_NOT_FOUND = (404, "application/json", b'{"error":"not found"}', False, "length")


class _Server(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # HTTPServer.server_bind looks up the host name, which can take 30 s.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class MediaServer:
    def __init__(self) -> None:
        self.files: dict[str, tuple[int, str, bytes, bool, str]] = {}
        self.requests: list[str] = []
        self._held: dict[str, threading.Event] = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                owner.requests.append(self.path)
                held = owner._held.get(self.path)
                if held is not None:
                    held.wait(30)
                status, content_type, body, truncate, framing = owner.files.get(
                    self.path, _NOT_FOUND
                )
                self.close_connection = True
                sent = body[: len(body) // 2] if truncate else body
                if framing == "chunked":
                    self.protocol_version = "HTTP/1.1"
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    if framing == "length":
                        self.send_header("Content-Length", str(len(body)))
                    elif framing == "chunked":
                        self.send_header("Transfer-Encoding", "chunked")
                        self.send_header("Connection", "close")
                    self.end_headers()
                    if framing == "chunked":
                        for start in range(0, len(sent), 1024):
                            chunk = sent[start : start + 1024]
                            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                        if not truncate:
                            self.wfile.write(b"0\r\n\r\n")
                    else:
                        self.wfile.write(sent)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args: Any) -> None:
                pass

        self.httpd = _Server(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> MediaServer:
        self.thread.start()
        return self

    def stop(self) -> None:
        for event in self._held.values():
            event.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}{path}"

    def serve(
        self,
        path: str,
        body: bytes,
        content_type: str,
        status: int = 200,
        truncate: bool = False,
        framing: str = "length",
    ) -> None:
        self.files[path] = (status, content_type, body, truncate, framing)

    def hold(self, path: str) -> None:
        self._held[path] = threading.Event()

    def release(self, path: str) -> None:
        self._held.pop(path).set()
