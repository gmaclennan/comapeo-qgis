"""QLocalSocket transport for the core protocol client, on the main thread."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal
from qgis.PyQt.QtNetwork import QLocalSocket

from ..core.discovery import DiscoveryInfo
from ..core.protocol import (
    Authenticated,
    ClientConnection,
    ClientError,
    ClientState,
    Closed,
    HandshakeRejected,
    Notification,
    ProtocolViolation,
    RequestError,
    RequestFailed,
    Response,
    VersionMismatch,
)

CLIENT_NAME = "comapeo-qgis"

Callback = Callable[[Any, "RequestError | None"], None]


class LocalApiClient(QObject):
    """One connection. Emits `failed` at most once, then `closed` exactly once.

    `failed` carries a VersionMismatch, HandshakeRejected or ProtocolViolation,
    or a str for a socket error before the handshake finished.
    """

    authenticated = pyqtSignal(object)
    notification = pyqtSignal(str, object)
    failed = pyqtSignal(object)
    closed = pyqtSignal(bool)

    def __init__(
        self,
        info: DiscoveryInfo,
        client_version: str,
        parent: QObject | None = None,
        *,
        request_timeout: float = 60.0,
        handshake_timeout: float = 10.0,
    ) -> None:
        super().__init__(parent)
        self.info = info
        self._conn = ClientConnection(
            info.secret,
            CLIENT_NAME,
            client_version,
            request_timeout=request_timeout,
            handshake_timeout=handshake_timeout,
        )
        self._callbacks: dict[int, Callback] = {}
        self._failed = False
        self._closed = False
        self._socket = QLocalSocket(self)
        self._socket.connected.connect(self._on_connected)
        self._socket.readyRead.connect(self._on_ready_read)
        self._socket.disconnected.connect(self._on_disconnected)
        self._socket.errorOccurred.connect(self._on_error)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timer)

    @property
    def is_ready(self) -> bool:
        return self._conn.state is ClientState.READY and not self._closed

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def app_info(self) -> Mapping[str, Any]:
        return self._conn.app_info

    def open(self) -> None:
        self._socket.connectToServer(self.info.socket_path)

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        callback: Callback | None = None,
    ) -> int | None:
        """Send a request; `callback(result, error)` runs exactly once.

        Returns None, after calling back with an error, if it cannot be sent.
        """
        try:
            request_id = self._conn.request(method, params)
        except ClientError as err:
            if callback is not None:
                callback(None, RequestError("CLIENT_ERROR", str(err)))
            return None
        if callback is not None:
            self._callbacks[request_id] = callback
        self._flush()
        return request_id

    def close(self) -> None:
        self._dispatch(self._conn.close())

    def wait(self, done: Callable[[], bool], timeout: float) -> bool:
        """Block on this socket until `done()` is true; False after `timeout`.

        No event loop runs, so nothing else in QGIS moves meanwhile; replies,
        notifications and request deadlines on this connection are handled."""
        deadline = time.monotonic() + timeout
        connected = QLocalSocket.LocalSocketState.ConnectedState
        while not done():
            remaining = deadline - time.monotonic()
            if self._closed or remaining <= 0:
                return done()
            slice_ms = max(1, min(100, int(remaining * 1000)))
            if self._socket.bytesToWrite() > 0:
                self._socket.waitForBytesWritten(slice_ms)
            if self._socket.bytesAvailable() == 0:
                self._socket.waitForReadyRead(slice_ms)
            if self._socket.bytesAvailable() > 0:
                self._on_ready_read()
            self._dispatch(self._conn.tick())
            if not self._closed and self._socket.state() != connected:
                self._on_disconnected()
        return True

    def _on_connected(self) -> None:
        try:
            self._conn.start()
        except ClientError as err:
            self._emit_failed(str(err))
            self.close()
            return
        self._flush()

    def _on_ready_read(self) -> None:
        data = bytes(self._socket.readAll())
        if data:
            self._dispatch(self._conn.receive_data(data))

    def _on_disconnected(self) -> None:
        self._dispatch(self._conn.connection_lost())

    def _on_error(self, error: Any) -> None:
        # A wait that timed out; the connection is fine.
        if error == QLocalSocket.LocalSocketError.SocketTimeoutError:
            return
        # macOS and Linux report PeerClosedError before `disconnected`; Windows not.
        if error == QLocalSocket.LocalSocketError.PeerClosedError:
            self._on_disconnected()
            return
        if self._conn.state is ClientState.IDLE:
            self._emit_failed(self._socket.errorString())
        self._dispatch(self._conn.connection_lost())
        if not self._closed:
            self._finish(False)

    def _on_timer(self) -> None:
        self._dispatch(self._conn.tick())

    def _dispatch(self, events: list[Any]) -> None:
        for event in events:
            if isinstance(event, (Response, RequestFailed)):
                callback = self._callbacks.pop(event.request_id, None)
                if callback is None:
                    continue
                if isinstance(event, Response):
                    callback(event.result, None)
                else:
                    callback(None, event.error)
            elif isinstance(event, Notification):
                self.notification.emit(event.method, event.params)
            elif isinstance(event, Authenticated):
                self.authenticated.emit(event)
            elif isinstance(
                event, (VersionMismatch, HandshakeRejected, ProtocolViolation)
            ):
                self._emit_failed(event)
            elif isinstance(event, Closed):
                self._finish(event.was_authenticated)
        self._flush()

    def _flush(self) -> None:
        if self._closed:
            return
        if self._conn.wants_close or self._conn.state is ClientState.CLOSED:
            self._timer.stop()
            self._socket.abort()
            return
        data = self._conn.data_to_send()
        if data and (
            self._socket.state() == QLocalSocket.LocalSocketState.ConnectedState
        ):
            self._socket.write(data)
        deadline = self._conn.next_deadline()
        if deadline is None:
            self._timer.stop()
        else:
            delay = max(0, int((deadline - time.monotonic()) * 1000) + 1)
            self._timer.start(delay)

    def _emit_failed(self, reason: Any) -> None:
        if not self._failed and not self._closed:
            self._failed = True
            self.failed.emit(reason)

    def _finish(self, was_authenticated: bool) -> None:
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        self._callbacks.clear()
        self._socket.abort()
        self.closed.emit(was_authenticated)
