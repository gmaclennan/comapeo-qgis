"""The plugin against the real dev server: layers, values, styles, live changes."""

from __future__ import annotations

import base64
import math

import pytest
from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsFeature,
    QgsLineSymbol,
    QgsSvgMarkerSymbolLayer,
)
from qgis.PyQt.QtGui import QColor

from comapeo_qgis.plugin import CoMapeoPlugin
from comapeo_qgis.qgis_tier.connection import ServerState
from comapeo_qgis.qgis_tier.qt_compat import meta_type
from tests.qgis.conftest import server_or_skip, use_discovery, wait_until
from tests.qgis.helpers import (
    Oracle,
    is_null,
    iso_millis,
    plain,
    rows,
    shown_text,
    symbol_for,
)

pytestmark = pytest.mark.real_server

FIELD_TYPES = {
    "string": "QString",
    "number": "Double",
    "integer": "LongLong",
    "datetime": "QDateTime",
    "json": "QVariantMap",
}


@pytest.fixture(scope="module")
def server():
    dev = server_or_skip(observations=40, tracks=3, photos=4, projects=1)
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


def add_and_sync(plugin, oracle):
    controller = plugin.controller
    wait_until(
        lambda: controller.available_layers(oracle.project_id), message="catalog"
    )
    layers = {
        layer.customProperty("comapeo/layerId"): layer
        for layer in controller.add_project(oracle.project_id)
    }
    assert set(layers) == {"observations", "tracks"}
    for layer_id, layer in layers.items():
        expected = set(oracle.features(layer_id))
        wait_until(
            lambda layer=layer, expected=expected: set(rows(layer)) == expected,
            message=f"{layer_id} rows",
        )
    wait_until(lambda: not controller.project_status(oracle.project_id).busy)
    return layers


def assert_value(column, qgis_value, served):
    kind = column["type"]
    if served is None:
        assert is_null(qgis_value), (column["name"], qgis_value)
        return
    if kind == "datetime":
        assert qgis_value.toMSecsSinceEpoch() == iso_millis(served), column["name"]
    elif kind == "number":
        assert math.isclose(qgis_value, served), column["name"]
    else:
        assert plain(qgis_value) == served, (column["name"], qgis_value, served)


def labelled_values(layer, oracle, layer_id) -> int:
    """Checks that single-choice values show their option label; returns how many."""
    served = oracle.features(layer_id)
    shown = rows(layer)
    count = 0
    for column in oracle.layers()[layer_id]["columns"]:
        labels = {o["value"]: o["label"] for o in column.get("options", [])}
        if not labels or column["type"] in ("string_list", "json"):
            continue
        for doc_id, feature in served.items():
            value = feature["properties"][column["name"]]
            if value in labels:
                text = shown_text(layer, column["name"], shown[doc_id][column["name"]])
                assert text == labels[value], (column["name"], value)
                count += 1
    return count


def assert_layer_matches(layer, oracle, layer_id):
    definition = oracle.layers()[layer_id]
    served = oracle.features(layer_id)
    shown = rows(layer)
    assert set(shown) == set(served)
    assert layer.featureCount() == len(served)
    for column in definition["columns"]:
        field = layer.fields().field(column["name"])
        expected_type = FIELD_TYPES[column["type"]]
        assert field.type() == meta_type(expected_type), column["name"]
        assert (
            layer.attributeDisplayName(layer.fields().indexOf(column["name"]))
            == (column["label"])
        )
    served_names = {c["name"] for c in definition["columns"]}
    primary_key = {layer.fields().at(i).name() for i in layer.primaryKeyAttributes()}
    private = [
        n
        for n in layer.fields().names()
        if n not in served_names and n not in primary_key
    ]
    assert private
    table_columns = {c.name: c for c in layer.attributeTableConfig().columns()}
    for name in private:
        assert table_columns[name].hidden, name
        index = layer.fields().indexOf(name)
        assert layer.editorWidgetSetup(index).type() == "Hidden", name
    for name in served_names:
        assert not table_columns[name].hidden, name
    for doc_id, feature in served.items():
        row = shown[doc_id]
        for column in definition["columns"]:
            name = column["name"]
            assert_value(column, row[name], feature["properties"][name])
        geometry = feature["geometry"]
        if geometry is None:
            assert not row.hasGeometry()
        elif geometry["type"] == "Point":
            point = row.geometry().constGet()
            assert (point.x(), point.y()) == tuple(geometry["coordinates"])
        else:
            line = row.geometry().constGet()
            assert line.wkbType() == Qgis.WkbType.LineStringM
            vertices = [(line.xAt(i), line.yAt(i)) for i in range(line.numPoints())]
            assert vertices == [tuple(c) for c in geometry["coordinates"]]
            times = [line.mAt(i) for i in range(line.numPoints())]
            assert [round(t * 1000) for t in times] == [
                iso_millis(t) for t in feature["times"]
            ]


def test_connects_and_shows_status_in_the_panel(plugin, oracle):
    controller = plugin.controller
    wait_until(lambda: controller.connected, message="connected")
    [status] = controller.server_statuses
    assert status.state is ServerState.CONNECTED
    wait_until(lambda: "Connected" in plugin.dock.status_label.text())
    wait_until(lambda: plugin.dock.projects.topLevelItemCount() == 1)
    assert plugin.dock.projects.topLevelItem(0).text(0) == oracle.project_name


def test_layers_match_what_the_server_serves(plugin, oracle):
    layers = add_and_sync(plugin, oracle)
    for layer_id, layer in layers.items():
        assert layer.isValid()
        assert layer.readOnly()
        assert not layer.startEditing()
        assert_layer_matches(layer, oracle, layer_id)
    observations = layers["observations"]
    assert observations.geometryType() == Qgis.GeometryType.Point
    no_location = [
        f for f in oracle.features("observations").values() if f["geometry"] is None
    ]
    assert no_location
    shown = rows(observations)
    assert all(not shown[f["id"]].hasGeometry() for f in no_location)
    assert layers["tracks"].wkbType() == Qgis.WkbType.LineStringM
    assert labelled_values(observations, oracle, "observations") > 0


def test_layers_sit_in_a_group_named_after_the_project(plugin, oracle):
    from qgis.core import QgsProject

    add_and_sync(plugin, oracle)
    root = QgsProject.instance().layerTreeRoot()
    group = root.findGroup(oracle.project_name)
    assert group is not None
    assert sorted(n.name() for n in group.findLayers()) == ["Observations", "Tracks"]


def test_categorized_style_with_embedded_icons(plugin, oracle):
    layers = add_and_sync(plugin, oracle)
    categories = oracle.categories()
    observations = layers["observations"]
    renderer = observations.renderer()
    assert isinstance(renderer, QgsCategorizedSymbolRenderer)
    by_value = {c.value(): c for c in renderer.categories()}
    assert set(by_value) == set(categories) | {None}
    svg_served = [
        c
        for c in categories.values()
        if c.get("icon", {}).get("mimeType") == "image/svg+xml"
    ]
    assert svg_served, "the dev server should serve at least one SVG icon"
    for category_id, served in categories.items():
        entry = by_value[category_id]
        assert entry.label() == served["name"]
        icon_layers = [
            layer
            for layer in entry.symbol().symbolLayers()
            if isinstance(layer, QgsSvgMarkerSymbolLayer)
        ]
        if served.get("icon", {}).get("mimeType") == "image/svg+xml":
            [icon] = icon_layers
            assert icon.path().startswith("base64:")
            embedded = base64.b64decode(icon.path()[len("base64:") :])
            assert embedded == base64.b64decode(served["icon"]["data"])

    field = renderer.classAttribute()
    fallback = by_value[None].symbol()
    for value in (None, "no-such-category"):
        feature = QgsFeature(observations.fields())
        feature[field] = value
        symbol = symbol_for(observations, feature)
        assert symbol is not None
        assert symbol.color() == fallback.color()

    served = oracle.features("observations")
    for row in observations.getFeatures():
        category_id = served[row["id"]]["categoryId"]
        if category_id is None:
            continue
        symbol = symbol_for(observations, row)
        expected = by_value[category_id].symbol()
        assert [layer.layerType() for layer in symbol.symbolLayers()] == [
            layer.layerType() for layer in expected.symbolLayers()
        ]

    tracks = layers["tracks"]
    track_renderer = tracks.renderer()
    assert isinstance(track_renderer, QgsCategorizedSymbolRenderer)
    coloured = [
        f
        for f in oracle.features("tracks").values()
        if f["categoryId"] and categories[f["categoryId"]].get("color")
    ]
    assert coloured, "the dev server should serve a track with a coloured category"
    for row in tracks.getFeatures():
        category_id = oracle.features("tracks")[row["id"]]["categoryId"]
        symbol = symbol_for(tracks, row)
        assert isinstance(symbol, QgsLineSymbol)
        if category_id is not None and categories[category_id].get("color"):
            assert symbol.color() == QColor(categories[category_id]["color"])


def test_live_create_update_delete_without_refresh(plugin, oracle, server):
    layers = add_and_sync(plugin, oracle)
    observations = layers["observations"]

    created = server.run("create")["docId"]
    wait_until(lambda: created in rows(observations), message="created feature")
    assert_layer_matches(observations, oracle, "observations")

    before = oracle.features("observations")[created]
    server.run(f"update {created}")
    wait_until(
        lambda: (
            plain(rows(observations)[created]["tags"]) != before["properties"]["tags"]
        ),
        message="updated feature",
    )
    assert_layer_matches(observations, oracle, "observations")

    server.run(f"delete {created}")
    wait_until(lambda: created not in rows(observations), message="deleted feature")
    assert_layer_matches(observations, oracle, "observations")


def test_new_category_rebuilds_the_layer_and_keeps_existing_style(
    plugin, oracle, server
):
    layers = add_and_sync(plugin, oracle)
    observations = layers["observations"]
    renderer = observations.renderer()
    first = renderer.categories()[0]
    custom = first.symbol().clone()
    custom.setColor(QColor("#ff0000"))
    renderer.updateCategorySymbol(0, custom)
    before_columns = {c["name"] for c in oracle.layers()["observations"]["columns"]}

    preset = server.run("category")["docId"]
    wait_until(
        lambda: preset in {c.value() for c in observations.renderer().categories()},
        message="new category in renderer",
    )
    new_columns = {
        c["name"] for c in oracle.layers()["observations"]["columns"]
    } - before_columns
    assert new_columns
    wait_until(
        lambda: all(observations.fields().indexOf(n) >= 0 for n in new_columns),
        message="new columns",
    )
    wait_until(lambda: not plugin.controller.project_status(oracle.project_id).busy)
    assert observations.isValid()
    assert_layer_matches(observations, oracle, "observations")
    kept = next(
        c for c in observations.renderer().categories() if c.value() == first.value()
    )
    assert kept.symbol().color() == QColor("#ff0000")


def test_refresh_button_refetches_and_updates_last_updated(plugin, oracle):
    add_and_sync(plugin, oracle)
    controller = plugin.controller
    before = controller.project_status(oracle.project_id).last_refresh
    assert before is not None
    plugin.dock.refresh_button.click()
    wait_until(
        lambda: (
            (controller.project_status(oracle.project_id).last_refresh or before)
            > before
        ),
        message="last updated moves on",
    )
    wait_until(lambda: not controller.project_status(oracle.project_id).busy)
    assert "Up to date" in plugin.dock.projects.topLevelItem(0).text(1)
