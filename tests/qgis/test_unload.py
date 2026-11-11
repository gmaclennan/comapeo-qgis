"""Unloading removes everything the plugin added, and the plugin can load again."""

from __future__ import annotations

import pytest
from qgis.core import QgsApplication, QgsProject
from qgis.PyQt.QtCore import QEventLoop, QTimer
from qgis.PyQt.QtXml import QDomDocument

from comapeo_qgis.core.protocol import Notification
from comapeo_qgis.plugin import CoMapeoPlugin
from tests.qgis.conftest import server_or_skip, use_discovery, wait_until
from tests.qgis.helpers import Oracle, rows


def provider_names() -> list[str]:
    return [p.name() for p in QgsApplication.dataItemProviderRegistry().providers()]


def comapeo_docks(iface) -> list:
    return [d for d in iface.docks() if d.objectName() == "CoMapeoDock"]


def test_unload_removes_docks_actions_and_provider(iface):
    for _ in range(2):
        plugin = CoMapeoPlugin(iface)
        plugin.initGui()
        assert len(comapeo_docks(iface)) == 1
        assert "CoMapeo" in provider_names()
        assert "&CoMapeo" in iface.menus
        assert len(iface.toolbar.actions()) == 1
        assert len(iface.layer_actions) == 1

        plugin.unload()
        assert comapeo_docks(iface) == []
        assert "CoMapeo" not in provider_names()
        assert iface.menus == {}
        assert iface.toolbar.actions() == []
        assert iface.layer_actions == {}
        QgsProject.instance().readProject.emit(QDomDocument())
        QgsProject.instance().layersAdded.emit([])


def pump(milliseconds: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def add_observations(plugin, oracle):
    controller = plugin.controller
    wait_until(lambda: controller.available_layers(oracle.project_id))
    layers = controller.add_project(oracle.project_id)
    observations = next(
        layer
        for layer in layers
        if layer.customProperty("comapeo/layerId") == "observations"
    )
    expected = set(oracle.features("observations"))
    wait_until(lambda: set(rows(observations)) == expected)
    return observations


@pytest.mark.real_server
def test_unload_removes_stale_badges(iface):
    server = server_or_skip(observations=8, tracks=1, photos=0, projects=1)
    plugin = CoMapeoPlugin(iface)
    try:
        use_discovery(server.discovery_dir)
        oracle = Oracle(server.discovery_dir)
        plugin.initGui()
        observations = add_observations(plugin, oracle)
        oracle.close()
        server.stop()
        wait_until(lambda: plugin.indicators.stale_layers())
        node = QgsProject.instance().layerTreeRoot().findLayer(observations.id())
        assert iface.layerTreeView().indicators(node) != []

        plugin.unload()
        assert iface.layerTreeView().indicators(node) == []
        assert observations.isValid() and observations.featureCount() > 0
    finally:
        plugin.unload()
        server.stop()


@pytest.mark.real_server
def test_unloaded_plugin_no_longer_applies_live_changes(iface):
    server = server_or_skip(observations=8, tracks=1, photos=0, projects=1)
    plugin = CoMapeoPlugin(iface)
    oracle = None
    try:
        use_discovery(server.discovery_dir)
        oracle = Oracle(server.discovery_dir)
        plugin.initGui()
        observations = add_observations(plugin, oracle)
        before = set(rows(observations))
        plugin.unload()

        oracle.client.call("subscribe", {"projectId": oracle.project_id})
        created = server.run("create")["docId"]
        oracle.client.wait_for(
            lambda e: (
                isinstance(e, Notification) and created in e.params.get("ids", [])
            ),
            timeout=10,
        )
        # The server has now sent its notifications; give Qt time to (wrongly) act.
        pump(500)
        assert set(rows(observations)) == before
    finally:
        if oracle is not None:
            oracle.close()
        plugin.unload()
        server.stop()
