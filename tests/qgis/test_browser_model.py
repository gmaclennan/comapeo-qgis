"""The Browser as QGIS drives it: items reached through a real QgsBrowserModel."""

from __future__ import annotations

import gc

import pytest
from qgis.core import (
    QgsBrowserModel,
    QgsCategorizedSymbolRenderer,
    QgsProject,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QModelIndex

from comapeo_qgis.plugin import CoMapeoPlugin
from tests.fake_server import FakeProject, FakeServer, observation, track
from tests.qgis.conftest import process_events, pump, use_discovery, wait_until
from tests.qgis.helpers import comapeo_map_layers, rows


@pytest.fixture
def fake():
    server = FakeServer().start()
    project = server.add_project(FakeProject("p1", "Fake project"))
    project.features["observations"] = [observation("a"), observation("b")]
    project.features["tracks"] = [track("t1")]
    yield server
    server.stop()


@pytest.fixture
def plugin(iface, fake):
    use_discovery(fake.write_discovery())
    instance = CoMapeoPlugin(iface)
    instance.initGui()
    yield instance
    instance.unload()


@pytest.fixture
def model():
    browser = QgsBrowserModel()
    browser.initialize()
    yield browser
    browser.deleteLater()
    process_events()


def child_index(model: QgsBrowserModel, parent: QModelIndex, name: str) -> QModelIndex:
    def find() -> QModelIndex | None:
        if model.canFetchMore(parent):
            model.fetchMore(parent)
        for row in range(model.rowCount(parent)):
            index = model.index(row, 0, parent)
            item = model.dataItem(index)
            if item is not None and item.name() == name:
                return index
        return None

    return wait_until(find, message=f"{name!r} in the Browser")


def project_index(model: QgsBrowserModel) -> QModelIndex:
    root = child_index(model, QModelIndex(), "CoMapeo")
    app = child_index(model, root, "CoMapeo Desktop")
    return child_index(model, app, "Fake project")


def churn(plugin: CoMapeoPlugin) -> None:
    """Refreshes as the plugin makes them, with garbage collection in between."""
    for _ in range(3):
        plugin.controller.changed.emit()
        pump(150)
        gc.collect()


def test_project_item_offers_add_all_layers(plugin, model):
    wait_until(lambda: plugin.controller.available_layers("p1"))
    project_index(model)
    churn(plugin)
    project = project_index(model)
    actions = model.dataItem(project).actions(None)
    [add] = [a for a in actions if a.text() == "Add all layers to the map"]
    add.trigger()
    wait_until(lambda: len(comapeo_map_layers()) == 2)
    observations = comapeo_map_layers("observations")[0]
    wait_until(lambda: set(rows(observations)) == {"a", "b"})


def test_layer_item_from_an_empty_cache_drags_a_valid_layer(plugin, model):
    wait_until(lambda: plugin.controller.available_layers("p1"))
    project_index(model)
    churn(plugin)
    layer_index = child_index(model, project_index(model), "Observations")
    [uri] = model.dataItem(layer_index).mimeUris()
    layer = QgsVectorLayer(uri.uri, uri.name, uri.providerKey)
    assert layer.isValid()
    QgsProject.instance().addMapLayer(layer)
    assert comapeo_map_layers() == [layer]
    assert isinstance(layer.renderer(), QgsCategorizedSymbolRenderer)
    wait_until(lambda: set(rows(layer)) == {"a", "b"})


def test_layer_item_from_an_empty_cache_double_clicks_to_the_map(plugin, model):
    wait_until(lambda: plugin.controller.available_layers("p1"))
    project_index(model)
    churn(plugin)
    layer_index = child_index(model, project_index(model), "Tracks")
    assert model.dataItem(layer_index).handleDoubleClick()
    [layer] = comapeo_map_layers()
    assert layer.isValid()
    wait_until(lambda: set(rows(layer)) == {"t1"})


def test_cached_project_is_offered_offline(iface, fake, model):
    use_discovery(fake.write_discovery())
    plugin = CoMapeoPlugin(iface)
    try:
        plugin.initGui()
        controller = plugin.controller
        wait_until(lambda: controller.available_layers("p1"))
        layer = controller.add_layer("p1", "observations")
        wait_until(lambda: set(rows(layer)) == {"a", "b"})
        wait_until(lambda: not controller.project_status("p1").busy)
        fake.stop()
        wait_until(lambda: not controller.connected)
        QgsProject.instance().clear()

        child_index(
            model,
            child_index(model, QModelIndex(), "CoMapeo"),
            "Cached projects (offline)",
        )
        churn(plugin)
        cached = child_index(
            model,
            child_index(model, QModelIndex(), "CoMapeo"),
            "Cached projects (offline)",
        )
        project = child_index(model, cached, "Fake project")
        layer_index = child_index(model, project, "Observations")
        assert model.dataItem(layer_index).handleDoubleClick()
        [offline] = comapeo_map_layers()
        assert offline.isValid()
        assert set(rows(offline)) == {"a", "b"}
    finally:
        plugin.unload()


def test_browser_survives_a_project_appearing_while_it_is_expanded(plugin, model, fake):
    wait_until(lambda: plugin.controller.available_layers("p1"))
    project_index(model)
    churn(plugin)
    fake.add_project(FakeProject("p2", "Second project"))
    plugin.controller.refresh()
    root = child_index(model, QModelIndex(), "CoMapeo")
    app = child_index(model, root, "CoMapeo Desktop")
    child_index(model, app, "Second project")
    churn(plugin)
    second = child_index(model, app, "Second project")
    layer_index = child_index(model, second, "Observations")
    churn(plugin)
    assert model.dataItem(layer_index).handleDoubleClick()
    [layer] = comapeo_map_layers()
    assert layer.isValid()


def test_layer_items_are_not_expandable(plugin, model):
    wait_until(lambda: plugin.controller.available_layers("p1"))
    project = project_index(model)
    churn(plugin)
    for name in ("Observations", "Tracks"):
        assert not model.hasChildren(child_index(model, project_index(model), name))
    assert model.hasChildren(project)
