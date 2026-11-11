"""Minimal blocking transport around ClientConnection, for tests only."""

from __future__ import annotations

import contextlib
import socket
import time
from collections import deque
from typing import Any

from comapeo_qgis.core.protocol import (
    Authenticated,
    ClientConnection,
    ClientState,
    Event,
    Notification,
    RequestFailed,
    Response,
)


class RemoteError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class BlockingClient:
    def __init__(
        self,
        socket_path: str,
        secret: str,
        *,
        protocol_versions: tuple[int, ...] = (1,),
        request_timeout: float = 5.0,
        handshake_timeout: float = 5.0,
    ) -> None:
        self.socket_path = socket_path
        self.conn = ClientConnection(
            secret,
            "comapeo-qgis",
            "0.1.0",
            protocol_versions=protocol_versions,
            request_timeout=request_timeout,
            handshake_timeout=handshake_timeout,
        )
        self.sock: Any = None
        self.events: deque[Event] = deque()
        self.log: list[Event] = []

    def connect(self) -> Event:
        """Connect and run the handshake; returns the first decisive event."""
        self.sock = self._open(self.socket_path)
        self.conn.start()
        self._flush()
        return self.wait_for(lambda e: not isinstance(e, Notification), timeout=6)

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = self.conn.request(method, params)
        self._flush()
        event = self.wait_for(
            lambda e: (
                isinstance(e, (Response, RequestFailed)) and e.request_id == request_id
            )
        )
        if isinstance(event, RequestFailed):
            raise RemoteError(event.error.code, event.error.message)
        assert isinstance(event, Response)
        return event.result

    def wait_for(self, predicate, timeout: float = 5.0) -> Event:
        deadline = time.monotonic() + timeout
        while True:
            for i, event in enumerate(self.events):
                if predicate(event):
                    del self.events[i]
                    return event
            if self.sock is None and self.conn.state is ClientState.CLOSED:
                raise ConnectionError("connection closed")
            if time.monotonic() > deadline:
                raise TimeoutError("no matching event")
            self._pump()

    def close(self) -> None:
        self._push(self.conn.close())
        if self.sock is not None:
            with contextlib.suppress(OSError):
                self._shut()
            self.sock = None

    @property
    def authenticated(self) -> bool:
        return any(isinstance(e, Authenticated) for e in self.log)

    def _pump(self) -> None:
        if self.sock is None:
            time.sleep(0.01)
            return
        data = self._recv()
        if data:
            self._push(self.conn.receive_data(data))
        elif data == b"":
            self._push(self.conn.connection_lost())
            self._shut()
            self.sock = None
        self._push(self.conn.tick())
        self._flush()

    def _flush(self) -> None:
        if self.conn.wants_close and self.sock is not None:
            self._shut()
            self.sock = None
            return
        data = self.conn.data_to_send()
        if data and self.sock is not None:
            self._send(data)

    # The stream; tests/qgis/helpers.py swaps in QLocalSocket for named pipes.

    def _open(self, path: str) -> Any:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        try:
            sock.connect(path)
        except OSError:
            sock.close()
            raise
        sock.settimeout(0.05)
        return sock

    def _recv(self) -> bytes | None:
        """Waits briefly; None when nothing arrived, b"" once the peer closed."""
        try:
            return self.sock.recv(65536)
        except socket.timeout:  # not TimeoutError before Python 3.10
            return None
        except OSError:
            return b""

    def _send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def _shut(self) -> None:
        self.sock.close()

    def _push(self, events: list[Event]) -> None:
        self.events.extend(events)
        self.log.extend(events)
