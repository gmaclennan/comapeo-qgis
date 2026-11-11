"""A cache written by a plugin that predates editing."""

from __future__ import annotations

import json
import sqlite3

from qgis.core import QgsProject, QgsProviderRegistry, QgsVectorLayer

from comapeo_qgis.plugin import CoMapeoPlugin
from tests.fake_server import FakeProject, FakeServer, observation
from tests.qgis.conftest import pump, use_discovery, wait_until
from tests.qgis.helpers import rows


def serve():
    server = FakeServer().start()
    project = server.add_project(FakeProject("p1", "Fake project"))
    project.features["observations"] = [observation("o1"), observation("o2")]
    use_discovery(server.write_discovery())
    return server


OLD_TABLE = "observations_before"


def make_it_old(path, table):
    """As the plugin before editing wrote it: no forks or edit-flag columns, and
    a schema key without the cache format, so also another table name."""
    connection = QgsProviderRegistry.instance().providerMetadata("ogr")
    connection.createConnection(path, {}).renameVectorTable("", table, OLD_TABLE)
    table = OLD_TABLE
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        (text,) = conn.execute(
            "SELECT value FROM comapeo_meta WHERE key = 'layer:observations'"
        ).fetchone()
        info = json.loads(text)
        for role in ("forks", "editable"):
            column = info["private"].pop(role)
            conn.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        key = json.loads(info["schemaKey"])
        info["schemaKey"] = json.dumps(key[:3] + key[4:])
        info["table"] = table
        conn.execute(
            "UPDATE comapeo_meta SET value = ? WHERE key = 'layer:observations'",
            (json.dumps(info),),
        )
    finally:
        conn.close()


def test_an_old_cache_shows_offline_and_is_rebuilt_once(iface):
    server = serve()
    first = CoMapeoPlugin(iface)
    first.initGui()
    try:
        controller = first.controller
        wait_until(lambda: controller.available_layers("p1"))
        layer = controller.add_layer("p1", "observations")
        wait_until(lambda: set(rows(layer)) == {"o1", "o2"})
        source = layer.source()
        table = controller.cache("p1").table("observations").table
        path = str(controller.cache("p1").path)
        properties = {
            key: layer.customProperty(key)
            for key in ("comapeo/projectId", "comapeo/layerId")
        }
    finally:
        first.unload()
        server.stop()
    QgsProject.instance().removeAllMapLayers()
    pump(50)
    make_it_old(path, table)

    # Reopened, as from a saved project, with the app closed.
    source = source.replace(f"layername={table}", f"layername={OLD_TABLE}")
    reopened = QgsVectorLayer(source, "Observations", "ogr")
    for key, value in properties.items():
        reopened.setCustomProperty(key, value)
    QgsProject.instance().addMapLayer(reopened)
    plugin = CoMapeoPlugin(iface)
    plugin.initGui()
    try:
        wait_until(lambda: set(rows(reopened)) == {"o1", "o2"})
        assert reopened.readOnly()
        assert "comapeo_forks" not in reopened.fields().names()

        server = serve()
        try:
            wait_until(
                lambda: "comapeo_forks" in reopened.fields().names(),
                message="rebuilt",
            )
            assert OLD_TABLE not in reopened.source()
            wait_until(lambda: not reopened.readOnly(), message="editable")
            assert set(rows(reopened)) == {"o1", "o2"}
            assert rows(reopened)["o1"]["comapeo_editable"] is True
            assert OLD_TABLE not in plugin.controller.cache("p1").table_names()
        finally:
            server.stop()
    finally:
        plugin.unload()
