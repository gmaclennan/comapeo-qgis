"""Editing against tests/fake_server.py: cases the dev server cannot produce on
demand (a change landing during the save, slow and dropped replies, flags that
change between connections, an old server)."""

from __future__ import annotations

import pytest
from qgis.core import (
    Qgis,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsValueMapFieldFormatter,
)
from qgis.gui import QgsAttributeForm
from qgis.PyQt.QtWidgets import QComboBox, QMessageBox

from comapeo_qgis.plugin import CoMapeoPlugin
from comapeo_qgis.qgis_tier import editing
from comapeo_qgis.qgis_tier.qt_compat import meta_type
from tests.fake_server import FakeProject, FakeServer, observation, track
from tests.qgis.conftest import pump, record_messages, use_discovery, wait_until
from tests.qgis.helpers import grey_rows, plain, rows, table_view

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


def test_a_move_saved_before_it_is_undone_is_never_sent(fake, plugin, iface):
    layer = open_layer(fake, plugin)
    before = rows(layer)["o1"].geometry().asPoint()
    assert layer.startEditing()
    assert layer.changeGeometry(
        fid(layer, "o1"), QgsGeometry.fromPointXY(QgsPointXY(1, 2))
    )
    change(layer, "o2", "notes", "kept")
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


def test_delete_round_trip(fake, plugin, iface):
    layer = open_layer(fake, plugin)
    assert layer.startEditing()
    assert layer.deleteFeature(fid(layer, "o2"))
    assert layer.commitChanges()
    [edit] = fake.edits()
    assert edit["method"] == "features.delete"
    assert edit["params"]["version"] == "o2-v1"
    assert served(fake, "o2") is None
    assert "o2" not in rows(layer)
    assert_matches_server(layer, fake)


def test_deleted_in_comapeo_meanwhile(fake, plugin):
    layer = open_layer(fake, plugin)
    assert layer.startEditing()
    change(layer, "o1", "notes", "too late")
    project = fake.projects["p1"]
    project.features["observations"] = [
        f for f in project.features["observations"] if f["id"] != "o1"
    ]
    project.deleted.add("o1")
    assert layer.commitChanges()
    assert "o1" not in rows(layer)
    wait_until(lambda: any("it was deleted in CoMapeo" in m for m in plugin.log))


def test_member_may_change_only_its_own(fake, plugin, iface):
    fake.role = "member"
    fake.projects["p1"].others.add("o2")
    layer = open_layer(fake, plugin)
    assert layer.startEditing()
    change(layer, "o2", "notes", "not mine")
    wait_until(lambda: any("created on another device" in m for m in plugin.log))
    change(layer, "o1", "notes", "mine")
    assert layer.commitChanges()
    assert served(fake, "o1")["properties"]["notes"] == "mine"
    assert served(fake, "o2")["properties"]["notes"] == "note o2"
    assert plain(rows(layer)["o2"]["notes"]) == "note o2"
    assert "1 saved, 1 not saved. The layer shows what CoMapeo holds." in (
        iface.bar_texts()
    )
    wait_until(
        lambda: any(
            "Observation o2: created on another device; your role lets you change "
            "only observations created on this device. Not saved: Notes = 'not mine'"
            in m
            for m in plugin.log
        )
    )


def test_the_attribute_table_greys_only_other_devices_rows(fake, plugin, iface):
    fake.role = "member"
    fake.projects["p1"].others.add("o2")
    layer = open_layer(fake, plugin)
    view = table_view(layer, iface)
    try:
        assert grey_rows(view) == {"o2"}
        assert layer.startEditing()
        assert grey_rows(table_view(layer, iface)) == {"o2"}
        layer.rollBack()
        fake.role = "coordinator"
        fake.notify(
            "layer.schemaChanged", {"projectId": "p1", "layerId": "observations"}
        )
        wait_until(lambda: rows(layer)["o2"]["comapeo_editable"] is True)
        wait_until(lambda: grey_rows(view) == set(), message="grey gone")
    finally:
        view.deleteLater()


def test_a_coordinator_sees_no_grey_rows(fake, plugin, iface):
    layer = open_layer(fake, plugin)
    view = table_view(layer, iface)
    try:
        assert grey_rows(view) == set()
    finally:
        view.deleteLater()


def test_a_value_the_server_refuses_names_the_column(fake, plugin):
    layer = open_layer(fake, plugin)
    assert layer.startEditing()
    change(layer, "o1", "building_type", "barn")
    assert layer.commitChanges()
    wait_until(
        lambda: any(
            "Building type: value not accepted, not one of the field's options" in m
            for m in plugin.log
        )
    )
    assert plain(rows(layer)["o1"]["building_type"]) is None


def test_another_change_at_the_same_moment_is_reported(fake, plugin):
    layer = open_layer(fake, plugin)
    fake.concurrent_next = True
    assert layer.startEditing()
    change(layer, "o1", "notes", "racing")
    assert layer.commitChanges()
    wait_until(
        lambda: any(
            "another change was made at the same moment" in m for m in plugin.log
        )
    )
    wait_until(lambda: rows(layer)["o1"]["comapeo_forks"] == '["edit-2"]')


def test_editing_switched_off_mid_session_keeps_the_edits(fake, plugin, iface):
    layer = open_layer(fake, plugin)
    assert layer.startEditing()
    change(layer, "o1", "notes", "kept for later")
    fake.allow_edits = False
    fake.drop_connections()
    controller = plugin.controller
    wait_until(
        lambda: (
            (controller.live_definition("p1", "observations") or None) is not None
            and controller.live_definition("p1", "observations").editable is False
        ),
        message="reconnected with editing off",
    )
    wait_until(lambda: any("switched off" in m for m in plugin.log))
    assert not layer.commitChanges()
    wait_until(
        lambda: any(
            "Not saved. Editing from QGIS is switched off" in t
            for t in iface.bar_texts()
        )
    )
    assert layer.isModified()
    assert plain(next(f for f in layer.getFeatures() if f["id"] == "o1")["notes"]) == (
        "kept for later"
    )
    assert fake.edits() == []
    layer.rollBack()
    wait_until(lambda: layer.readOnly(), message="read-only once the session ends")


def test_role_taken_away_mid_session_keeps_the_edits(fake, plugin, iface):
    layer = open_layer(fake, plugin)
    assert layer.startEditing()
    change(layer, "o1", "notes", "kept")
    fake.role = "blocked"
    fake.notify("layer.schemaChanged", {"projectId": "p1", "layerId": "observations"})
    controller = plugin.controller
    wait_until(
        lambda: controller.live_definition("p1", "observations").editable is False
    )
    assert not layer.commitChanges()
    wait_until(
        lambda: any(
            "Your role in this project does not allow editing." in t
            for t in iface.bar_texts()
        )
    )
    assert layer.isModified()
    assert fake.edits() == []


def test_a_slow_save_finishes_later_and_a_second_save_waits(
    fake, plugin, iface, monkeypatch
):
    monkeypatch.setattr(editing, "SAVE_WAIT_S", 0.3)
    layer = open_layer(fake, plugin)
    fake.edit_delay = 1.0
    assert layer.startEditing()
    change(layer, "o1", "notes", "first")
    change(layer, "o2", "notes", "second")
    assert layer.commitChanges(False)
    assert any(
        "Still waiting for CoMapeo Desktop to confirm" in t for t in iface.bar_texts()
    )
    # An edit to a row whose save is still in flight is based on that save.
    change(layer, "o1", "notes", "first, again")
    change(layer, "o3", "notes", "third")
    assert not layer.commitChanges(False)
    wait_until(lambda: any("earlier change" in t for t in iface.bar_texts()))
    assert layer.isModified()
    wait_until(
        lambda: any("2 changes saved to CoMapeo." in t for t in iface.bar_texts())
    )
    fake.edit_delay = 0
    wait_until(
        lambda: len(layer.editBuffer().changedAttributeValues()) == 2,
        message="edits restored",
    )
    assert layer.commitChanges()
    wait_until(lambda: served(fake, "o3")["properties"]["notes"] == "third")
    assert served(fake, "o1")["properties"]["notes"] == "first, again"
    conflicts = [m for m in plugin.log if "changed in CoMapeo since" in m]
    assert conflicts == []
    wait_until(lambda: not plugin.controller.sync_for("p1").held("observations"))
    assert_matches_server(layer, fake)


def test_lost_replies_are_checked_once_connected_again(fake, plugin, iface):
    layer = open_layer(fake, plugin)
    fake.drop_after_edits = 1
    assert layer.startEditing()
    change(layer, "o1", "notes", "answered")
    change(layer, "o2", "notes", "applied, reply lost")
    change(layer, "o3", "notes", "never arrived")
    assert layer.commitChanges()
    wait_until(
        lambda: any(
            "The connection to CoMapeo Desktop closed before it confirmed 2 changes."
            in m
            for m in plugin.log
        )
    )
    wait_until(
        lambda: any("had not confirmed" in m for m in plugin.log), message="checked"
    )
    [checked] = [m for m in plugin.log if "had not confirmed" in m]
    assert "Of the 2 changes" in checked and "1 was saved and 1 was not" in checked
    assert "Observation o3 not saved: Notes = 'never arrived'" in checked
    assert "o2" not in checked
    assert served(fake, "o2")["properties"]["notes"] == "applied, reply lost"
    wait_until(lambda: plain(rows(layer)["o2"]["notes"]) == "applied, reply lost")


def test_a_large_save_asks_first(fake, plugin, monkeypatch):
    monkeypatch.setattr(editing, "LARGE_SAVE", 2)
    answers = []

    def ask(_parent, _title, text, *_args):
        answers.append(text)
        return answers_to_give.pop(0)

    answers_to_give = [
        QMessageBox.StandardButton.Cancel,
        QMessageBox.StandardButton.Save,
    ]
    monkeypatch.setattr(QMessageBox, "question", ask)
    layer = open_layer(fake, plugin)
    assert layer.startEditing()
    for doc_id in ("o1", "o2", "o3"):
        change(layer, doc_id, "notes", f"bulk {doc_id}")
    assert not layer.commitChanges(False)
    assert answers == [
        "Save 3 changes to CoMapeo? QGIS will wait while CoMapeo Desktop applies them."
    ]
    wait_until(lambda: len(layer.editBuffer().changedAttributeValues()) == 3)
    assert not layer.editBuffer().addedFeatures()
    assert fake.edits() == []
    assert layer.commitChanges()
    assert len(fake.edits()) == 3


def test_transaction_groups_stop_editing(fake, plugin):
    layer = open_layer(fake, plugin)
    QgsProject.instance().setTransactionMode(Qgis.TransactionMode.AutomaticGroups)
    try:
        layer.startEditing()
        wait_until(lambda: not layer.isEditable(), message="session ended")
        wait_until(lambda: any("transaction groups" in m for m in plugin.log))
    finally:
        QgsProject.instance().setTransactionMode(Qgis.TransactionMode.Disabled)


def test_columns_stay_editable_after_a_rebuild_during_a_session(fake, plugin):
    layer = open_layer(fake, plugin)
    assert layer.startEditing()
    project = fake.projects["p1"]
    project.layers[0]["schemaVersion"] = "s2"
    project.layers[0]["columns"].insert(
        3, {"name": "owner", "type": "string", "label": "Owner", "role": "field"}
    )
    fake.notify("layer.schemaChanged", {"projectId": "p1", "layerId": "observations"})
    wait_until(lambda: "owner" in layer.fields().names(), message="rebuilt")
    notes = layer.fields().indexOf("notes")
    wait_until(lambda: not layer.editFormConfig().readOnly(notes))
    change(layer, "o1", "notes", "after the rebuild")
    assert layer.commitChanges()
    assert served(fake, "o1")["properties"]["notes"] == "after the rebuild"


def test_a_column_added_in_the_session_is_discarded(fake, plugin):
    layer = open_layer(fake, plugin)
    assert layer.startEditing()
    assert layer.addAttribute(QgsField("extra", meta_type("QString")))
    change(layer, "o1", "notes", "kept")
    assert layer.commitChanges(False)
    assert [e["params"]["properties"] for e in fake.edits()] == [{"notes": "kept"}]
    wait_until(lambda: any("Column changes are not supported" in m for m in plugin.log))
    wait_until(lambda: "extra" not in layer.fields().names())
    wait_until(lambda: layer.isEditable() and not layer.isModified())
    change(layer, "o2", "notes", "and again")
    assert layer.commitChanges()
    assert served(fake, "o2")["properties"]["notes"] == "and again"
    assert "extra" not in layer.dataProvider().fields().names()


def test_connection_lost_while_the_large_save_question_is_open(
    fake, plugin, iface, monkeypatch
):
    monkeypatch.setattr(editing, "LARGE_SAVE", 2)
    layer = open_layer(fake, plugin)

    def ask(*_args):
        fake.stop()
        wait_until(lambda: plugin.controller.client_for("p1") is None, message="lost")
        return QMessageBox.StandardButton.Save

    monkeypatch.setattr(QMessageBox, "question", ask)
    assert layer.startEditing()
    for doc_id in ("o1", "o2", "o3"):
        change(layer, doc_id, "notes", f"typed {doc_id}")
    assert layer.deleteFeature(fid(layer, "o4"))
    assert not layer.commitChanges(False)
    wait_until(lambda: any("not connected" in t for t in iface.bar_texts()))
    stored = {f["id"]: plain(f["notes"]) for f in layer.dataProvider().getFeatures()}
    assert stored == {d: f"note {d}" for d in DOCS}
    assert fake.edits() == []
    wait_until(lambda: len(layer.editBuffer().changedAttributeValues()) == 3)
    assert len(layer.editBuffer().deletedFeatureIds()) == 1
    assert "o4" not in rows(layer)
    assert not layer.editBuffer().addedFeatures()


def test_unloading_with_unsaved_edits_says_they_were_discarded(fake, plugin, iface):
    layer = open_layer(fake, plugin)
    assert layer.startEditing()
    change(layer, "o1", "notes", "unsaved at unload")
    assert layer.deleteFeature(fid(layer, "o2"))
    plugin.unload()
    assert not layer.isEditable()
    assert fake.edits() == []
    stored = {f["id"]: plain(f["notes"]) for f in layer.dataProvider().getFeatures()}
    assert stored == {d: f"note {d}" for d in DOCS}
    assert any(
        "unsaved changes to 2 observations in Observations were discarded" in m
        for m in plugin.log
    ), plugin.log


def test_a_delete_that_another_write_overlapped_is_fetched_again(fake, plugin, iface):
    layer = open_layer(fake, plugin)
    fake.concurrent_next = "kept"
    fake.quiet_next = True
    assert layer.startEditing()
    assert layer.deleteFeature(fid(layer, "o2"))
    assert layer.commitChanges()
    wait_until(
        lambda: (
            "o2" in rows(layer)
            and plain(rows(layer)["o2"]["notes"]) == "written at the same moment"
        ),
        message="the observation CoMapeo kept",
    )
    assert any(
        t.startswith("0 saved, 1 deletion met another change made at the same moment")
        for t in iface.bar_texts()
    ), iface.bar_texts()
    wait_until(lambda: any("may still be there" in m for m in plugin.log))


def test_a_moved_point_goes_back_at_once(fake, plugin):
    layer = open_layer(fake, plugin)
    before = rows(layer)["o1"].geometry().asWkt()
    assert layer.startEditing()
    change(layer, "o2", "notes", "kept")
    assert layer.changeGeometry(
        fid(layer, "o1"), QgsGeometry.fromPointXY(QgsPointXY(1, 2))
    )
    wait_until(lambda: rows(layer)["o1"].geometry().asWkt() == before, message="back")
    wait_until(lambda: any("change of location was undone" in m for m in plugin.log))
    assert plain(rows(layer)["o2"]["notes"]) == "kept"
    layer.undoStack().redo()
    pump(100)
    assert rows(layer)["o1"].geometry().asWkt() == before
    layer.beginEditCommand("Move")
    layer.changeGeometry(fid(layer, "o3"), QgsGeometry.fromPointXY(QgsPointXY(5, 6)))
    layer.endEditCommand()
    wait_until(lambda: rows(layer)["o3"].geometry().asWkt() != "Point (5 6)")
    assert layer.commitChanges()
    assert [e["params"]["id"] for e in fake.edits()] == ["o2"]


def test_a_reply_that_fails_is_checked_at_once(fake, plugin, iface):
    layer = open_layer(fake, plugin)
    fake.fail_next("features.update", "INTERNAL_ERROR")
    assert layer.startEditing()
    change(layer, "o1", "notes", "never written")
    assert layer.commitChanges()
    wait_until(
        lambda: any(
            "CoMapeo Desktop did not confirm 1 change. The plugin is checking "
            "whether it was saved." in m
            for m in plugin.log
        )
    )
    wait_until(
        lambda: any(
            "Of the 1 change CoMapeo Desktop had not confirmed, 0 were saved and 1 "
            "was not."
            in m
            and "Notes = 'never written'" in m
            for m in plugin.log
        )
    )
    assert plain(rows(layer)["o1"]["notes"]) == "note o1"


def test_role_removed_while_the_large_save_question_is_open(
    fake, plugin, iface, monkeypatch
):
    monkeypatch.setattr(editing, "LARGE_SAVE", 2)
    layer = open_layer(fake, plugin)
    controller = plugin.controller

    def ask(*_args):
        fake.role = "blocked"
        fake.notify(
            "layer.schemaChanged", {"projectId": "p1", "layerId": "observations"}
        )
        wait_until(
            lambda: controller.live_definition("p1", "observations").editable is False,
            message="role removed",
        )
        return QMessageBox.StandardButton.Save

    monkeypatch.setattr(QMessageBox, "question", ask)
    assert layer.startEditing()
    for doc_id in ("o1", "o2", "o3"):
        change(layer, doc_id, "notes", f"typed {doc_id}")
    assert not layer.commitChanges(False)
    wait_until(
        lambda: any(
            "Your role in this project does not allow editing." in t
            for t in iface.bar_texts()
        )
    )
    assert fake.edits() == []
    wait_until(lambda: len(layer.editBuffer().changedAttributeValues()) == 3)
