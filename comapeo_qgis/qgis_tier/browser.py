"""Browser panel entry: CoMapeo → app → project → layers.

Items are built from what the controller already knows (Fast capability, so
QGIS creates children on the main thread and never waits on the socket).
Dragging a layer hands QGIS the cache table; the controller adopts any layer
whose source is in the cache, so drag and double-click end up the same.
"""

from __future__ import annotations

import contextlib
from typing import Any

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsDataCollectionItem,
    QgsDataItem,
    QgsDataItemProvider,
    QgsErrorItem,
    QgsLayerItem,
    QgsMimeDataUtils,
)
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import QAction

from ..core.model import GEOMETRY_LINESTRING, LayerDefinition
from .connection import ServerState
from .controller import Controller

PROVIDER_NAME = "CoMapeo"
ROOT_PATH = "comapeo:"


def _fast(item: QgsDataItem, fertile: bool = False) -> None:
    flags = Qgis.BrowserItemCapabilities(Qgis.BrowserItemCapability.Fast)
    if fertile:
        flags |= Qgis.BrowserItemCapability.Fertile
    if hasattr(item, "setCapabilitiesV2"):
        item.setCapabilitiesV2(flags)
    else:
        item.setCapabilities(flags)


def _item_key(item: QgsDataItem) -> tuple[str, str]:
    return type(item).__name__, item.path()


class _Keeper:
    """Strong references to every item the provider made, until QGIS deletes it.

    QGIS owns the items, but nothing on the C++ side holds their Python half;
    without a reference here, items below the top level lose their overrides."""

    def __init__(self) -> None:
        self._items: list[QgsDataItem] = []

    def keep(self, item: QgsDataItem) -> None:
        self._items.append(item)

    def prune(self) -> None:
        self._items = [i for i in self._items if not sip.isdeleted(i)]

    def collections(self) -> list[_Collection]:
        return [
            i
            for i in self._items
            if isinstance(i, _Collection) and not sip.isdeleted(i)
        ]

    def clear(self) -> None:
        self._items.clear()


class _Collection(QgsDataCollectionItem):
    def __init__(
        self,
        parent: QgsDataItem | None,
        name: str,
        path: str,
        controller: Controller,
        keeper: _Keeper,
    ) -> None:
        super().__init__(parent, name, path, PROVIDER_NAME)
        self.controller = controller
        self.keeper = keeper
        self.owner: _Collection | None = None
        # The children QGIS holds, by key. Kept here because QgsDataItem.children()
        # under PyQt5 returns new Python-owned wrappers that delete the items.
        self.held: dict[tuple[str, str], QgsDataItem] = {}
        # Without Fertile, a refresh adopts children its temporary copy then deletes.
        _fast(self, fertile=True)

    def createChildren(self) -> list[QgsDataItem]:
        children = self.build_children()
        held = {}
        for child in children:
            sip.transferto(child, None)
            self.keeper.keep(child)
            if isinstance(child, _Collection):
                child.owner = self
            # QgsDataItem::refresh keeps an existing child equal to a new one.
            key = _item_key(child)
            old = self.held.get(key)
            held[key] = old if old is not None and not sip.isdeleted(old) else child
        self.held = held
        return children

    def build_children(self) -> list[QgsDataItem]:
        return []

    def actions(self, parent: Any) -> list[QAction]:
        refresh = QAction("Refresh", parent)
        refresh.triggered.connect(self.controller.refresh)
        return [refresh]


class RootItem(_Collection):
    def __init__(
        self, parent: QgsDataItem | None, controller: Controller, keeper: _Keeper
    ) -> None:
        super().__init__(parent, "CoMapeo", ROOT_PATH, controller, keeper)
        self.setIcon(QgsApplication.getThemeIcon("/mIconPointLayer.svg"))

    def build_children(self) -> list[QgsDataItem]:
        children: list[QgsDataItem] = []
        served = set()
        for status in self.controller.server_statuses:
            item = ServerItem(self, status.build_type, self.controller, self.keeper)
            children.append(item)
            served |= {
                p.project_id for p in self.controller.served_projects(status.build_type)
            }
        offline = [
            p for p in self.controller.cached_projects() if p.project_id not in served
        ]
        if offline:
            children.append(CachedItem(self, self.controller, self.keeper))
        if not children:
            children.append(
                QgsErrorItem(
                    self,
                    "CoMapeo Desktop is not running",
                    f"{ROOT_PATH}/none",
                )
            )
        return children


class ServerItem(_Collection):
    def __init__(
        self,
        parent: QgsDataItem,
        build_type: str,
        controller: Controller,
        keeper: _Keeper,
    ) -> None:
        status = next(
            (s for s in controller.server_statuses if s.build_type == build_type), None
        )
        name = status.app_name if status else build_type
        if status is not None and status.state is not ServerState.CONNECTED:
            name = f"{name} (not connected)"
        # The state is in the path so a refresh replaces the item when it changes.
        state = status.state.value if status else "unknown"
        path = f"{ROOT_PATH}/{build_type}/{state}"
        super().__init__(parent, name, path, controller, keeper)
        self.build_type = build_type
        self.status = status

    def build_children(self) -> list[QgsDataItem]:
        if self.status is not None and self.status.state is not ServerState.CONNECTED:
            return [QgsErrorItem(self, self.status.message, f"{self.path()}/error")]
        return [
            ProjectItem(
                self,
                p.project_id,
                p.name or p.project_id[:8],
                self.controller,
                self.keeper,
            )
            for p in self.controller.served_projects(self.build_type)
        ]


class CachedItem(_Collection):
    def __init__(
        self, parent: QgsDataItem, controller: Controller, keeper: _Keeper
    ) -> None:
        super().__init__(
            parent,
            "Cached projects (offline)",
            f"{ROOT_PATH}/cached",
            controller,
            keeper,
        )

    def build_children(self) -> list[QgsDataItem]:
        served = {
            p.project_id
            for s in self.controller.server_statuses
            for p in self.controller.served_projects(s.build_type)
        }
        return [
            ProjectItem(
                self,
                p.project_id,
                p.name or p.project_id[:8],
                self.controller,
                self.keeper,
            )
            for p in self.controller.cached_projects()
            if p.project_id not in served
        ]


class ProjectItem(_Collection):
    def __init__(
        self,
        parent: QgsDataItem,
        project_id: str,
        name: str,
        controller: Controller,
        keeper: _Keeper,
    ) -> None:
        super().__init__(
            parent, name, f"{parent.path()}/{project_id}", controller, keeper
        )
        self.project_id = project_id

    def build_children(self) -> list[QgsDataItem]:
        return [
            LayerItem(self, self.project_id, definition, self.controller)
            for definition in self.controller.available_layers(self.project_id)
        ]

    def actions(self, parent: Any) -> list[QAction]:
        add = QAction("Add all layers to the map", parent)
        add.triggered.connect(lambda: self.controller.add_project(self.project_id))
        return [add, *super().actions(parent)]


class LayerItem(QgsLayerItem):
    def __init__(
        self,
        parent: QgsDataItem,
        project_id: str,
        definition: LayerDefinition,
        controller: Controller,
    ) -> None:
        layer_type = (
            Qgis.BrowserLayerType.Line
            if definition.geometry_type == GEOMETRY_LINESTRING
            else Qgis.BrowserLayerType.Point
        )
        cache = controller.cache(project_id)
        info = cache.table(definition.layer_id) if cache.exists else None
        uri = cache.uri(info.table) if info else ""
        super().__init__(
            parent,
            definition.title,
            f"{parent.path()}/{definition.layer_id}",
            uri,
            layer_type,
            "ogr",
        )
        self.project_id = project_id
        self.layer_id = definition.layer_id
        self.controller = controller
        _fast(self)
        self.setState(Qgis.BrowserItemState.Populated)

    def handleDoubleClick(self) -> bool:
        self.controller.add_layer(self.project_id, self.layer_id)
        return True

    def mimeUris(self) -> list[Any]:
        info = self.controller.ensure_table(self.project_id, self.layer_id)
        if info is None:
            return []
        uri = QgsMimeDataUtils.Uri()
        uri.layerType = "vector"
        uri.providerKey = "ogr"
        uri.name = info.definition.title
        uri.uri = self.controller.cache(self.project_id).uri(info.table)
        return [uri]

    def hasDragEnabled(self) -> bool:
        return True


class CoMapeoItemProvider(QgsDataItemProvider):
    def __init__(self, controller: Controller) -> None:
        super().__init__()
        self.controller = controller
        self._roots: list[RootItem] = []
        self._keeper = _Keeper()
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self.refresh)
        controller.changed.connect(self._timer.start)

    def name(self) -> str:
        return PROVIDER_NAME

    def dataProviderKey(self) -> str:
        return PROVIDER_NAME

    def capabilities(self) -> Any:
        return Qgis.DataItemProviderCapabilities(
            Qgis.DataItemProviderCapability.NetworkSources
        )

    def createDataItem(self, path: str, parentItem: QgsDataItem | None) -> Any:
        if path:
            return None
        root = RootItem(parentItem, self.controller, self._keeper)
        self._roots.append(root)
        return root

    def refresh(self) -> None:
        alive = []
        for root in self._roots:
            with contextlib.suppress(RuntimeError):
                root.refresh()
                alive.append(root)
        self._roots = alive
        # QGIS refreshes only the top level of Fertile items; refresh expanded ones.
        for item in self._keeper.collections():
            if item.state() == Qgis.BrowserItemState.Populated and self._attached(item):
                item.refresh()
        self._keeper.prune()

    def _attached(self, item: _Collection) -> bool:
        """Still in a tree under one of our roots, not removed and awaiting deletion."""
        while True:
            if sip.isdeleted(item):
                return False
            owner = item.owner
            if owner is None:
                return any(r is item for r in self._roots)
            if owner.held.get(_item_key(item)) is not item:
                return False
            item = owner

    def shutdown(self) -> None:
        self._timer.stop()
        with contextlib.suppress(TypeError, RuntimeError):
            self.controller.changed.disconnect(self._timer.start)
        self._roots.clear()
        self._keeper.clear()
