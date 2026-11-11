"""Find running CoMapeo apps and keep one authenticated connection to each."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from ..core.discovery import (
    APP_FOLDERS,
    BUILD_OVERRIDE,
    EXTRA_PREFIX,
    DiscoveryInfo,
    DiscoveryResult,
    candidate_paths,
    find_servers,
)
from ..core.protocol import (
    UPDATE_APP,
    UPDATE_PLUGIN,
    Backoff,
    ProtocolViolation,
    VersionMismatch,
)
from .transport import LocalApiClient

POLL_INTERVAL_MS = 10_000


def not_running_message(app_name: str = "CoMapeo Desktop") -> str:
    return f"{app_name} is not running, or QGIS access is switched off in its settings."


NOT_RUNNING_MESSAGE = not_running_message()

_BUILD_LABELS = {build: folder for build, folder in APP_FOLDERS}
_BUILD_LABELS[BUILD_OVERRIDE] = "CoMapeo Desktop"


def build_label(build_type: str) -> str:
    """What to call a build before its discovery file or hello names the app."""
    if build_type.startswith(EXTRA_PREFIX):
        return f"CoMapeo Desktop in {build_type[len(EXTRA_PREFIX) :]}"
    return _BUILD_LABELS.get(build_type, "CoMapeo Desktop")


class ServerState(Enum):
    NOT_RUNNING = "not_running"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    INCOMPATIBLE = "incompatible"
    ERROR = "error"


@dataclass(frozen=True)
class ServerStatus:
    build_type: str
    state: ServerState
    message: str
    app_name: str


def describe_failure(reason: Any) -> tuple[ServerState, str]:
    if isinstance(reason, VersionMismatch):
        if reason.update == UPDATE_APP:
            return ServerState.INCOMPATIBLE, (
                "This version of CoMapeo Desktop is too old for the plugin. "
                "Update CoMapeo Desktop."
            )
        if reason.update == UPDATE_PLUGIN:
            return ServerState.INCOMPATIBLE, (
                "This CoMapeo plugin is too old for CoMapeo Desktop. "
                "Update the plugin in the QGIS Plugin Manager."
            )
        return ServerState.INCOMPATIBLE, "CoMapeo Desktop and the plugin cannot agree."
    if isinstance(reason, ProtocolViolation):
        return (
            ServerState.ERROR,
            f"CoMapeo Desktop sent an unexpected reply: {reason.message}",
        )
    return ServerState.NOT_RUNNING, NOT_RUNNING_MESSAGE


class Connector(QObject):
    """Scans discovery files and connects to every usable one, one per build type.

    Re-reads discovery before every attempt, so an app restarted with a new
    socket or secret is picked up. Failed attempts back off; when every app
    found is connected it still polls, to notice apps started later.
    """

    statusChanged = pyqtSignal()
    clientReady = pyqtSignal(str, object)
    clientLost = pyqtSignal(str)

    def __init__(
        self,
        client_version: str,
        override: Callable[[], str | None] = lambda: None,
        parent: QObject | None = None,
        extra: Callable[[], list[str]] = lambda: [],
    ) -> None:
        super().__init__(parent)
        self._client_version = client_version
        self._override = override
        self._extra = extra
        self._clients: dict[str, LocalApiClient] = {}
        self._statuses: dict[str, ServerStatus] = {}
        # Discovery that ended in a version mismatch; not retried until it changes.
        self._mismatched: dict[str, DiscoveryInfo] = {}
        self._backoff = Backoff(initial=0.5, maximum=10.0)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.scan)
        self._running = False

    @property
    def statuses(self) -> list[ServerStatus]:
        return list(self._statuses.values())

    def client(self, build_type: str) -> LocalApiClient | None:
        client = self._clients.get(build_type)
        return client if client is not None and client.is_ready else None

    def ready_clients(self) -> dict[str, LocalApiClient]:
        return {b: c for b, c in self._clients.items() if c.is_ready}

    def start(self) -> None:
        self._running = True
        self.scan()

    def stop(self) -> None:
        self._running = False
        self._timer.stop()
        for client in list(self._clients.values()):
            client.close()
        self._clients.clear()
        self._statuses.clear()
        self._mismatched.clear()

    def scan(self) -> None:
        if not self._running:
            return
        try:
            override, extra = self._override(), self._extra()
            configured = {
                c.build_type for c in candidate_paths(override=override, extra=extra)
            }
            results = find_servers(override=override, extra=extra)
        except Exception as err:  # never let a bad file stop the plugin
            results = []
            self._statuses = {
                BUILD_OVERRIDE: ServerStatus(
                    BUILD_OVERRIDE,
                    ServerState.ERROR,
                    f"Cannot read CoMapeo discovery files: {err}",
                    "CoMapeo",
                )
            }
            self.statusChanged.emit()
            self._schedule(failed=True)
            return
        for build_type in [b for b in self._clients if b not in configured]:
            self._forget(build_type)
        seen = set()
        failed = False
        for result in results:
            seen.add(result.build_type)
            failed = self._consider(result) or failed
        for build_type in list(self._statuses):
            if build_type not in seen and build_type not in self._clients:
                del self._statuses[build_type]
        self.statusChanged.emit()
        self._schedule(failed=failed or not results)

    def _forget(self, build_type: str) -> None:
        """Let go of a build whose location is no longer looked in."""
        client = self._clients.pop(build_type)
        self._statuses.pop(build_type, None)
        self._mismatched.pop(build_type, None)
        client.close()

    def _consider(self, result: DiscoveryResult) -> bool:
        """Returns True when this build type still needs another attempt."""
        build_type = result.build_type
        client = self._clients.get(build_type)
        if client is not None and not client.is_closed:
            same = (
                result.info is not None
                and client.info.socket_path == result.info.socket_path
            )
            if same:
                return not client.is_ready
            client.close()
        label = (result.info.app_name if result.info else None) or build_label(
            build_type
        )
        if result.error is not None:
            self._set(build_type, ServerState.ERROR, result.error, label)
            return True
        if not result.usable or result.info is None:
            message = not_running_message(label)
            self._set(build_type, ServerState.NOT_RUNNING, message, label)
            return True
        if self._mismatched.get(build_type) == result.info:
            return False
        self._mismatched.pop(build_type, None)
        info = result.info
        client = LocalApiClient(info, self._client_version, self)
        self._clients[build_type] = client
        client.authenticated.connect(lambda _e, b=build_type: self._on_ready(b))
        client.failed.connect(
            lambda reason, b=build_type, i=info: self._on_failed(b, i, reason)
        )
        client.closed.connect(
            lambda was, b=build_type, c=client: self._on_closed(b, c, was)
        )
        self._set(build_type, ServerState.CONNECTING, "Connecting…", label)
        client.open()
        return True

    def _set(
        self, build_type: str, state: ServerState, message: str, label: str
    ) -> None:
        self._statuses[build_type] = ServerStatus(build_type, state, message, label)

    def _label(self, build_type: str) -> str:
        current = self._statuses.get(build_type)
        return current.app_name if current else build_label(build_type)

    def _on_ready(self, build_type: str) -> None:
        client = self._clients.get(build_type)
        if client is None:
            return
        self._backoff.reset()
        name = client.app_info.get("name")
        label = name if isinstance(name, str) and name else self._label(build_type)
        self._set(build_type, ServerState.CONNECTED, f"Connected to {label}", label)
        self.statusChanged.emit()
        self.clientReady.emit(build_type, client)
        self._schedule(failed=False)

    def _on_failed(self, build_type: str, info: DiscoveryInfo, reason: Any) -> None:
        if isinstance(reason, VersionMismatch):
            self._mismatched[build_type] = info
        state, message = describe_failure(reason)
        label = self._label(build_type)
        if state is ServerState.NOT_RUNNING:
            message = not_running_message(label)
        self._set(build_type, state, message, label)
        self.statusChanged.emit()

    def _on_closed(
        self, build_type: str, client: LocalApiClient, was_authenticated: bool
    ) -> None:
        if self._clients.get(build_type) is client:
            del self._clients[build_type]
        client.deleteLater()
        current = self._statuses.get(build_type)
        if current is not None and current.state in (
            ServerState.CONNECTED,
            ServerState.CONNECTING,
        ):
            self._set(
                build_type,
                ServerState.NOT_RUNNING,
                not_running_message(current.app_name),
                current.app_name,
            )
        if was_authenticated:
            self.clientLost.emit(build_type)
        self.statusChanged.emit()
        self._schedule(failed=True)

    def _schedule(self, failed: bool) -> None:
        if not self._running:
            return
        if failed:
            delay = int(self._backoff.next_delay() * 1000)
            if self._timer.isActive() and self._timer.remainingTime() <= delay:
                return
        else:
            delay = POLL_INTERVAL_MS
        self._timer.start(delay)
