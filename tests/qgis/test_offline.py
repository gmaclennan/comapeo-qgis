"""Cached layers outlive the app: saved, reopened in a fresh project, marked stale."""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest
from qgis.core import QgsProject, QgsVectorLayer

from comapeo_qgis.plugin import CoMapeoPlugin
from comapeo_qgis.qgis_tier.connection import NOT_RUNNING_MESSAGE
from tests.qgis.conftest import server_or_skip, use_discovery, wait_until
from tests.qgis.helpers import Oracle, comapeo_map_layers, plain, rows

pytestmark = pytest.mark.real_server


def snapshot(layer: QgsVectorLayer) -> dict:
    return {
        doc_id: (
            feature.geometry().asWkt() if feature.hasGeometry() else None,
            {name: plain(feature[name]) for name in layer.fields().names()},
        )
        for doc_id, feature in rows(layer).items()
    }


def test_cached_layers_reopen_with_the_app_closed_and_show_as_stale(iface):
    server = server_or_skip(observations=30, tracks=2, photos=2, projects=1)
    folder = tempfile.mkdtemp(prefix="cq-proj-")
    project_file = os.path.join(folder, "survey.qgz")
    plugin = CoMapeoPlugin(iface)
    try:
        use_discovery(server.discovery_dir)
        oracle = Oracle(server.discovery_dir)
        plugin.initGui()
        controller = plugin.controller
        wait_until(lambda: controller.available_layers(oracle.project_id))
        layers = controller.add_project(oracle.project_id)
        for layer in layers:
            layer_id = layer.customProperty("comapeo/layerId")
            expected = set(oracle.features(layer_id))
            wait_until(lambda layer=layer, e=expected: set(rows(layer)) == e)
        wait_until(lambda: not controller.project_status(oracle.project_id).busy)
        oracle.close()
        before = {
            layer.customProperty("comapeo/layerId"): snapshot(layer) for layer in layers
        }
        renderers = {
            layer.customProperty("comapeo/layerId"): layer.renderer().dump()
            for layer in layers
        }
        assert plugin.indicators.stale_layers() == []
        assert QgsProject.instance().write(project_file)

        server.stop()
        wait_until(lambda: not controller.connected, message="disconnect noticed")
        wait_until(
            lambda: (
                sorted(plugin.indicators.stale_layers())
                == sorted(layer.id() for layer in layers)
            ),
            message="stale badges while open",
        )

        plugin.unload()
        QgsProject.instance().clear()
        assert comapeo_map_layers() == []

        plugin = CoMapeoPlugin(iface)
        plugin.initGui()
        assert QgsProject.instance().read(project_file)
        reopened = comapeo_map_layers()
        assert len(reopened) == 2
        for layer in reopened:
            layer_id = layer.customProperty("comapeo/layerId")
            assert layer.isValid()
            assert layer.readOnly()
            assert snapshot(layer) == before[layer_id]
            assert layer.renderer().dump() == renderers[layer_id]
        wait_until(
            lambda: (
                sorted(plugin.indicators.stale_layers())
                == sorted(layer.id() for layer in reopened)
            ),
            message="stale badges after reopening",
        )
        status = plugin.controller.project_status(oracle.project_id)
        assert status.stale
        assert "Showing data cached" in status.describe()
        wait_until(lambda: NOT_RUNNING_MESSAGE in plugin.dock.status_label.text())
        row = plugin.dock.projects.topLevelItem(0)
        assert row is not None and "Showing data cached" in row.text(1)
    finally:
        plugin.unload()
        server.stop()
        shutil.rmtree(folder, ignore_errors=True)


def test_restarted_app_is_found_and_a_missing_project_is_reported(iface, monkeypatch):
    first = server_or_skip(observations=10, tracks=1, photos=0, projects=1)
    second = None
    plugin = CoMapeoPlugin(iface)
    try:
        use_discovery(first.discovery_dir)
        oracle = Oracle(first.discovery_dir)
        old_project = oracle.project_id
        oracle.close()
        plugin.initGui()
        controller = plugin.controller
        wait_until(lambda: controller.available_layers(old_project))
        layers = controller.add_project(old_project)
        wait_until(lambda: all(layer.featureCount() > 0 for layer in layers))
        assert plugin.indicators.stale_layers() == []

        first.stop()
        wait_until(lambda: not controller.connected)
        # The app comes back with a new socket and secret but without this project.
        second = server_or_skip(
            observations=5,
            tracks=0,
            photos=0,
            projects=1,
            discovery_dir=first.discovery_dir,
        )
        wait_until(lambda: controller.connected, timeout=30, message="reconnected")
        wait_until(lambda: controller.project_status(old_project).gone)
        assert sorted(plugin.indicators.stale_layers()) == sorted(
            layer.id() for layer in layers
        )
        names = [
            plugin.dock.projects.topLevelItem(i).text(1)
            for i in range(plugin.dock.projects.topLevelItemCount())
        ]
        assert any("No longer available" in text for text in names)

        from qgis.PyQt.QtWidgets import QMessageBox

        monkeypatch.setattr(
            QMessageBox,
            "question",
            lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
        )
        dock = plugin.dock
        for i in range(dock.projects.topLevelItemCount()):
            item = dock.projects.topLevelItem(i)
            if "No longer available" in item.text(1):
                item.setSelected(True)
        assert dock.remove_button.isEnabled()
        dock.remove_button.click()
        assert comapeo_map_layers() == []
        assert not controller.cache(old_project).exists
    finally:
        plugin.unload()
        first.stop()
        if second is not None:
            second.stop()
            shutil.rmtree(first.discovery_dir, ignore_errors=True)


def test_saved_project_reattaches_after_its_table_was_rebuilt(iface):
    server = server_or_skip(observations=10, tracks=1, photos=0, projects=1)
    folder = tempfile.mkdtemp(prefix="cq-proj-")
    project_file = os.path.join(folder, "before-change.qgz")
    plugin = CoMapeoPlugin(iface)
    try:
        use_discovery(server.discovery_dir)
        oracle = Oracle(server.discovery_dir)
        plugin.initGui()
        controller = plugin.controller
        wait_until(lambda: controller.available_layers(oracle.project_id))
        [layer] = [
            layer
            for layer in controller.add_project(oracle.project_id)
            if layer.customProperty("comapeo/layerId") == "observations"
        ]
        wait_until(lambda: set(rows(layer)) == set(oracle.features("observations")))
        saved_source = layer.source()
        assert QgsProject.instance().write(project_file)

        server.run("category")
        wait_until(lambda: layer.source() != saved_source, message="table rebuilt")
        wait_until(lambda: not controller.project_status(oracle.project_id).busy)

        QgsProject.instance().clear()
        assert QgsProject.instance().read(project_file)
        [reopened] = comapeo_map_layers("observations")
        assert reopened.isValid()
        assert reopened.source() != saved_source
        wait_until(
            lambda: set(rows(reopened)) == set(oracle.features("observations")),
            message="reopened layer has the data",
        )
        oracle.close()
    finally:
        plugin.unload()
        server.stop()
        shutil.rmtree(folder, ignore_errors=True)


def test_saved_project_reattaches_when_its_cache_is_missing(iface):
    from tests.qgis.conftest import cache_root

    server = server_or_skip(observations=10, tracks=1, photos=0, projects=1)
    folder = tempfile.mkdtemp(prefix="cq-proj-")
    project_file = os.path.join(folder, "elsewhere.qgz")
    plugin = CoMapeoPlugin(iface)
    try:
        use_discovery(server.discovery_dir)
        oracle = Oracle(server.discovery_dir)
        plugin.initGui()
        controller = plugin.controller
        wait_until(lambda: controller.available_layers(oracle.project_id))
        for layer in controller.add_project(oracle.project_id):
            layer_id = layer.customProperty("comapeo/layerId")
            expected = set(oracle.features(layer_id))
            wait_until(lambda layer=layer, e=expected: set(rows(layer)) == e)
        renderers = {
            layer.customProperty("comapeo/layerId"): layer.renderer().dump()
            for layer in comapeo_map_layers()
        }
        assert QgsProject.instance().write(project_file)
        plugin.unload()
        QgsProject.instance().clear()
        # As on another computer, or after "Remove cached data".
        shutil.rmtree(cache_root())

        plugin = CoMapeoPlugin(iface)
        plugin.initGui()
        assert QgsProject.instance().read(project_file)
        reopened = comapeo_map_layers()
        assert len(reopened) == 2
        for layer in reopened:
            layer_id = layer.customProperty("comapeo/layerId")
            expected = set(oracle.features(layer_id))
            wait_until(
                lambda layer=layer, e=expected: (
                    layer.isValid() and set(rows(layer)) == e
                ),
                message=f"{layer_id} revived",
            )
            assert layer.renderer().dump() == renderers[layer_id]
            assert layer.readOnly()
        oracle.close()
    finally:
        plugin.unload()
        server.stop()
        shutil.rmtree(folder, ignore_errors=True)
