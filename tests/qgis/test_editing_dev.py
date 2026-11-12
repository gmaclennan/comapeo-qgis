"""Editing against the real dev server, as a member of a project another device
created (`--allow-edits --as-member`). Each test edits its own observations, so
the module shares one server."""

from __future__ import annotations

import math

import pytest
from qgis.core import QgsGeometry, QgsPointXY
from qgis.gui import QgsAttributeForm
from qgis.PyQt.QtWidgets import QComboBox

from comapeo_qgis.plugin import CoMapeoPlugin
from tests.qgis.conftest import (
    pump,
    record_messages,
    server_or_skip,
    use_discovery,
    wait_until,
)
from tests.qgis.helpers import (
    Oracle,
    badges,
    grey_rows,
    panel_row,
    plain,
    rows,
    table_view,
)

pytestmark = pytest.mark.real_server


@pytest.fixture(scope="module")
def server():
    # The served device creates a tenth of the observations, at least five.
    dev = server_or_skip(
        observations=80, tracks=2, photos=2, allow_edits=True, as_member=True
    )
    truth = Oracle(dev.discovery_dir)
    served = truth.features("observations")
    truth.close()
    examples = {
        f["properties"]["notes"][len("An example ") :]: i
        for i, f in served.items()
        if f["editable"] and str(f["properties"]["notes"]).startswith("An example")
    }
    others = [i for i in dev.run("list own 50")["ids"] if i not in examples.values()]
    # Chosen once, before any test changes them, so each test has its own.
    dev.picks = {"examples": examples, "own": others}
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
    instance.log = record_messages(instance.controller)
    yield instance
    instance.unload()


def open_observations(plugin, oracle):
    controller = plugin.controller
    wait_until(lambda: controller.available_layers(oracle.project_id))
    layer = controller.add_layer(oracle.project_id, "observations")
    expected = set(oracle.features("observations"))
    wait_until(lambda: set(rows(layer)) == expected, message="rows")
    wait_until(lambda: not layer.readOnly(), message="editable")
    return layer


def example(server, category):
    """The example observation of a category that this device created; the
    dev command makes one of each with every field answered."""
    return server.picks["examples"][category]


def plain_own(server, index):
    """Another observation this device created, with a location; one per test."""
    return server.picks["own"][index]


def category_id(oracle, name):
    return next(c for c, v in oracle.categories().items() if v["name"] == name)


def fid(layer, doc_id):
    return next(f.id() for f in layer.getFeatures() if f["id"] == doc_id)


def change(layer, doc_id, column, value):
    assert layer.changeAttributeValue(
        fid(layer, doc_id), layer.fields().indexOf(column), value
    )


def assert_row_matches(layer, oracle, doc_id, columns):
    truth = oracle.features("observations")[doc_id]
    row = rows(layer)[doc_id]
    assert row["comapeo_version"] == truth["version"]
    for name in columns:
        served = truth["properties"][name]
        shown = plain(row[name])
        if isinstance(served, float) or isinstance(shown, float):
            assert math.isclose(shown, served), name
        else:
            assert shown == served, name


def test_each_edit_round_trips(plugin, oracle, server, iface, editor_widgets):
    layer = open_observations(plugin, oracle)
    plain_one = plain_own(server, 0)
    plot = example(server, "Survey plot")
    spring = example(server, "Spring")
    plant = category_id(oracle, "Plant")
    assert layer.startEditing()
    change(layer, plain_one, "notes", "  checked on site  ")
    change(layer, plot, "notes", None)
    change(layer, plot, "plot_area", 3.75)
    category = next(
        layer.fields().at(i).name()
        for i in range(layer.fields().count())
        if layer.attributeAlias(i) == "Category"
        and layer.editorWidgetSetup(i).type() == "ValueMap"
    )
    change(layer, spring, category, plant)
    # A choice with numbers for values, through the form's value map.
    feature = next(f for f in layer.getFeatures() if f["id"] == plot)
    form = QgsAttributeForm(layer, feature)
    columns = oracle.layers()["observations"]["columns"]
    quality = next(c for c in columns if c["name"] == "plot_quality")
    label = next(o["label"] for o in quality["options"] if o["value"] == 3)
    combo = next(c for c in form.findChildren(QComboBox) if c.findText(label) >= 0)
    combo.setCurrentIndex(combo.findText(label))
    assert form.save()
    form.deleteLater()
    assert layer.commitChanges()
    assert "3 changes saved to CoMapeo." in iface.bar_texts()

    served = oracle.features("observations")
    assert served[plain_one]["properties"]["notes"] == "checked on site"
    assert served[plot]["properties"]["notes"] is None
    assert served[plot]["properties"]["plot_area"] == 3.75
    assert served[plot]["properties"]["plot_quality"] == 3
    assert served[spring]["categoryId"] == plant
    assert_row_matches(layer, oracle, plain_one, ["notes"])
    assert_row_matches(layer, oracle, plot, ["notes", "plot_area", "plot_quality"])
    assert_row_matches(layer, oracle, spring, ["category", "water_source"])
    assert plain(rows(layer)[spring]["category"]) == "Plant"


def test_only_offered_columns_are_open_in_the_form(plugin, oracle):
    layer = open_observations(plugin, oracle)
    config = layer.editFormConfig()

    def locked(name):
        return config.readOnly(layer.fields().indexOf(name))

    # Built-in, all tags, and the date and multiple-choice fields the server
    # would accept but the plugin does not offer yet.
    for name in ("id", "created_at", "accuracy", "tags", "visit_date", "threats"):
        assert locked(name), name
    for name in ("notes", "plot_area", "plot_quality", "water_source"):
        assert not locked(name), name
    private = [n for n in layer.fields().names() if n.startswith("comapeo_")]
    chooser = [
        n
        for n in private
        if layer.attributeAlias(layer.fields().indexOf(n)) == "Category"
    ]
    assert len(chooser) == 1 and not locked(chooser[0])
    assert all(locked(n) for n in private if n not in chooser)


def test_a_value_that_would_change_the_category_is_refused(plugin, oracle, server):
    layer = open_observations(plugin, oracle)
    well = example(server, "Well")
    before = oracle.features("observations")[well]
    assert layer.startEditing()
    change(layer, well, "water_source", "spring")
    assert layer.commitChanges()
    wait_until(
        lambda: any(
            "Water source: this value would change the observation's category" in m
            for m in plugin.log
        )
    )
    assert oracle.features("observations")[well]["version"] == before["version"]
    assert plain(rows(layer)[well]["water_source"]) == "well"


def test_moving_a_point_changes_nothing(plugin, oracle, server):
    layer = open_observations(plugin, oracle)
    community = plain_own(server, 0)
    before = oracle.features("observations")[community]
    assert layer.startEditing()
    assert layer.changeGeometry(
        fid(layer, community), QgsGeometry.fromPointXY(QgsPointXY(-55.0, -2.5))
    )
    wait_until(lambda: not layer.isModified(), message="the move undone")
    assert layer.commitChanges()
    after = oracle.features("observations")[community]
    assert after["version"] == before["version"]
    assert after["geometry"] == before["geometry"]
    point = rows(layer)[community].geometry().asPoint()
    assert [point.x(), point.y()] == before["geometry"]["coordinates"][:2]


def test_a_stale_save_is_refused_and_the_layer_shows_comapeo(plugin, oracle, server):
    layer = open_observations(plugin, oracle)
    path = plain_own(server, 1)
    assert layer.startEditing()
    change(layer, path, "notes", "edited in QGIS")
    server.run(f"update {path}")
    changed = oracle.features("observations")[path]
    assert layer.commitChanges()
    wait_until(lambda: any("changed in CoMapeo since QGIS" in m for m in plugin.log))
    truth = oracle.features("observations")[path]
    assert truth["version"] == changed["version"]
    assert truth["forks"] == []
    assert plain(rows(layer)[path]["notes"]) == truth["properties"]["notes"]
    assert rows(layer)[path]["comapeo_version"] == truth["version"]


def test_a_late_losing_fork_is_refused_then_saved(plugin, oracle, server):
    layer = open_observations(plugin, oracle)
    hunting = plain_own(server, 2)
    shown = server.run(f"fork-prepare {hunting}")["versionId"]
    wait_until(lambda: rows(layer)[hunting]["comapeo_version"] == shown)
    assert layer.startEditing()
    change(layer, hunting, "notes", "before the other version arrived")
    released = server.run("fork-release")
    assert released["versionId"] == shown and released["forks"]
    pump(300)
    assert rows(layer)[hunting]["comapeo_forks"] in (None, "[]")
    assert layer.commitChanges()
    wait_until(lambda: any("changed in CoMapeo since QGIS" in m for m in plugin.log))
    truth = oracle.features("observations")[hunting]
    assert truth["version"] == shown
    assert set(released["forks"]) <= set(truth["forks"])
    wait_until(lambda: rows(layer)[hunting]["comapeo_forks"] != "[]")
    assert layer.startEditing()
    change(layer, hunting, "notes", "seen both")
    assert layer.commitChanges()
    truth = oracle.features("observations")[hunting]
    assert truth["properties"]["notes"] == "seen both"
    assert truth["version"] != shown
    assert_row_matches(layer, oracle, hunting, ["notes"])


def test_a_member_is_refused_on_another_devices_observation(
    plugin, oracle, server, iface
):
    layer = open_observations(plugin, oracle)
    served = oracle.features("observations")
    other = sorted(i for i, f in served.items() if not f["editable"])[0]
    spring = plain_own(server, 3)
    assert layer.startEditing()
    change(layer, other, "notes", "not mine to change")
    change(layer, spring, "notes", "mine to change")
    assert layer.commitChanges()
    assert "1 saved, 1 not saved. The layer shows what CoMapeo holds." in (
        iface.bar_texts()
    )
    wait_until(lambda: any("created on another device" in m for m in plugin.log))
    truth = oracle.features("observations")
    assert truth[other]["version"] == served[other]["version"]
    assert truth[spring]["properties"]["notes"] == "mine to change"
    assert plain(rows(layer)[other]["notes"]) == served[other]["properties"]["notes"]


def test_delete(plugin, oracle, server):
    layer = open_observations(plugin, oracle)
    well = plain_own(server, 4)
    assert layer.startEditing()
    assert layer.deleteFeature(fid(layer, well))
    assert layer.commitChanges()
    assert well not in oracle.features("observations")
    assert well not in rows(layer)


def test_made_coordinator_can_change_others(plugin, oracle, server, iface):
    layer = open_observations(plugin, oracle)
    served = oracle.features("observations")
    others = {i for i, f in served.items() if not f["editable"]}
    other = sorted(others)[1]
    assert rows(layer)[other]["comapeo_editable"] is False
    view = table_view(layer, iface)
    assert grey_rows(view) == others
    server.run("role coordinator")
    try:
        wait_until(lambda: rows(layer)[other]["comapeo_editable"] is True)
        wait_until(lambda: grey_rows(view) == set(), message="no grey rows")
        view.deleteLater()
        assert layer.startEditing()
        change(layer, other, "notes", "as coordinator")
        assert layer.commitChanges()
        assert oracle.features("observations")[other]["properties"]["notes"] == (
            "as coordinator"
        )
    finally:
        server.run("role member")


def test_editing_off_leaves_the_layer_read_only_and_says_why(iface):
    dev = server_or_skip(observations=5, tracks=1, photos=0)
    off = "Editing from QGIS is switched off in CoMapeo Desktop."
    try:
        use_discovery(dev.discovery_dir)
        plugin = CoMapeoPlugin(iface)
        plugin.initGui()
        try:
            truth = Oracle(dev.discovery_dir)
            controller = plugin.controller
            wait_until(lambda: controller.available_layers(truth.project_id))
            layer = controller.add_layer(truth.project_id, "observations")
            tracks = controller.add_layer(truth.project_id, "tracks")
            wait_until(lambda: set(rows(layer)) == set(truth.features("observations")))
            truth.close()
            wait_until(
                lambda: controller.live_definition(truth.project_id, "observations")
            )
            pump(200)
            assert layer.readOnly()
            assert not layer.startEditing()
            allowed, message, _ = controller.edit_state(layer)
            assert not allowed
            assert message == off
            # Toggle Editing is greyed out on a read-only layer, so the reason
            # must show without trying to edit.
            wait_until(lambda: badges(iface, layer) == [off], message="badge")
            wait_until(lambda: off in panel_row(plugin, truth.project_id))
            assert panel_row(plugin, truth.project_id).startswith("Up to date")
            assert badges(iface, tracks) == ["Tracks cannot be edited from QGIS."]
        finally:
            plugin.unload()
    finally:
        dev.stop()
