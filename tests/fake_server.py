"""A small server implementing PROTOCOL.md version 1 over a real Unix socket,
or a named pipe on Windows.

Written from the protocol document, independently of the client code, so it
uses its own framing rather than comapeo_qgis.core.framing.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hmac
import json
import os
import secrets
import shutil
import socket
import struct
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Any

HANDSHAKE_LIMIT = 4 * 1024
FRAME_LIMIT = 16 * 1024 * 1024

SVG_ICON = b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>'


def short_tempdir() -> str:
    # pytest's tmp_path on macOS is too long for a Unix socket path.
    return tempfile.mkdtemp(prefix="cq-", dir="/tmp" if os.name == "posix" else None)


def is_pipe(path: str) -> bool:
    return path.lower().startswith("\\\\.\\pipe\\")


def new_endpoint(directory: str) -> str:
    """A fresh address to listen on: a socket in `directory`, or a named pipe."""
    if sys.platform == "win32":
        from tests import winpipe

        return winpipe.new_pipe_name()
    return os.path.join(directory, "api.sock")


def listen(path: str) -> Any:
    """A listener with accept(), settimeout() and close(), like a socket's."""
    if is_pipe(path):
        from tests import winpipe

        return winpipe.PipeListener(path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen()
    return listener


def frame(message: Any) -> bytes:
    payload = json.dumps(message).encode("utf-8")
    return struct.pack("<I", len(payload)) + payload


def observation(
    doc_id: str,
    version: str = "v1",
    lon: float | None = -58.1,
    lat: float | None = -3.2,
    **properties: Any,
) -> dict[str, Any]:
    props = {"id": doc_id, "category": "School", "tags": {}}
    props.update(properties)
    return {
        "id": doc_id,
        "version": version,
        "geometry": None
        if lon is None
        else {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
        "categoryId": "cat-school",
        "attachments": [],
    }


def track(doc_id: str, version: str = "v1") -> dict[str, Any]:
    return {
        "id": doc_id,
        "version": version,
        "geometry": {
            "type": "LineString",
            "coordinates": [[-58.0, -3.0], [-58.1, -3.1]],
        },
        "times": ["2026-01-01T10:00:00.000Z", "2026-01-01T10:05:00.000Z"],
        "properties": {"id": doc_id, "category": None},
        "categoryId": None,
        "attachments": [],
    }


def default_layers() -> list[dict[str, Any]]:
    return [
        {
            "layerId": "observations",
            "title": "Observations",
            "docType": "observation",
            "geometryType": "Point",
            "schemaVersion": "s1",
            "columns": [
                {"name": "id", "type": "string", "label": "ID", "role": "builtin"},
                {
                    "name": "category",
                    "type": "string",
                    "label": "Category",
                    "role": "builtin",
                },
                {
                    "name": "building_type",
                    "type": "string",
                    "label": "Building type",
                    "role": "field",
                    "options": [{"value": "school", "label": "School"}],
                },
                {"name": "tags", "type": "json", "label": "All tags", "role": "tags"},
            ],
        },
        {
            "layerId": "tracks",
            "title": "Tracks",
            "docType": "track",
            "geometryType": "LineString",
            "schemaVersion": "s1",
            "columns": [
                {"name": "id", "type": "string", "label": "ID", "role": "builtin"},
                {
                    "name": "category",
                    "type": "string",
                    "label": "Category",
                    "role": "builtin",
                },
            ],
        },
    ]


@dataclass
class FakeProject:
    project_id: str
    name: str
    layers: list[dict[str, Any]] = field(default_factory=default_layers)
    features: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    categories: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "categoryId": "cat-school",
                "name": "School",
                "color": "#ff0000",
                "icon": {
                    "mimeType": "image/svg+xml",
                    "data": base64.b64encode(SVG_ICON).decode(),
                },
            }
        ]
    )


class _Connection:
    def __init__(self, server: FakeServer, sock: socket.socket) -> None:
        self.server = server
        self.sock = sock
        self.subscriptions: set[str] = set()
        self.authenticated = False
        self.lock = threading.Lock()

    def send(self, message: Any) -> None:
        data = frame(message)
        with self.lock:
            chunk = self.server.chunk_size or len(data)
            for i in range(0, len(data), chunk):
                self.sock.sendall(data[i : i + chunk])

    def send_raw(self, data: bytes) -> None:
        with self.lock:
            self.sock.sendall(data)

    def read_frame(self, limit: int) -> Any:
        header = self._read_exact(4)
        (size,) = struct.unpack("<I", header)
        if size > limit:
            raise ConnectionError("over-length frame")
        return json.loads(self._read_exact(size).decode("utf-8"))

    def _read_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError("closed")
            data += chunk
        return bytes(data)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.sock.close()


class FakeServer:
    def __init__(
        self,
        *,
        protocol_versions: tuple[int, ...] = (1,),
        handshake_timeout: float = 5.0,
        chunk_size: int | None = None,
        socket_path: str | None = None,
        secret: str | None = None,
        app_name: str = "CoMapeo Desktop",
    ) -> None:
        self.app_name = app_name
        self._own_dir = socket_path is None or is_pipe(socket_path)
        self.dir = short_tempdir() if self._own_dir else os.path.dirname(socket_path)
        self.socket_path = socket_path or new_endpoint(self.dir)
        self.secret = secret or (
            base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        )
        self.protocol_versions = protocol_versions
        self.handshake_timeout = handshake_timeout
        self.chunk_size = chunk_size
        self.cursor_epoch = 0
        self.projects: dict[str, FakeProject] = {}
        self.requests: list[dict[str, Any]] = []
        self.connections: list[_Connection] = []
        self._listener: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self.connected = threading.Event()
        self.open_connections = 0
        self.accepted = 0
        self.failures: list[tuple[str, dict[str, Any], str]] = []
        self.stuck_cursor: str | None = None

    def fail_next(self, method: str, code: str, **params: Any) -> None:
        """The next `method` request whose params include `params` gets `code`."""
        self.failures.append((method, params, code))

    def add_project(self, project: FakeProject) -> FakeProject:
        self.projects[project.project_id] = project
        return project

    def write_discovery(self, directory: str | None = None) -> str:
        directory = directory or self.dir
        path = os.path.join(directory, "comapeo-local-api.json")
        with open(path, "w") as f:
            json.dump(
                {
                    "discoveryVersion": 1,
                    "socketPath": self.socket_path,
                    "secret": self.secret,
                    "protocolVersions": list(self.protocol_versions),
                    "pid": os.getpid(),
                    "app": {"name": self.app_name, "version": "9.9.9"},
                },
                f,
            )
        os.chmod(path, 0o600)
        return path

    def start(self) -> FakeServer:
        listener = listen(self.socket_path)
        listener.settimeout(0.1)
        self._listener = listener
        thread = threading.Thread(target=self._accept_loop, daemon=True)
        thread.start()
        self._threads.append(thread)
        return self

    def stop(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        self.drop_connections()
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads.clear()
        if not is_pipe(self.socket_path):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.socket_path)
        if self._own_dir:
            shutil.rmtree(self.dir, ignore_errors=True)

    def drop_connections(self) -> None:
        with self._lock:
            connections, self.connections = self.connections, []
        for conn in connections:
            conn.close()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        project_id = params.get("projectId")
        with self._lock:
            targets = [c for c in self.connections if project_id in c.subscriptions]
        for conn in targets:
            conn.send({"method": method, "params": params})

    def broadcast_raw(self, data: bytes) -> None:
        with self._lock:
            targets = [c for c in self.connections if c.authenticated]
        for conn in targets:
            conn.send_raw(data)

    def __enter__(self) -> FakeServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _accept_loop(self) -> None:
        while self._listener is not None:
            try:
                sock, _ = self._listener.accept()
            except socket.timeout:  # not TimeoutError before Python 3.10
                continue
            except OSError:
                return
            conn = _Connection(self, sock)
            with self._lock:
                self.connections.append(conn)
                self.open_connections += 1
                self.accepted += 1
            self.connected.set()
            thread = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            thread.start()
            self._threads.append(thread)

    def _serve(self, conn: _Connection) -> None:
        try:
            if not self._handshake(conn):
                return
            while True:
                message = conn.read_frame(FRAME_LIMIT)
                self.requests.append(message)
                reply = self._dispatch(conn, message)
                if reply is not None:
                    conn.send(reply)
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            conn.close()
            with self._lock:
                self.open_connections -= 1
                if conn in self.connections:
                    self.connections.remove(conn)

    def _handshake(self, conn: _Connection) -> bool:
        conn.sock.settimeout(self.handshake_timeout)
        try:
            hello = conn.read_frame(HANDSHAKE_LIMIT)
        except (TimeoutError, ConnectionError, OSError, ValueError):
            return False
        conn.sock.settimeout(None)
        self.requests.append(hello)
        params = hello.get("params") if isinstance(hello, dict) else None
        if (
            not isinstance(hello, dict)
            or hello.get("method") != "hello"
            or not isinstance(params, dict)
            or not isinstance(params.get("secret"), str)
        ):
            return False
        if not hmac.compare_digest(params["secret"].encode(), self.secret.encode()):
            return False
        offered = params.get("protocolVersions") or []
        shared = [v for v in self.protocol_versions if v in offered]
        if not shared:
            conn.send(
                {
                    "id": hello.get("id"),
                    "error": {
                        "code": "UNSUPPORTED_PROTOCOL_VERSION",
                        "message": "no shared protocol version",
                        "data": {"supported": list(self.protocol_versions)},
                    },
                }
            )
            return False
        conn.authenticated = True
        conn.send(
            {
                "id": hello.get("id"),
                "result": {
                    "protocolVersion": max(shared),
                    "server": {"name": "@comapeo/local-api", "version": "0.0.0"},
                    "app": {"name": self.app_name, "version": "9.9.9"},
                },
            }
        )
        return True

    def _dispatch(self, conn: _Connection, message: Any) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        handler = {
            "projects.list": self._projects_list,
            "layers.list": self._layers_list,
            "layers.getFeatures": self._get_features,
            "categories.list": self._categories_list,
            "subscribe": lambda p: self._subscribe(conn, p, True),
            "unsubscribe": lambda p: self._subscribe(conn, p, False),
        }.get(method)
        if handler is None:
            return _error(request_id, "METHOD_NOT_FOUND", f"no method {method}")
        for failure in list(self.failures):
            wanted, subset, code = failure
            if wanted == method and all(params.get(k) == v for k, v in subset.items()):
                self.failures.remove(failure)
                return _error(request_id, code, "injected failure")
        try:
            return {"id": request_id, "result": handler(params)}
        except _Failure as failure:
            return _error(request_id, failure.code, failure.message)

    def _project(self, params: dict[str, Any]) -> FakeProject:
        project = self.projects.get(params.get("projectId"))
        if project is None:
            raise _Failure("PROJECT_NOT_FOUND", "no such project")
        return project

    def _projects_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "projects": [
                {"projectId": p.project_id, "name": p.name}
                for p in self.projects.values()
            ]
        }

    def _layers_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"layers": copy.deepcopy(self._project(params).layers)}

    def _categories_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"categories": copy.deepcopy(self._project(params).categories)}

    def _subscribe(
        self, conn: _Connection, params: dict[str, Any], on: bool
    ) -> dict[str, Any]:
        project = self._project(params)
        if on:
            conn.subscriptions.add(project.project_id)
        else:
            conn.subscriptions.discard(project.project_id)
        return {}

    def _get_features(self, params: dict[str, Any]) -> dict[str, Any]:
        project = self._project(params)
        layer_id = params.get("layerId")
        if layer_id not in {layer["layerId"] for layer in project.layers}:
            raise _Failure("LAYER_NOT_FOUND", "no such layer")
        limit = params.get("limit", 1000)
        if not isinstance(limit, int) or not 1 <= limit <= 5000:
            raise _Failure("INVALID_PARAMS", "bad limit")
        features = project.features.get(layer_id, [])
        ids = params.get("ids")
        removed = None
        if ids is not None:
            present = {f["id"] for f in features}
            features = [f for f in features if f["id"] in ids]
            removed = [i for i in ids if i not in present]
        cursor = params.get("cursor")
        stuck = cursor is not None and cursor == self.stuck_cursor
        start = 0 if cursor is None or stuck else self._decode_cursor(cursor)
        if start < 0 or start > len(features):
            raise _Failure("INVALID_CURSOR", "unknown cursor")
        page = features[start : start + limit]
        end = start + len(page)
        next_cursor = f"{self.cursor_epoch}:{end}" if end < len(features) else None
        if self.stuck_cursor is not None:
            page, next_cursor = features[:limit], self.stuck_cursor
        result: dict[str, Any] = {
            "features": copy.deepcopy(page),
            "nextCursor": next_cursor,
        }
        if removed is not None:
            result["removed"] = removed
        return result

    def _decode_cursor(self, cursor: Any) -> int:
        """Cursors from before the last `cursor_epoch` bump are refused."""
        try:
            epoch, offset = str(cursor).split(":")
            if int(epoch) == self.cursor_epoch:
                return int(offset)
        except ValueError:
            pass
        raise _Failure("INVALID_CURSOR", "unknown cursor")


class _Failure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _error(request_id: Any, code: str, message: str) -> dict[str, Any]:
    return {"id": request_id, "error": {"code": code, "message": message}}
