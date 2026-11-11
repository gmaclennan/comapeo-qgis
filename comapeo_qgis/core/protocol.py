"""Sans-I/O client for the CoMapeo local API (PROTOCOL.md, version 1).

The caller owns the transport. Feed received bytes to `receive_data()`, write
whatever `data_to_send()` returns, call `tick()` when `next_deadline()` passes
and `connection_lost()` when the transport closes. One instance per connection.

Responses larger than `max_frame_size` (16 MiB) close the connection, so keep
`layers.getFeatures` limits at the default 1000 or below.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Union

from .framing import (
    HANDSHAKE_MAX_FRAME_SIZE,
    MAX_FRAME_SIZE,
    FrameDecoder,
    FrameError,
    encode_frame,
)

SUPPORTED_PROTOCOL_VERSIONS: tuple[int, ...] = (1,)

UNSUPPORTED_PROTOCOL_VERSION = "UNSUPPORTED_PROTOCOL_VERSION"
METHOD_NOT_FOUND = "METHOD_NOT_FOUND"
INVALID_PARAMS = "INVALID_PARAMS"
PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
LAYER_NOT_FOUND = "LAYER_NOT_FOUND"
INVALID_CURSOR = "INVALID_CURSOR"
INTERNAL_ERROR = "INTERNAL_ERROR"

# Local codes, never sent by the server.
TIMEOUT = "TIMEOUT"
CONNECTION_CLOSED = "CONNECTION_CLOSED"

UPDATE_APP = "app"
UPDATE_PLUGIN = "plugin"

DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_HANDSHAKE_TIMEOUT = 10.0

_HELLO_ID = 1


class ClientState(Enum):
    IDLE = "idle"
    HANDSHAKING = "handshaking"
    READY = "ready"
    CLOSED = "closed"


@dataclass(frozen=True)
class RequestError:
    code: str
    message: str
    data: Any = None


@dataclass(frozen=True)
class Authenticated:
    protocol_version: int
    server: Mapping[str, Any]
    app: Mapping[str, Any]


@dataclass(frozen=True)
class Response:
    request_id: int
    method: str
    result: Any


@dataclass(frozen=True)
class RequestFailed:
    request_id: int
    method: str
    error: RequestError


@dataclass(frozen=True)
class Notification:
    method: str
    params: Mapping[str, Any]


@dataclass(frozen=True)
class VersionMismatch:
    """No shared protocol version. `update` is UPDATE_APP, UPDATE_PLUGIN or None."""

    client_versions: tuple[int, ...]
    server_versions: tuple[int, ...]
    update: str | None


@dataclass(frozen=True)
class HandshakeRejected:
    """The server closed or refused before agreeing a version (e.g. wrong secret)."""

    reason: str


@dataclass(frozen=True)
class ProtocolViolation:
    message: str


@dataclass(frozen=True)
class Closed:
    """Emitted exactly once per connection, after any failure events."""

    was_authenticated: bool


# typing.Union, not `|`: QGIS 3.40 may bundle Python 3.9.
Event = Union[
    Authenticated,
    Response,
    RequestFailed,
    Notification,
    VersionMismatch,
    HandshakeRejected,
    ProtocolViolation,
    Closed,
]


class ClientError(Exception):
    pass


@dataclass
class _Pending:
    method: str
    deadline: float | None


def update_target(
    client_versions: Iterable[int], server_versions: Iterable[int]
) -> str | None:
    """Which side is too old when the two sets of versions do not overlap."""
    client = sorted(set(client_versions))
    server = sorted(set(server_versions))
    if not client or not server:
        return None
    if max(server) < min(client):
        return UPDATE_APP
    if min(server) > max(client):
        return UPDATE_PLUGIN
    return UPDATE_PLUGIN if max(server) > max(client) else UPDATE_APP


class ClientConnection:
    def __init__(
        self,
        secret: str,
        client_name: str,
        client_version: str,
        *,
        protocol_versions: Iterable[int] = SUPPORTED_PROTOCOL_VERSIONS,
        request_timeout: float | None = DEFAULT_REQUEST_TIMEOUT,
        handshake_timeout: float | None = DEFAULT_HANDSHAKE_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
        max_frame_size: int = MAX_FRAME_SIZE,
    ) -> None:
        self._secret: str | None = secret
        self._client = {"name": client_name, "version": client_version}
        self._versions = tuple(protocol_versions)
        self._request_timeout = request_timeout
        self._handshake_timeout = handshake_timeout
        self._clock = clock
        self._max_frame_size = max_frame_size
        self._decoder = FrameDecoder(max_frame_size)
        self._state = ClientState.IDLE
        self._out = bytearray()
        self._queued: list[tuple[int, bytes]] = []
        self._pending: dict[int, _Pending] = {}
        self._next_id = _HELLO_ID + 1
        self._handshake_deadline: float | None = None
        self._authenticated = False
        self._wants_close = False
        self.protocol_version: int | None = None
        self.server_info: Mapping[str, Any] = {}
        self.app_info: Mapping[str, Any] = {}

    @property
    def state(self) -> ClientState:
        return self._state

    @property
    def wants_close(self) -> bool:
        """True once the client has given up on the connection; close the transport."""
        return self._wants_close

    @property
    def pending_requests(self) -> int:
        return len(self._pending)

    def start(self) -> None:
        """Queue the hello frame. Call once the transport is connected.

        Raises ClientError if the hello cannot fit the 4 KiB handshake limit.
        """
        if self._state is not ClientState.IDLE:
            raise ClientError(f"cannot start from state {self._state.value}")
        hello = {
            "id": _HELLO_ID,
            "method": "hello",
            "params": {
                "secret": self._secret,
                "protocolVersions": list(self._versions),
                "client": self._client,
            },
        }
        self._out += _encode(hello, HANDSHAKE_MAX_FRAME_SIZE)
        self._secret = None
        self._state = ClientState.HANDSHAKING
        if self._handshake_timeout is not None:
            self._handshake_deadline = self._clock() + self._handshake_timeout

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> int:
        """Send a request (queued until authenticated). Returns its id.

        Raises ClientError if the connection is closed or the params cannot be
        sent (not JSON-serialisable, or over the frame size limit).
        """
        if self._state is ClientState.CLOSED:
            raise ClientError("connection is closed")
        if method == "hello":
            raise ClientError("hello is sent by start()")
        request_id = self._next_id
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = dict(params)
        frame = _encode(message, self._max_frame_size)
        self._next_id += 1
        limit = self._request_timeout if timeout is None else timeout
        deadline = None if limit is None else self._clock() + limit
        self._pending[request_id] = _Pending(method, deadline)
        if self._state is ClientState.READY:
            self._out += frame
        else:
            self._queued.append((request_id, frame))
        return request_id

    def data_to_send(self) -> bytes:
        data = bytes(self._out)
        self._out.clear()
        return data

    def receive_data(self, data: bytes) -> list[Event]:
        if self._state is ClientState.CLOSED:
            return []
        events: list[Event] = []
        try:
            self._decoder.append(data)
            for message in self._decoder.messages():
                events.extend(self._handle(message))
                if self._state is ClientState.CLOSED:
                    break
        except FrameError as err:
            events.extend(self._fail(ProtocolViolation(str(err))))
        return events

    def next_deadline(self) -> float | None:
        if self._state is ClientState.CLOSED:
            return None
        deadlines = [
            p.deadline for p in self._pending.values() if p.deadline is not None
        ]
        if (
            self._state is ClientState.HANDSHAKING
            and self._handshake_deadline is not None
        ):
            deadlines.append(self._handshake_deadline)
        return min(deadlines) if deadlines else None

    def tick(self, now: float | None = None) -> list[Event]:
        if self._state is ClientState.CLOSED:
            return []
        now = self._clock() if now is None else now
        if (
            self._state is ClientState.HANDSHAKING
            and self._handshake_deadline is not None
            and now >= self._handshake_deadline
        ):
            return self._fail(HandshakeRejected("no reply to hello before timeout"))
        events: list[Event] = []
        for request_id, pending in list(self._pending.items()):
            if pending.deadline is not None and now >= pending.deadline:
                del self._pending[request_id]
                error = RequestError(TIMEOUT, f"{pending.method} timed out")
                events.append(RequestFailed(request_id, pending.method, error))
        return events

    def close(self) -> list[Event]:
        """Close from the client side. The caller then closes the transport."""
        if self._state is ClientState.CLOSED:
            return []
        self._wants_close = True
        return self._finish([])

    def connection_lost(self) -> list[Event]:
        """The transport closed. Safe to call more than once."""
        if self._state is ClientState.CLOSED:
            return []
        if self._state is ClientState.HANDSHAKING:
            return self._finish([HandshakeRejected("server closed the connection")])
        return self._finish([])

    def _handle(self, message: Any) -> list[Event]:
        if not isinstance(message, dict):
            return self._fail(ProtocolViolation("message is not a JSON object"))
        if self._state is ClientState.IDLE:
            return self._fail(ProtocolViolation("server spoke before hello"))
        if self._state is ClientState.HANDSHAKING:
            request_id = message.get("id")
            if type(request_id) is not int or request_id != _HELLO_ID:
                return self._fail(
                    ProtocolViolation(
                        "server sent something other than the hello reply"
                    )
                )
            return self._handle_hello_reply(message)
        if "id" not in message:
            method = message.get("method")
            if not isinstance(method, str):
                return []
            params = message.get("params")
            return [Notification(method, params if isinstance(params, dict) else {})]
        request_id = message.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            return []
        pending = self._pending.pop(request_id, None)
        if pending is None:
            return []
        if "error" in message:
            return [RequestFailed(request_id, pending.method, _parse_error(message))]
        return [Response(request_id, pending.method, message.get("result"))]

    def _handle_hello_reply(self, message: dict[str, Any]) -> list[Event]:
        if "error" in message:
            error = _parse_error(message)
            if error.code == UNSUPPORTED_PROTOCOL_VERSION:
                supported = _int_list(
                    error.data.get("supported") if isinstance(error.data, dict) else []
                )
                mismatch = VersionMismatch(
                    self._versions, supported, update_target(self._versions, supported)
                )
                return self._fail(mismatch)
            return self._fail(HandshakeRejected(f"{error.code}: {error.message}"))
        result = message.get("result")
        version = result.get("protocolVersion") if isinstance(result, dict) else None
        if type(version) is not int or version not in self._versions:
            return self._fail(
                ProtocolViolation(f"server agreed to unoffered version {version!r}")
            )
        server = result.get("server")
        app = result.get("app")
        self.protocol_version = version
        self.server_info = server if isinstance(server, dict) else {}
        self.app_info = app if isinstance(app, dict) else {}
        self._state = ClientState.READY
        self._authenticated = True
        self._handshake_deadline = None
        for request_id, frame in self._queued:
            if request_id in self._pending:
                self._out += frame
        self._queued.clear()
        return [Authenticated(version, self.server_info, self.app_info)]

    def _fail(self, event: Event) -> list[Event]:
        self._wants_close = True
        return self._finish([event])

    def _finish(self, events: list[Event]) -> list[Event]:
        self._state = ClientState.CLOSED
        self._out.clear()
        self._queued.clear()
        for request_id, pending in self._pending.items():
            error = RequestError(CONNECTION_CLOSED, "connection closed")
            events.append(RequestFailed(request_id, pending.method, error))
        self._pending.clear()
        events.append(Closed(self._authenticated))
        return events


def _encode(message: Any, limit: int) -> bytes:
    try:
        return encode_frame(message, limit)
    except FrameError as err:
        raise ClientError(str(err)) from err
    except (TypeError, ValueError, RecursionError) as err:
        raise ClientError(f"cannot encode as JSON: {err}") from err


def _parse_error(message: Mapping[str, Any]) -> RequestError:
    error = message.get("error")
    if not isinstance(error, dict):
        return RequestError("UNKNOWN", str(error))
    code = error.get("code")
    text = error.get("message")
    return RequestError(
        code if isinstance(code, str) else "UNKNOWN",
        text if isinstance(text, str) else "",
        error.get("data"),
    )


def _int_list(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, int) and not isinstance(v, bool))


@dataclass
class Backoff:
    """Reconnect delays: initial, then doubling up to `maximum`."""

    initial: float = 0.5
    maximum: float = 30.0
    factor: float = 2.0
    _next: float | None = None

    def next_delay(self) -> float:
        delay = self.initial if self._next is None else self._next
        self._next = min(delay * self.factor, self.maximum)
        return delay

    def reset(self) -> None:
        self._next = None
