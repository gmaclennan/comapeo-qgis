"""A small server implementing PROTOCOL.md version 1 over a real Unix socket,
or a named pipe on Windows, editing included.

Written from the protocol document, independently of the client code, so it
uses its own framing rather than comapeo_qgis.core.framing.

Editing follows the contract's checks in their order, with the error data it
gives. What the dev server cannot be made to do is set on the instance and may
change at any time: `allow_edits` (None: a server that predates editing, with
no editing members and no editing methods), `role` ("coordinator", "member" or
"blocked"), `edit_delay` (seconds before each edit is answered),
`concurrent_next` (the next edit meets a change landing at the same moment)
and `changes_before_edit` (callables run inside the next edit, after its checks
would have passed on the old state: a change from elsewhere).
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
import time
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
    *,
    forks: list[str] | None = None,
    category_id: str = "cat-school",
    **properties: Any,
) -> dict[str, Any]:
    props = {
        "id": doc_id,
        "category": "School",
        "notes": None,
        "building_type": None,
        "tags": {},
    }
    props.update(properties)
    return {
        "id": doc_id,
        "version": version,
        "forks": list(forks or []),
        "geometry": None
        if lon is None
        else {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
        "categoryId": category_id,
        "attachments": [],
    }


def track(doc_id: str, version: str = "v1") -> dict[str, Any]:
    return {
        "id": doc_id,
        "version": version,
        "forks": [],
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
            "editOperations": ["properties", "category", "delete"],
            "columns": [
                {"name": "id", "type": "string", "label": "ID", "role": "builtin"},
                {
                    "name": "category",
                    "type": "string",
                    "label": "Category",
                    "role": "builtin",
                },
                {
                    "name": "notes",
                    "type": "string",
                    "label": "Notes",
                    "role": "builtin",
                    "editable": True,
                },
                {
                    "name": "building_type",
                    "type": "string",
                    "label": "Building type",
                    "role": "field",
                    "editable": True,
                    "categoryIds": ["cat-school"],
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
            "editOperations": [],
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


_EDIT_LAYER_MEMBERS = ("editable", "readOnlyReason", "editOperations")
_EDIT_COLUMN_MEMBERS = ("editable", "categoryIds")
_EDIT_FEATURE_MEMBERS = ("editable", "forks")


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
                "geometryTypes": ["Point"],
                "icon": {
                    "mimeType": "image/svg+xml",
                    "data": base64.b64encode(SVG_ICON).decode(),
                },
            },
        ]
    )
    # Ids of observations created on another device; a member may not edit them.
    others: set[str] = field(default_factory=set)
    deleted: set[str] = field(default_factory=set)


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
        allow_edits: bool | None = True,
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
        self.failures: list[tuple[str, dict[str, Any], str, Any]] = []
        self.stuck_cursor: str | None = None
        self.allow_edits: bool | None = allow_edits
        self.role = "coordinator"
        self.edit_delay = 0.0
        self.concurrent_next = False
        self.changes_before_edit: list[Any] = []
        self.drop_after_edits: int | None = None
        self._edits = 0
        self._versions = 0

    def fail_next(
        self, method: str, code: str, data: Any = None, **params: Any
    ) -> None:
        """The next `method` request whose params include `params` gets `code`."""
        self.failures.append((method, params, code, data))

    def edits(self) -> list[dict[str, Any]]:
        """The editing requests received, in order."""
        return [
            r
            for r in self.requests
            if r.get("method") in ("features.update", "features.delete")
        ]

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
                editing = message.get("method") in (
                    "features.update",
                    "features.delete",
                )
                if editing and self.edit_delay:
                    time.sleep(self.edit_delay)
                limit = self.drop_after_edits
                if editing and limit is not None and self._edits >= limit:
                    self._edits += 1
                    raise ConnectionError("dropped by the test")
                reply = self._dispatch(conn, message)
                if reply is not None:
                    conn.send(reply)
                if editing:
                    self._edits += 1
                    self._after_edit(message, reply)
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
        }
        if self.allow_edits is not None:
            handler["features.update"] = self._update
            handler["features.delete"] = self._delete
        handler = handler.get(method)
        if handler is None:
            return _error(request_id, "METHOD_NOT_FOUND", f"no method {method}")
        for failure in list(self.failures):
            wanted, subset, code, data = failure
            if wanted == method and all(params.get(k) == v for k, v in subset.items()):
                self.failures.remove(failure)
                return _error(request_id, code, "injected failure", data)
        try:
            with self._lock:
                return {"id": request_id, "result": handler(params)}
        except _Failure as failure:
            return _error(request_id, failure.code, failure.message, failure.data)

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
        layers = copy.deepcopy(self._project(params).layers)
        for layer in layers:
            if self.allow_edits is None:
                for name in _EDIT_LAYER_MEMBERS:
                    layer.pop(name, None)
                for column in layer.get("columns", []):
                    for name in _EDIT_COLUMN_MEMBERS:
                        column.pop(name, None)
                continue
            reason = self._read_only_reason(layer)
            layer["editable"] = reason is None
            layer.setdefault("editOperations", [])
            if reason is not None:
                layer["readOnlyReason"] = reason
        return {"layers": layers}

    def _read_only_reason(self, layer: dict[str, Any]) -> str | None:
        if not layer.get("editOperations"):
            return "unsupported"
        if not self.allow_edits:
            return "disabled"
        if self.role == "blocked":
            return "role"
        return None

    def _categories_list(self, params: dict[str, Any]) -> dict[str, Any]:
        categories = copy.deepcopy(self._project(params).categories)
        if self.allow_edits is None:
            for category in categories:
                category.pop("geometryTypes", None)
        return {"categories": categories}

    def _served(self, project: FakeProject, feature: dict[str, Any]) -> dict[str, Any]:
        served = copy.deepcopy(feature)
        if self.allow_edits is None:
            for name in _EDIT_FEATURE_MEMBERS:
                served.pop(name, None)
        else:
            served["forks"] = sorted(served.get("forks") or [])
            served["editable"] = self._may_edit(project, feature["id"])
        return served

    def _may_edit(self, project: FakeProject, doc_id: str) -> bool:
        if self.role == "blocked":
            return False
        return self.role != "member" or doc_id not in project.others

    # Editing, in the order of the contract's checks

    def _edit_target(
        self, params: dict[str, Any], method: str
    ) -> tuple[FakeProject, dict[str, Any], dict[str, Any], list[str]]:
        if not isinstance(params.get("id"), str) or not isinstance(
            params.get("version"), str
        ):
            raise _Failure("INVALID_PARAMS", "id and version are required")
        forks = params.get("forks")
        forks = [] if forks is None else forks
        if not isinstance(forks, list) or not all(isinstance(f, str) for f in forks):
            raise _Failure("INVALID_PARAMS", "forks must be a list of strings")
        if method == "update":
            properties = params.get("properties")
            if properties is not None and not isinstance(properties, dict):
                raise _Failure("INVALID_PARAMS", "properties must be an object")
            if "categoryId" in params and not isinstance(params["categoryId"], str):
                raise _Failure("INVALID_PARAMS", "categoryId must be a string")
            geometry = params.get("geometry")
            if "geometry" in params and (
                not isinstance(geometry, dict) or geometry.get("type") != "Point"
            ):
                raise _Failure("INVALID_PARAMS", "geometry must be a Point")
            if (
                not properties
                and "categoryId" not in params
                and "geometry" not in params
            ):
                raise _Failure("INVALID_PARAMS", "nothing to change")
        if not self.allow_edits:
            raise _Failure("EDITING_DISABLED", "editing is switched off")
        project = self._project(params)
        layers = {layer["layerId"]: layer for layer in project.layers}
        layer = layers.get(params.get("layerId"))
        if layer is None:
            raise _Failure("LAYER_NOT_FOUND", "no such layer")
        return project, layer, params, forks

    def _check_offered(
        self, layer: dict[str, Any], params: dict[str, Any], op: str
    ) -> None:
        ops = layer.get("editOperations") or []
        if not ops:
            raise _Failure("NOT_EDITABLE", "layer", {"target": "layer"})
        if op == "delete" and "delete" not in ops:
            raise _Failure("NOT_EDITABLE", "delete", {"target": "delete"})
        if "categoryId" in params and "category" not in ops:
            raise _Failure("NOT_EDITABLE", "category", {"target": "category"})
        if "geometry" in params and "move" not in ops:
            raise _Failure("NOT_EDITABLE", "geometry", {"target": "geometry"})
        columns = {c["name"]: c for c in layer.get("columns", [])}
        for name in params.get("properties") or {}:
            column = columns.get(name)
            if column is None or not column.get("editable") or "properties" not in ops:
                data = {"target": "column", "column": name}
                raise _Failure("NOT_EDITABLE", "column", data)

    def _check_feature(
        self,
        project: FakeProject,
        layer: dict[str, Any],
        params: dict[str, Any],
        forks: list[str],
    ) -> dict[str, Any]:
        doc_id = params["id"]
        features = project.features.get(layer["layerId"], [])
        feature = next((f for f in features if f["id"] == doc_id), None)
        if feature is None:
            data = {"deleted": doc_id in project.deleted}
            raise _Failure("FEATURE_NOT_FOUND", "no such feature", data)
        if self.role == "blocked":
            raise _Failure("NOT_PERMITTED", "role", {"reason": "role"})
        if not self._may_edit(project, doc_id):
            raise _Failure("NOT_PERMITTED", "not own", {"reason": "not-own"})
        for change in self.changes_before_edit:
            change(feature)
        self.changes_before_edit = []
        stale = params["version"] != feature["version"]
        if stale or sorted(forks) != sorted(feature.get("forks") or []):
            data = {"feature": self._served(project, feature)}
            raise _Failure("CONFLICT", "changed since fetched", data)
        return feature

    def _update(self, params: dict[str, Any]) -> dict[str, Any]:
        project, layer, params, forks = self._edit_target(params, "update")
        self._check_offered(layer, params, "update")
        feature = self._check_feature(project, layer, params, forks)
        category_id = params.get("categoryId", feature.get("categoryId"))
        columns = {c["name"]: c for c in layer.get("columns", [])}
        properties = params.get("properties") or {}
        for name in properties:
            allowed = columns[name].get("categoryIds")
            if allowed is not None and category_id not in allowed:
                data = {"target": "column", "column": name, "reason": "not-in-category"}
                raise _Failure("NOT_EDITABLE", "not in category", data)
        categories = {c["categoryId"]: c for c in project.categories}
        if "categoryId" in params and category_id not in categories:
            data = {"target": "category", "reason": "unknown-category"}
            raise _Failure("INVALID_VALUE", "unknown category", data)
        for name, value in properties.items():
            options = [o["value"] for o in columns[name].get("options") or []]
            if value is not None and options and value not in options:
                data = {"target": "column", "column": name, "reason": "not-an-option"}
                raise _Failure("INVALID_VALUE", "not an option", data)
        changed = False
        if "categoryId" in params and category_id != feature.get("categoryId"):
            feature["categoryId"] = category_id
            feature["properties"]["category"] = categories[category_id]["name"]
            changed = True
        for name, value in properties.items():
            value = value.strip() if isinstance(value, str) else value
            if feature["properties"].get(name) != value:
                feature["properties"][name] = value
                changed = True
        result: dict[str, Any] = {}
        if changed:
            feature["version"] = self._new_version()
            if self.concurrent_next:
                self.concurrent_next = False
                feature.setdefault("forks", []).append(self._new_version())
                result["concurrent"] = True
        result["feature"] = self._served(project, feature)
        return result

    def _delete(self, params: dict[str, Any]) -> dict[str, Any]:
        project, layer, params, forks = self._edit_target(params, "delete")
        self._check_offered(layer, params, "delete")
        self._check_feature(project, layer, params, forks)
        features = project.features[layer["layerId"]]
        project.features[layer["layerId"]] = [
            f for f in features if f["id"] != params["id"]
        ]
        project.deleted.add(params["id"])
        result: dict[str, Any] = {"id": params["id"]}
        if self.concurrent_next:
            self.concurrent_next = False
            result["concurrent"] = True
        return result

    def _new_version(self) -> str:
        self._versions += 1
        return f"edit-{self._versions}"

    def _after_edit(self, message: dict[str, Any], reply: Any) -> None:
        if not isinstance(reply, dict) or "result" not in reply:
            return
        params = message.get("params") or {}
        self.notify(
            "layer.changed",
            {
                "projectId": params.get("projectId"),
                "layerId": params.get("layerId"),
                "ids": [params.get("id")],
            },
        )

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
            "features": [self._served(project, f) for f in page],
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
    def __init__(self, code: str, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _error(
    request_id: Any, code: str, message: str, data: Any = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"id": request_id, "error": error}
