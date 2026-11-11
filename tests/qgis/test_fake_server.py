"""Cases the dev server's data does not produce, served by tests/fake_server.py."""

from __future__ import annotations

import threading

import pytest
from qgis.core import QgsProject

from comapeo_qgis.plugin import CoMapeoPlugin
from comapeo_qgis.qgis_tier.connection import ServerState
from comapeo_qgis.qgis_tier.qt_compat import meta_type
from tests.fake_server import FakeProject, FakeServer
from tests.qgis.conftest import use_discovery, wait_until
from tests.qgis.helpers import iso_millis, plain, rows

LAYER = {
    "layerId": "things",
    "title": "Things",
    "docType": "observation",
    "geometryType": "Point",
    "schemaVersion": "s1",
    "columns": [
        {"name": "id", "type": "string", "label": "ID", "role": "builtin"},
        {"name": "fid", "type": "string", "label": "Fid tag", "role": "field"},
        {"name": "geom", "type": "integer", "label": "Geom tag", "role": "field"},
        {
            "name": "comapeo_version",
            "type": "string",
            "label": "Version tag",
            "role": "field",
        },
        {"name": "flag", "type": "boolean", "label": "Flag", "role": "field"},
        {"name": "choices", "type": "string_list", "label": "Choices", "role": "field"},
        {"name": "when_seen", "type": "datetime", "label": "Seen", "role": "field"},
        {"name": "size", "type": "number", "label": "Size", "role": "field"},
        {"name": "odd", "type": "something_new", "label": "Odd", "role": "field"},
        {"name": "tags", "type": "json", "label": "All tags", "role": "tags"},
    ],
}


def thing(doc_id: str, **values):
    properties = {column["name"]: None for column in LAYER["columns"]}
    properties.update(id=doc_id, **values)
    return {
        "id": doc_id,
        "version": "v1",
        "geometry": {"type": "Point", "coordinates": [10.5, -3.25]},
        "properties": properties,
        "categoryId": None,
        "attachments": [],
    }


@pytest.fixture
def fake():
    server = FakeServer().start()
    yield server
    server.stop()


@pytest.fixture
def plugin(iface):
    instance = CoMapeoPlugin(iface)
    yield instance
    instance.unload()


def test_every_column_type_and_tag_keys_named_like_private_columns(fake, plugin):
    project = fake.add_project(FakeProject("p1", "Fake project", layers=[LAYER]))
    project.features["things"] = [
        thing(
            "a",
            fid="tag value",
            geom=7,
            comapeo_version="tag v",
            flag=True,
            choices=["x", "y"],
            when_seen="2026-03-07T10:00:00+02:00",
            size=1.25,
            odd="kept as text",
            tags={"fid": "tag value", "nested": {"k": [1, 2]}},
        ),
        thing("b", flag=False, choices=[]),
    ]
    use_discovery(fake.write_discovery())
    plugin.initGui()
    controller = plugin.controller
    wait_until(lambda: controller.available_layers("p1"))
    layer = controller.add_layer("p1", "things")
    assert layer is not None
    wait_until(lambda: set(rows(layer)) == {"a", "b"})

    fields = layer.fields()
    assert fields.field("flag").type() == meta_type("Bool")
    assert fields.field("geom").type() == meta_type("LongLong")
    assert fields.field("fid").type() == meta_type("QString")
    assert fields.field("odd").type() == meta_type("QString")
    # GeoPackage stores lists as JSON; QGIS 4.x reads them back as a string list.
    assert fields.field("choices").type() == meta_type("QStringList")
    a, b = rows(layer)["a"], rows(layer)["b"]
    assert a["fid"] == "tag value"
    assert a["geom"] == 7
    assert a["comapeo_version"] == "tag v"
    assert a["flag"] is True and b["flag"] is False
    assert list(a["choices"]) == ["x", "y"]
    assert plain(b["choices"]) in (None, [])
    assert a["when_seen"].toMSecsSinceEpoch() == iso_millis("2026-03-07T08:00:00Z")
    assert a["size"] == 1.25
    assert a["odd"] == "kept as text"
    assert a["tags"] == {"fid": "tag value", "nested": {"k": [1, 2]}}
    point = a.geometry().asPoint()
    assert (point.x(), point.y()) == (10.5, -3.25)


def test_too_new_app_asks_for_a_plugin_update(plugin):
    server = FakeServer(protocol_versions=(2,)).start()
    try:
        use_discovery(server.write_discovery())
        plugin.initGui()
        wait_until(lambda: "Update the plugin" in plugin.dock.status_label.text())
        [status] = plugin.controller.server_statuses
        assert status.state is ServerState.INCOMPATIBLE
        assert QgsProject.instance().mapLayers() == {}
    finally:
        server.stop()


def test_feature_that_loses_its_location_keeps_its_row_without_geometry(fake, plugin):
    project = fake.add_project(FakeProject("p1", "Fake project", layers=[LAYER]))
    project.features["things"] = [thing("a", size=1.0), thing("b", size=2.0)]
    use_discovery(fake.write_discovery())
    plugin.initGui()
    controller = plugin.controller
    wait_until(lambda: controller.available_layers("p1"))
    layer = controller.add_layer("p1", "things")
    wait_until(lambda: set(rows(layer)) == {"a", "b"})
    fids = {doc_id: f.id() for doc_id, f in rows(layer).items()}
    wait_until(lambda: any(c.subscriptions for c in fake.connections))

    moved = thing("a", size=3.0)
    moved["version"] = "v2"
    moved["geometry"] = None
    project.features["things"] = [moved]
    fake.notify(
        "layer.changed", {"projectId": "p1", "layerId": "things", "ids": ["a", "b"]}
    )
    wait_until(lambda: set(rows(layer)) == {"a"}, message="b removed, a kept")
    row = rows(layer)["a"]
    assert not row.hasGeometry()
    assert row["size"] == 3.0
    assert row.id() == fids["a"]


def test_reconnect_refreshes_and_clears_the_stale_badge(plugin):
    import shutil

    from tests.fake_server import new_endpoint, short_tempdir

    folder = short_tempdir()
    socket_path = new_endpoint(folder)
    first = FakeServer(socket_path=socket_path).start()
    second = None
    try:
        project = first.add_project(FakeProject("p1", "Fake project", layers=[LAYER]))
        project.features["things"] = [thing("a")]
        use_discovery(first.write_discovery())
        plugin.initGui()
        controller = plugin.controller
        wait_until(lambda: controller.available_layers("p1"))
        layer = controller.add_layer("p1", "things")
        wait_until(lambda: set(rows(layer)) == {"a"})
        assert plugin.indicators.stale_layers() == []

        first.stop()
        wait_until(lambda: plugin.indicators.stale_layers() == [layer.id()])

        # The app comes back; meanwhile "b" was added and "a" deleted.
        second = FakeServer(socket_path=socket_path, secret=first.secret)
        second.add_project(project)
        project.features["things"] = [thing("b")]
        second.start()
        wait_until(lambda: set(rows(layer)) == {"b"}, timeout=30, message="refetched")
        wait_until(lambda: plugin.indicators.stale_layers() == [])
    finally:
        first.stop()
        if second is not None:
            second.stop()
        shutil.rmtree(folder, ignore_errors=True)


def test_unload_closes_the_connection(fake, plugin):
    fake.add_project(FakeProject("p1", "Fake project", layers=[LAYER]))
    use_discovery(fake.write_discovery())
    plugin.initGui()
    wait_until(lambda: plugin.controller.connected)
    assert fake.open_connections == 1
    plugin.unload()
    # No Qt event loop here, so a close left to deleteLater() would never happen.
    closed = threading.Event()
    for _ in range(200):
        if fake.open_connections == 0:
            closed.set()
            break
        closed.wait(0.01)
    assert closed.is_set(), "unload left the connection open"
