"""Browser panel against the dev server, driven through a real QgsBrowserModel:
CoMapeo → app → project → layers; double-click and drag add layers."""

from __future__ import annotations

import pytest
from qgis.core import (
    QgsBrowserModel,
    QgsCategorizedSymbolRenderer,
    QgsLayerItem,
    QgsProject,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QModelIndex

from comapeo_qgis.plugin import CoMapeoPlugin
from tests.qgis.conftest import (
    process_events,
    pump,
    server_or_skip,
    use_discovery,
    wait_until,
)
from tests.qgis.helpers import Oracle, comapeo_map_layers, rows
from tests.qgis.test_browser_model import child_index

pytestmark = pytest.mark.real_server


@pytest.fixture(scope="module")
def server():
    dev = server_or_skip(observations=12, tracks=2, photos=0, projects=1)
    yield dev
    dev.stop()


@pytest.fixture
def oracle(server):
    truth = Oracle(server.discovery_dir)
    yield truth
    truth.close()


@pytest.fixture
def plugin(iface, server):
    use_discovery(server.discovery_dir)
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


def browse(plugin, oracle, model) -> dict[str, QgsLayerItem]:
    wait_until(lambda: plugin.controller.available_layers(oracle.project_id))
    root = child_index(model, QModelIndex(), "CoMapeo")
    app = child_index(model, root, "CoMapeo local API dev server")
    assert model.rowCount(root) == 1
    project = child_index(model, app, oracle.project_name)
    child_index(model, project, "Tracks")
    items = {}
    for row in range(model.rowCount(project)):
        item = model.dataItem(model.index(row, 0, project))
        items[item.name()] = item
    return items


def test_tree_lists_the_project_layers(plugin, oracle, model):
    items = browse(plugin, oracle, model)
    assert sorted(items) == ["Observations", "Tracks"]


def test_double_click_adds_a_styled_layer(plugin, oracle, model):
    items = browse(plugin, oracle, model)
    assert items["Observations"].handleDoubleClick()
    [layer] = comapeo_map_layers()
    expected = set(oracle.features("observations"))
    wait_until(lambda: set(rows(layer)) == expected)
    assert isinstance(layer.renderer(), QgsCategorizedSymbolRenderer)
    assert layer.readOnly()


def test_dragged_layer_is_adopted_like_a_double_clicked_one(plugin, oracle, model):
    items = browse(plugin, oracle, model)
    [uri] = items["Tracks"].mimeUris()
    assert uri.providerKey == "ogr"
    # What QGIS does with a drop: build the layer from the URI and add it.
    layer = QgsVectorLayer(uri.uri, uri.name, uri.providerKey)
    assert layer.isValid()
    QgsProject.instance().addMapLayer(layer)
    assert comapeo_map_layers() == [layer]
    assert layer.name() == "Tracks"
    assert layer.readOnly()
    assert isinstance(layer.renderer(), QgsCategorizedSymbolRenderer)
    expected = set(oracle.features("tracks"))
    wait_until(lambda: set(rows(layer)) == expected)


def test_real_browser_model_survives_repeated_refreshes(plugin, oracle, model):
    wait_until(lambda: plugin.controller.available_layers(oracle.project_id))
    # Through the model only: QgsDataItem.children() is unsafe under PyQt5.
    items = browse(plugin, oracle, model)
    assert sorted(items) == ["Observations", "Tracks"]
    for _ in range(5):
        plugin.controller.changed.emit()
        pump(150)  # past the provider's 100 ms debounce, so each emit refreshes
    items = browse(plugin, oracle, model)
    assert sorted(items) == ["Observations", "Tracks"]
    assert items["Observations"].handleDoubleClick()
    [layer] = comapeo_map_layers()
    wait_until(lambda: set(rows(layer)) == set(oracle.features("observations")))
