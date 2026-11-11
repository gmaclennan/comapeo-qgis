"""Read what the server serves (over a separate connection) and what QGIS shows."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from qgis.core import (
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsFeature,
    QgsProject,
    QgsRenderContext,
    QgsVariantUtils,
    QgsVectorLayer,
)
from qgis.PyQt.QtNetwork import QLocalSocket

from comapeo_qgis.core.discovery import find_servers
from tests.driver import BlockingClient


class QtBlockingClient(BlockingClient):
    """BlockingClient over QLocalSocket, which also speaks Windows named pipes."""

    def _open(self, path: str) -> QLocalSocket:
        sock = QLocalSocket()
        sock.connectToServer(path)
        if not sock.waitForConnected(5000):
            message = sock.errorString()
            sock.abort()
            raise ConnectionError(f"cannot connect to {path}: {message}")
        return sock

    def _recv(self) -> bytes | None:
        sock = self.sock
        if sock.bytesAvailable() == 0 and not sock.waitForReadyRead(50):
            connected = QLocalSocket.LocalSocketState.ConnectedState
            return b"" if sock.state() != connected else None
        return bytes(sock.readAll()) or None

    def _send(self, data: bytes) -> None:
        self.sock.write(data)
        self.sock.waitForBytesWritten(5000)

    def _shut(self) -> None:
        self.sock.abort()


class Oracle:
    """The truth, fetched over its own connection with the core's protocol client."""

    def __init__(self, discovery_dir: str) -> None:
        [found] = find_servers(override=discovery_dir)
        assert found.usable, found
        self.socket_path = found.info.socket_path
        self.client = QtBlockingClient(
            found.info.socket_path, found.info.secret, request_timeout=30
        )
        self.client.connect()
        projects = self.client.call("projects.list")["projects"]
        self.project_id = projects[0]["projectId"]
        self.project_name = projects[0]["name"]

    def close(self) -> None:
        self.client.close()

    def layers(self) -> dict[str, dict[str, Any]]:
        result = self.client.call("layers.list", {"projectId": self.project_id})
        return {layer["layerId"]: layer for layer in result["layers"]}

    def categories(self) -> dict[str, dict[str, Any]]:
        result = self.client.call("categories.list", {"projectId": self.project_id})
        return {c["categoryId"]: c for c in result["categories"]}

    def features(self, layer_id: str) -> dict[str, dict[str, Any]]:
        features: dict[str, dict[str, Any]] = {}
        cursor = None
        while True:
            params: dict[str, Any] = {"projectId": self.project_id, "layerId": layer_id}
            if cursor is not None:
                params["cursor"] = cursor
            page = self.client.call("layers.getFeatures", params)
            for feature in page["features"]:
                features[feature["id"]] = feature
            cursor = page["nextCursor"]
            if cursor is None:
                return features


def is_null(value: Any) -> bool:
    return value is None or QgsVariantUtils.isNull(value)


def plain(value: Any) -> Any:
    return None if is_null(value) else value


def rows(layer: QgsVectorLayer, key: str = "id") -> dict[str, QgsFeature]:
    return {f[key]: QgsFeature(f) for f in layer.getFeatures()}


def iso_millis(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return round(parsed.timestamp() * 1000)


def comapeo_map_layers(layer_id: str | None = None) -> list[QgsVectorLayer]:
    found = []
    for layer in QgsProject.instance().mapLayers().values():
        if not isinstance(layer, QgsVectorLayer):
            continue
        if layer.customProperty("comapeo/layerId") is None:
            continue
        if layer_id is None or layer.customProperty("comapeo/layerId") == layer_id:
            found.append(layer)
    return found


def shown_text(layer: QgsVectorLayer, name: str, value: Any) -> str:
    """The text the attribute table and identify results show for a value."""
    index = layer.fields().indexOf(name)
    setup = layer.editorWidgetSetup(index)
    formatter = QgsApplication.fieldFormatterRegistry().fieldFormatter(setup.type())
    return formatter.representValue(layer, index, setup.config(), None, value)


def symbol_for(layer: QgsVectorLayer, feature: QgsFeature) -> Any:
    renderer = layer.renderer()
    assert isinstance(renderer, QgsCategorizedSymbolRenderer)
    context = QgsRenderContext()
    renderer.startRender(context, layer.fields())
    try:
        symbol = renderer.symbolForFeature(feature, context)
        return None if symbol is None else symbol.clone()
    finally:
        renderer.stopRender(context)
