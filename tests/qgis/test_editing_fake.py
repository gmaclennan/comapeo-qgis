"""Editing against tests/fake_server.py: cases the dev server cannot produce on
demand (a change landing during the save, slow and dropped replies, flags that
change between connections, an old server)."""

from __future__ import annotations

import pytest
from qgis.core import (
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsValueMapFieldFormatter,
)
from qgis.gui import QgsAttributeForm
from qgis.PyQt.QtWidgets import QComboBox

from comapeo_qgis.plugin import CoMapeoPlugin
from tests.fake_server import FakeProject, FakeServer, observation, track
from tests.qgis.conftest import pump, record_messages, use_discovery, wait_until
from tests.qgis.helpers import plain, rows

DOCS = ["o1", "o2", "o3", "o4"]


@pytest.fixture
def fake():
    server = FakeServer().start()
    project = server.add_project(FakeProject("p1", "Fake project"))
    building = project.layers[0]["columns"][3]
    assert building["name"] == "building_type"
    building["options"].append({"value": "clinic", "label": "Clinic"})
    project.categories += [
        {
            "categoryId": "cat-river",
            "name": "River",
            "geometryTypes": ["Point", "LineString"],
        },
        {"categoryId": "cat-path", "name": "Path", "geometryTypes": ["LineString"]},
    ]
    project.features["observations"] = [
        observation(d, f"{d}-v1", -58.0 - i / 10, -3.0, notes=f"note {d}")
        for i, d in enumerate(DOCS)
    ]
    project.features["observations"][3]["categoryId"] = "cat-river"
    project.features["observations"][3]["properties"]["category"] = "River"
    project.features["tracks"] = [track("t1")]
    yield server
    server.stop()


@pytest.fixture
def plugin(iface):
    instance = CoMapeoPlugin(iface)
    yield instance
    instance.unload()


def served(fake, doc_id):
    features = fake.projects["p1"].features["observations"]
    return next((f for f in features if f["id"] == doc_id), None)


def open_layer(fake, plugin, layer_id="observations"):
    use_discovery(fake.write_discovery())
    plugin.initGui()
    controller = plugin.controller
    plugin.log = record_messages(controller)
    wait_until(lambda: controller.available_layers("p1"), message="catalog")
    layer = controller.add_layer("p1", layer_id)
    assert layer is not None
    expected = {f["id"] for f in fake.projects["p1"].features[layer_id]}
    wait_until(lambda: set(rows(layer)) == expected, message="rows")
    if layer_id == "observations":
        wait_until(lambda: not layer.readOnly(), message="editable")
    return layer


def fid(layer, doc_id):
    return next(f.id() for f in layer.getFeatures() if f["id"] == doc_id)


def change(layer, doc_id, column, value):
    index = layer.fields().indexOf(column)
    assert index >= 0, column
    assert layer.changeAttributeValue(fid(layer, doc_id), index, value)


def category_column(layer):
    """The column the user picks a category in: a value map of category ids."""
    for i in range(layer.fields().count()):
        setup = layer.editorWidgetSetup(i)
        values = [v for pair in setup.config().get("map", []) for v in pair.values()]
        if setup.type() == "ValueMap" and "cat-school" in values:
            assert layer.attributeAlias(i) == "Category"
            return layer.fields().at(i).name()
    raise AssertionError("no category column")


def assert_matches_server(layer, fake):
    shown = rows(layer)
    truth = {f["id"]: f for f in fake.projects["p1"].features["observations"]}
    assert set(shown) == set(truth)
    for doc_id, feature in truth.items():
        for name in ("notes", "building_type", "category"):
            assert plain(shown[doc_id][name]) == feature["properties"][name], (
                doc_id,
                name,
            )


def test_field_notes_choice_null_and_category_round_trip(fake, plugin, iface):
    layer = open_layer(fake, plugin)
    assert layer.startEditing()
    change(layer, "o1", "notes", "  checked on site  ")
    change(layer, "o2", "building_type", "clinic")
    change(layer, "o3", "notes", None)
    change(layer, "o4", category_column(layer), "cat-school")
    before = len(fake.requests)
    assert layer.commitChanges()

    edits = fake.edits()
    assert len(edits) == 4, [e["params"] for e in edits]
    by_id = {e["params"]["id"]: e["params"] for e in edits}
    assert by_id["o1"]["properties"] == {"notes": "  checked on site  "}
    assert by_id["o1"]["version"] == "o1-v1" and by_id["o1"]["forks"] == []
    assert by_id["o2"]["properties"] == {"building_type": "clinic"}
    assert by_id["o3"]["properties"] == {"notes": None}
    assert by_id["o4"] == {
        **{k: by_id["o4"][k] for k in ("projectId", "layerId", "id", "version")},
        "forks": [],
        "categoryId": "cat-school",
        **({"lang": by_id["o4"]["lang"]} if "lang" in by_id["o4"] else {}),
    }
    assert not any("geometry" in e["params"] for e in edits)
    # The replies carry the features, so nothing is fetched to show them.
    after = fake.requests[before:]
    assert not [r for r in after[: len(edits)] if r["method"] == "layers.getFeatures"]
    assert served(fake, "o1")["properties"]["notes"] == "checked on site"
    assert_matches_server(layer, fake)
    assert plain(rows(layer)["o4"]["category"]) == "School"
    assert not layer.isModified()
    assert "4 changes saved to CoMapeo." in iface.bar_texts()


def test_choice_through_the_value_map_in_the_form(fake, plugin, editor_widgets):
    layer = open_layer(fake, plugin)
    index = layer.fields().indexOf("building_type")
    setup = layer.editorWidgetSetup(index)
    assert setup.type() == "ValueMap"
    entries = {k: v for pair in setup.config()["map"] for k, v in pair.items()}
    assert entries["Clinic"] == "clinic"
    assert QgsValueMapFieldFormatter.NULL_VALUE in entries.values()
    assert layer.startEditing()
    feature = next(f for f in layer.getFeatures() if f["id"] == "o1")
    form = QgsAttributeForm(layer, feature)
    combo = next(
        c
        for c in form.findChildren(QComboBox)
        if c.findText("Clinic") >= 0 and c.isEnabled()
    )
    combo.setCurrentIndex(combo.findText("Clinic"))
    assert form.save()
    form.deleteLater()
    assert layer.commitChanges()
    assert served(fake, "o1")["properties"]["building_type"] == "clinic"
    assert plain(rows(layer)["o1"]["building_type"]) == "clinic"


def test_save_refused_when_changed_in_comapeo_meanwhile(fake, plugin, iface):
    layer = open_layer(fake, plugin)
    assert layer.startEditing()
    change(layer, "o1", "notes", "from QGIS")
    feature = served(fake, "o1")
    feature["version"] = "o1-v2"
    feature["properties"]["notes"] = "from CoMapeo"
    fake.notify(
        "layer.changed", {"projectId": "p1", "layerId": "observations", "ids": ["o1"]}
    )
    pump(300)
    # Held: the row stays as the user saw it.
    assert plain(rows(layer)["o1"]["notes"]) == "from QGIS"
    assert layer.commitChanges()
    assert plain(rows(layer)["o1"]["notes"]) == "from CoMapeo"
    assert feature["properties"]["notes"] == "from CoMapeo"
    assert (
        "0 saved, 1 not saved. The layer shows what CoMapeo holds." in iface.bar_texts()
    )
    wait_until(lambda: any("changed in CoMapeo since QGIS" in m for m in plugin.log))


def test_moving_a_point_is_never_sent(fake, plugin, iface):
    layer = open_layer(fake, plugin)
    before = rows(layer)["o1"].geometry().asPoint()
    assert layer.startEditing()
    assert layer.changeGeometry(
        fid(layer, "o1"), QgsGeometry.fromPointXY(QgsPointXY(1, 2))
    )
    change(layer, "o2", "notes", "kept")
    wait_until(lambda: any("cannot be moved" in m for m in plugin.log))
    assert layer.commitChanges()
    assert [e["params"]["id"] for e in fake.edits()] == ["o2"]
    assert not any("geometry" in e["params"] for e in fake.edits())
    point = rows(layer)["o1"].geometry().asPoint()
    assert (point.x(), point.y()) == (before.x(), before.y())
    wait_until(
        lambda: any("points cannot be moved from QGIS yet" in m for m in plugin.log)
    )


def test_new_features_are_discarded(fake, plugin):
    layer = open_layer(fake, plugin)
    assert layer.startEditing()
    feature = QgsFeature(layer.fields())
    feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(3, 4)))
    assert layer.addFeature(feature)
    assert layer.commitChanges()
    assert fake.edits() == []
    assert set(rows(layer)) == set(DOCS)
    wait_until(lambda: any("cannot be created from QGIS" in m for m in plugin.log))


def test_field_of_another_category_is_dropped(fake, plugin):
    layer = open_layer(fake, plugin)
    assert layer.startEditing()
    change(layer, "o4", "building_type", "school")
    wait_until(lambda: any("not a field of this observation" in m for m in plugin.log))
    change(layer, "o1", "notes", "fine")
    assert layer.commitChanges()
    assert [e["params"]["id"] for e in fake.edits()] == ["o1"]
    assert plain(rows(layer)["o4"]["building_type"]) is None


def test_field_accepted_with_a_change_to_a_category_that_has_it(fake, plugin):
    layer = open_layer(fake, plugin)
    assert layer.startEditing()
    change(layer, "o4", category_column(layer), "cat-school")
    change(layer, "o4", "building_type", "school")
    assert layer.commitChanges()
    [edit] = fake.edits()
    assert edit["params"]["categoryId"] == "cat-school"
    assert edit["params"]["properties"] == {"building_type": "school"}
    assert plain(rows(layer)["o4"]["building_type"]) == "school"


def test_not_connected_keeps_the_edits(fake, plugin, iface):
    layer = open_layer(fake, plugin)
    assert layer.startEditing()
    change(layer, "o1", "notes", "offline edit")
    assert layer.deleteFeature(fid(layer, "o2"))
    fake.stop()
    wait_until(lambda: plugin.controller.client_for("p1") is None, message="lost")
    assert not layer.commitChanges()
    wait_until(lambda: any("not connected" in t for t in iface.bar_texts()))
    assert layer.isEditable() and layer.isModified()
    assert plain(next(f for f in layer.getFeatures() if f["id"] == "o1")["notes"]) == (
        "offline edit"
    )
    assert "o2" not in rows(layer)
    assert len(list(layer.dataProvider().getFeatures())) == len(DOCS)
    assert len(layer.editBuffer().addedFeatures()) == 0


def test_tracks_cannot_be_edited(fake, plugin):
    layer = open_layer(fake, plugin, "tracks")
    pump(200)
    assert layer.readOnly()
    assert not layer.startEditing()


def test_old_server_reads_as_read_only(plugin):
    server = FakeServer(allow_edits=None).start()
    try:
        project = server.add_project(FakeProject("p1", "Old"))
        project.features["observations"] = [observation("o1")]
        use_discovery(server.write_discovery())
        plugin.initGui()
        controller = plugin.controller
        wait_until(lambda: controller.available_layers("p1"))
        layer = controller.add_layer("p1", "observations")
        wait_until(lambda: set(rows(layer)) == {"o1"})
        pump(200)
        assert layer.readOnly()
        allowed, message, _ = controller.edit_state(layer)
        assert not allowed
        assert message == (
            "This version of CoMapeo Desktop cannot be edited from QGIS. "
            "Update CoMapeo Desktop."
        )
    finally:
        server.stop()
