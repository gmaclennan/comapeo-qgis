"""Layer tree badges on CoMapeo layers that show cached, not live, data."""

from __future__ import annotations

import contextlib

from qgis.core import QgsApplication, QgsLayerTreeLayer
from qgis.gui import QgsLayerTreeView, QgsLayerTreeViewIndicator
from qgis.PyQt.QtCore import QObject, QTimer

from .controller import Controller
from .layers import comapeo_ids, comapeo_layers


class StaleIndicators(QObject):
    def __init__(self, view: QgsLayerTreeView, controller: Controller) -> None:
        super().__init__(view)
        self.view = view
        self.controller = controller
        self._indicators: dict[str, QgsLayerTreeViewIndicator] = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self.update)
        controller.changed.connect(self._timer.start)
        root = controller.project.layerTreeRoot()
        root.addedChildren.connect(self._timer.start)
        self._root = root

    def stale_layers(self) -> list[str]:
        """Ids of map layers currently showing a stale badge."""
        found = []
        for layer in comapeo_layers(self.controller.project):
            node = self._root.findLayer(layer.id())
            if node is not None and any(
                i in self._indicators.values() for i in self.view.indicators(node)
            ):
                found.append(layer.id())
        return found

    def update(self) -> None:
        wanted: dict[str, str] = {}
        for layer in comapeo_layers(self.controller.project):
            project_id = comapeo_ids(layer)[0]
            if project_id not in wanted:
                status = self.controller.project_status(project_id)
                wanted[project_id] = status.describe() if status.stale else ""
        for node in self._root.findLayers():
            layer = node.layer()
            ids = comapeo_ids(layer) if layer is not None else None
            tooltip = wanted.get(ids[0], "") if ids else ""
            for project_id, indicator in self._indicators.items():
                if project_id != (ids[0] if ids else None) or not tooltip:
                    self._remove(node, indicator)
            if tooltip and ids:
                indicator = self._indicator(ids[0])
                indicator.setToolTip(tooltip)
                if indicator not in self.view.indicators(node):
                    self.view.addIndicator(node, indicator)

    def clear(self) -> None:
        self._timer.stop()
        with contextlib.suppress(TypeError, RuntimeError):
            self.controller.changed.disconnect(self._timer.start)
        with contextlib.suppress(TypeError, RuntimeError):
            self._root.addedChildren.disconnect(self._timer.start)
        for node in self._root.findLayers():
            for indicator in self._indicators.values():
                self._remove(node, indicator)
        self._indicators.clear()

    def _indicator(self, project_id: str) -> QgsLayerTreeViewIndicator:
        indicator = self._indicators.get(project_id)
        if indicator is None:
            indicator = QgsLayerTreeViewIndicator(self)
            indicator.setIcon(QgsApplication.getThemeIcon("/mIndicatorBadLayer.svg"))
            self._indicators[project_id] = indicator
        return indicator

    def _remove(
        self, node: QgsLayerTreeLayer, indicator: QgsLayerTreeViewIndicator
    ) -> None:
        if indicator in self.view.indicators(node):
            self.view.removeIndicator(node, indicator)
