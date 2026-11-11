"""Fixtures for tests that run inside QGIS's Python (scripts/qgis-python.sh)."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from qgis.core import QgsApplication, QgsLayerTreeModel, QgsProject, QgsSettings
from qgis.gui import QgsLayerTreeView, QgsMapCanvas
from qgis.PyQt.QtCore import QEventLoop, Qt, QTimer
from qgis.PyQt.QtWidgets import QDockWidget, QMainWindow, QMenu, QToolBar
from qgis.testing import start_app

from tests import dev_server as dev

APP = start_app()

DISCOVERY_SETTING = "comapeo/discoveryPath"


def _under_temp(path: str) -> bool:
    temp = os.path.realpath(tempfile.gettempdir())
    return os.path.realpath(path).startswith(temp + os.sep)


@pytest.fixture(scope="session", autouse=True)
def temporary_profile() -> None:
    # Never write caches or settings into the developer's real QGIS profile.
    assert _under_temp(QgsApplication.qgisSettingsDirPath())
    assert _under_temp(QgsSettings().fileName())


@pytest.fixture(autouse=True)
def no_uncaught_exceptions(monkeypatch) -> Iterator[list[Any]]:
    """Exceptions raised in Qt slots reach sys.excepthook; any one fails the test."""
    seen: list[Any] = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: seen.append(exc))
    yield seen
    assert not seen, seen


def wait_until(
    predicate: Callable[[], Any], timeout: float = 20.0, message: str = ""
) -> Any:
    """Run the Qt event loop until `predicate()` is truthy."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting: {message or predicate}")
        loop = QEventLoop()
        QTimer.singleShot(20, loop.quit)
        loop.exec()


def pump(milliseconds: int) -> None:
    """Run the Qt event loop for a fixed time, for checks that nothing happens."""
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def process_events() -> None:
    loop = QEventLoop()
    QTimer.singleShot(0, loop.quit)
    loop.exec()


class TestIface:
    """The parts of QgisInterface the plugin uses, backed by real widgets."""

    __test__ = False

    def __init__(self) -> None:
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas(self.window)
        self.window.setCentralWidget(self.canvas)
        self.view = QgsLayerTreeView(self.window)
        self.model = QgsLayerTreeModel(QgsProject.instance().layerTreeRoot(), self.view)
        self.view.setModel(self.model)
        self.toolbar = QToolBar("Plugins", self.window)
        self.window.addToolBar(self.toolbar)
        self.plugin_menu = self.window.menuBar().addMenu("Plugins")
        self.menus: dict[str, QMenu] = {}
        self.layer_actions: dict[Any, tuple[Any, bool, list[str]]] = {}
        self.active_layer = None

    def mainWindow(self) -> QMainWindow:
        return self.window

    def mapCanvas(self) -> QgsMapCanvas:
        return self.canvas

    def layerTreeView(self) -> QgsLayerTreeView:
        return self.view

    def activeLayer(self) -> Any:
        return self.active_layer

    def addDockWidget(self, area: Any, dock: QDockWidget) -> None:
        self.window.addDockWidget(area, dock)

    def removeDockWidget(self, dock: QDockWidget) -> None:
        self.window.removeDockWidget(dock)

    def addToolBarIcon(self, action: Any) -> None:
        self.toolbar.addAction(action)

    def removeToolBarIcon(self, action: Any) -> None:
        self.toolbar.removeAction(action)

    def addPluginToMenu(self, name: str, action: Any) -> None:
        menu = self.menus.get(name)
        if menu is None:
            menu = self.menus[name] = self.plugin_menu.addMenu(name)
        menu.addAction(action)

    def removePluginMenu(self, name: str, action: Any) -> None:
        menu = self.menus.get(name)
        if menu is None:
            return
        menu.removeAction(action)
        if not menu.actions():
            self.plugin_menu.removeAction(menu.menuAction())
            del self.menus[name]

    def addCustomActionForLayerType(
        self, action: Any, menu: str, layer_type: Any, all_layers: bool
    ) -> None:
        self.layer_actions[action] = (layer_type, all_layers, [])

    def addCustomActionForLayer(self, action: Any, layer: Any) -> None:
        if action in self.layer_actions:
            self.layer_actions[action][2].append(layer.id())

    def removeCustomActionForLayerType(self, action: Any) -> bool:
        return self.layer_actions.pop(action, None) is not None

    def docks(self) -> list[QDockWidget]:
        return [
            d
            for d in self.window.findChildren(QDockWidget)
            if self.window.dockWidgetArea(d) != Qt.DockWidgetArea.NoDockWidgetArea
        ]

    def close(self) -> None:
        self.view.setModel(None)
        self.window.deleteLater()


@pytest.fixture
def iface() -> Iterator[TestIface]:
    import qgis.utils

    test_iface = TestIface()
    previous = qgis.utils.iface
    qgis.utils.iface = test_iface
    yield test_iface
    qgis.utils.iface = previous
    test_iface.close()
    process_events()


def cache_root() -> Path:
    return Path(QgsApplication.qgisSettingsDirPath()) / "comapeo" / "cache"


@pytest.fixture(autouse=True)
def clean_state() -> Iterator[None]:
    """Each test starts with an empty project and cache, and by default finds no
    app: discovery points at an empty folder, never at a real CoMapeo Desktop."""
    QgsProject.instance().clear()
    shutil.rmtree(cache_root(), ignore_errors=True)
    empty = tempfile.mkdtemp(prefix="cq-none-")
    use_discovery(empty)
    yield
    QgsProject.instance().clear()
    process_events()
    shutil.rmtree(cache_root(), ignore_errors=True)
    shutil.rmtree(empty, ignore_errors=True)
    QgsSettings().remove(DISCOVERY_SETTING)


def use_discovery(path: str) -> None:
    QgsSettings().setValue(DISCOVERY_SETTING, path)


def server_or_skip(**kwargs: Any) -> dev.DevServer:
    reason = dev.skip_reason()
    if reason:
        pytest.skip(reason)
    return dev.start(**kwargs)
