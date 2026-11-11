"""Plugin entry point: wires the QGIS tier into the QGIS interface."""

from __future__ import annotations

import configparser
import contextlib
from pathlib import Path
from typing import Any

from qgis.core import Qgis, QgsApplication, QgsMapLayer, QgsProject
from qgis.gui import QgsLayerTreeView
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QAction, QWidget

from .qgis_tier.browser import CoMapeoItemProvider
from .qgis_tier.controller import Controller
from .qgis_tier.dock import CoMapeoDock
from .qgis_tier.indicators import StaleIndicators
from .qgis_tier.layers import comapeo_ids

MENU = "&CoMapeo"


class CoMapeoPlugin:
    def __init__(self, iface: Any) -> None:
        self.iface = iface
        self.controller: Controller | None = None
        self.dock: CoMapeoDock | None = None
        self.provider: CoMapeoItemProvider | None = None
        self.indicators: StaleIndicators | None = None
        self._menu_actions: list[QAction] = []
        self._toolbar_actions: list[QAction] = []
        self._owned_actions: list[QAction] = []
        self._layer_action: QAction | None = None
        self._project_signal: tuple[Any, Any] | None = None

    def initGui(self) -> None:
        window = self.iface.mainWindow()
        parent = window if isinstance(window, QWidget) else None
        self.controller = Controller(client_version=plugin_version())

        self.dock = CoMapeoDock(self.controller, parent)
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)

        panel = self.dock.toggleViewAction()
        panel.setText("CoMapeo panel")
        refresh = QAction("Refresh CoMapeo data", parent)
        refresh.triggered.connect(self.controller.refresh)
        self._owned_actions.append(refresh)
        for action in (panel, refresh):
            self.iface.addPluginToMenu(MENU, action)
            self._menu_actions.append(action)
        self.iface.addToolBarIcon(panel)
        self._toolbar_actions.append(panel)

        self._layer_action = QAction("Reset to CoMapeo style", parent)
        self._layer_action.triggered.connect(self._reset_active_layer_style)
        self.iface.addCustomActionForLayerType(
            self._layer_action, "", Qgis.LayerType.Vector, False
        )

        self.provider = CoMapeoItemProvider(self.controller)
        QgsApplication.dataItemProviderRegistry().addProvider(self.provider)

        view = self.iface.layerTreeView()
        if isinstance(view, QgsLayerTreeView):
            self.indicators = StaleIndicators(view, self.controller)

        self.controller.start()
        project = QgsProject.instance()
        project.layersAdded.connect(self._on_layers_added)
        self._project_signal = (project.layersAdded, self._on_layers_added)
        self._on_layers_added(list(project.mapLayers().values()))

    def unload(self) -> None:
        if self.controller is None:
            return
        if self._project_signal is not None:
            signal, slot = self._project_signal
            with contextlib.suppress(TypeError, RuntimeError):
                signal.disconnect(slot)
            self._project_signal = None
        if self.indicators is not None:
            self.indicators.clear()
            self.indicators.deleteLater()
            self.indicators = None
        if self.dock is not None:
            self.dock.media_area.shutdown()
        self.controller.stop()
        if self.provider is not None:
            self.provider.shutdown()
            QgsApplication.dataItemProviderRegistry().removeProvider(self.provider)
            self.provider = None
        if self._layer_action is not None:
            self.iface.removeCustomActionForLayerType(self._layer_action)
            self._layer_action.deleteLater()
            self._layer_action = None
        for action in self._toolbar_actions:
            self.iface.removeToolBarIcon(action)
        for action in self._menu_actions:
            self.iface.removePluginMenu(MENU, action)
        for action in self._owned_actions:
            action.deleteLater()
        self._toolbar_actions.clear()
        self._menu_actions.clear()
        self._owned_actions.clear()
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.setParent(None)
            self.dock.deleteLater()
            self.dock = None
        self.controller.deleteLater()
        self.controller = None

    def _on_layers_added(self, layers: list[QgsMapLayer]) -> None:
        if self._layer_action is None:
            return
        for layer in layers:
            if comapeo_ids(layer) is not None:
                self.iface.addCustomActionForLayer(self._layer_action, layer)

    def _reset_active_layer_style(self) -> None:
        layer = self.iface.activeLayer()
        if layer is not None and self.controller is not None:
            self.controller.reset_style(layer)


def plugin_version() -> str:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(Path(__file__).with_name("metadata.txt"), encoding="utf-8")
    return parser.get("general", "version", fallback="0.0.0")
