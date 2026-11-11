"""Byte-mode Windows named pipes with a socket-like interface, for the tests.

Overlapped I/O throughout, because the fake server writes notifications from
one thread while another is blocked reading the same connection.
"""

from __future__ import annotations

import _winapi  # type: ignore[import-not-found]
import contextlib
import secrets
import socket
import threading
import time
from typing import Any

PIPE_PREFIX = "\\\\.\\pipe\\"
_PIPE_TYPE_BYTE = 0
_PIPE_REJECT_REMOTE_CLIENTS = 0x8
_BUFSIZE = 64 * 1024
_POLL_MS = 50


def new_pipe_name() -> str:
    return PIPE_PREFIX + "cq-" + secrets.token_hex(8)


class _Closed(Exception):
    pass


class _Handle:
    """A pipe handle closed by whichever thread is last to stop using it."""

    def __init__(self, handle: int) -> None:
        self._handle = handle
        self._lock = threading.Lock()
        self._users = 0
        self.closed = False
        self._pending: list[Any] = []

    def enter(self) -> int:
        with self._lock:
            if self.closed:
                raise OSError("pipe is closed")
            self._users += 1
            return self._handle

    def leave(self) -> None:
        with self._lock:
            self._users -= 1
            release = self.closed and self._users == 0
        if release:
            self._release()

    def keep(self, ov: Any) -> None:
        self._pending.append(ov)

    def forget(self, ov: Any) -> None:
        self._pending.remove(ov)

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            release = self._users == 0
        if release:
            self._release()

    def wait(self, ov: Any, timeout: float | None) -> bool:
        """Wait for `ov`; False on timeout. Raises _Closed if closed meanwhile."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            step = _POLL_MS
            if deadline is not None:
                step = max(0, min(step, int((deadline - time.monotonic()) * 1000)))
            result = _winapi.WaitForMultipleObjects([ov.event], False, step)
            if result == _winapi.WAIT_OBJECT_0:
                return True
            if self.closed:
                _cancel(ov)
                raise _Closed
            if deadline is not None and time.monotonic() >= deadline:
                return False

    def _release(self) -> None:
        for ov in self._pending:
            _cancel(ov)
        self._pending.clear()
        with contextlib.suppress(OSError):
            _winapi.CloseHandle(self._handle)


def _cancel(ov: Any) -> None:
    try:
        ov.cancel()
        ov.GetOverlappedResult(True)
    except OSError:
        pass


class PipeStream:
    """One end of a connected pipe: recv, sendall, settimeout and close."""

    def __init__(self, handle: int) -> None:
        self._handle = _Handle(handle)
        self._timeout: float | None = None
        self._reading: Any = None
        self._buffer = b""

    def settimeout(self, timeout: float | None) -> None:
        self._timeout = timeout

    def recv(self, size: int) -> bytes:
        """As socket.recv: b"" once the other end has gone."""
        if self._buffer:
            data, self._buffer = self._buffer[:size], self._buffer[size:]
            return data
        try:
            handle = self._handle.enter()
        except OSError:
            return b""
        try:
            if self._reading is None:
                try:
                    ov, _ = _winapi.ReadFile(handle, max(size, _BUFSIZE), True)
                except OSError:
                    return b""
                self._reading = ov
                self._handle.keep(ov)
            ov = self._reading
            try:
                if not self._handle.wait(ov, self._timeout):
                    raise socket.timeout("timed out")
            except _Closed:
                self._handle.forget(ov)
                self._reading = None
                return b""
            self._handle.forget(ov)
            self._reading = None
            try:
                ov.GetOverlappedResult(True)
            except OSError:
                return b""
            data = bytes(ov.getbuffer())
        finally:
            self._handle.leave()
        if not data:
            return b""
        data, self._buffer = data[:size], data[size:]
        return data

    def sendall(self, data: bytes) -> None:
        handle = self._handle.enter()
        try:
            view = memoryview(bytes(data))
            while view:
                ov, _ = _winapi.WriteFile(handle, view, True)
                try:
                    self._handle.wait(ov, None)
                except _Closed:
                    raise OSError("pipe closed while writing") from None
                written, _ = ov.GetOverlappedResult(True)
                view = view[written:]
        finally:
            self._handle.leave()

    def shutdown(self, _how: int) -> None:
        self.close()

    def close(self) -> None:
        self._handle.close()


class PipeListener:
    """Accepts connections on a named pipe, always keeping one instance waiting."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._timeout: float | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._connecting: Any = None
        self._waiting = self._new_instance(first=True)

    def _new_instance(self, first: bool = False) -> int:
        mode = _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_OVERLAPPED
        if first:
            mode |= _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE
        return _winapi.CreateNamedPipe(
            self.name,
            mode,
            _PIPE_TYPE_BYTE | _PIPE_REJECT_REMOTE_CLIENTS | _winapi.PIPE_WAIT,
            _winapi.PIPE_UNLIMITED_INSTANCES,
            _BUFSIZE,
            _BUFSIZE,
            _winapi.NMPWAIT_WAIT_FOREVER,
            _winapi.NULL,
        )

    def settimeout(self, timeout: float | None) -> None:
        self._timeout = timeout

    def accept(self) -> tuple[PipeStream, str]:
        with self._lock:
            if self._closed:
                raise OSError("listener is closed")
            if self._connecting is None:
                try:
                    self._connecting = _winapi.ConnectNamedPipe(self._waiting, True)
                except OSError as err:
                    # A client that connected and left already: hand it over anyway.
                    if err.winerror != _winapi.ERROR_NO_DATA:
                        raise
                    return self._hand_over(), self.name
            ov = self._connecting
            timeout = -1 if self._timeout is None else self._timeout
            wait_ms = _winapi.INFINITE if timeout < 0 else int(timeout * 1000)
            if _winapi.WaitForMultipleObjects([ov.event], False, wait_ms) != 0:
                raise socket.timeout("timed out")
            self._connecting = None
            with contextlib.suppress(OSError):
                ov.GetOverlappedResult(True)
            return self._hand_over(), self.name

    def _hand_over(self) -> PipeStream:
        connected, self._waiting = self._waiting, self._new_instance()
        return PipeStream(connected)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._connecting is not None:
                _cancel(self._connecting)
                self._connecting = None
            with contextlib.suppress(OSError):
                _winapi.CloseHandle(self._waiting)


def connect(name: str, timeout: float = 2.0) -> PipeStream:
    """Open the client end; FileNotFoundError when no server has the pipe."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle = _winapi.CreateFile(
                name,
                _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
                0,
                _winapi.NULL,
                _winapi.OPEN_EXISTING,
                _winapi.FILE_FLAG_OVERLAPPED,
                _winapi.NULL,
            )
            return PipeStream(handle)
        except OSError as err:
            if err.winerror != _winapi.ERROR_PIPE_BUSY:
                raise
            if time.monotonic() >= deadline:
                raise
            try:
                _winapi.WaitNamedPipe(name, 100)
            except OSError:
                time.sleep(0.01)
