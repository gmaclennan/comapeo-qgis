"""A cache and a saved project left by the editing plugin (branch parked/editing):
the cache has two more private columns and a schema key with a cache format, and
an editable observations layer was saved with the category column hidden and a
row style that greys rows by the private `editable` column."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import zipfile
from contextlib import closing

import pytest
from qgis.core import (
    QgsConditionalStyle,
    QgsEditorWidgetSetup,
    QgsProject,
    QgsProviderRegistry,
)

from comapeo_qgis.plugin import CoMapeoPlugin
from tests.fake_server import FakeProject, FakeServer, observation, short_tempdir
from tests.qgis.conftest import cache_root, process_events, use_discovery, wait_until
from tests.qgis.helpers import comapeo_map_layers, plain, rows

IDS = ["a", "b", "c", "d"]
ROW_STYLE = "CoMapeo: created on another device"
NEWER = ("comapeo_forks", "comapeo_editable")


@pytest.fixture
def fake():
    server = FakeServer().start()
    project = server.add_project(FakeProject("p1", "Fake project"))
    project.features["observations"] = [observation(i) for i in IDS]
    yield server
    server.stop()


@pytest.fixture
def project_file():
    folder = short_tempdir()
    yield os.path.join(folder, "saved.qgz")
    shutil.rmtree(folder, ignore_errors=True)


def leave_as_the_editing_plugin_did(layer) -> None:
    fields = layer.fields()
    index = fields.indexOf("category")
    layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup("Hidden", {}))
    config = layer.attributeTableConfig()
    columns = config.columns()
    for column in columns:
        if column.name == "category":
            column.hidden = True
    config.setColumns(columns)
    layer.setAttributeTableConfig(config)
    style = QgsConditionalStyle(
        "attribute(get_feature_by_id(@layer, $id), 'comapeo_editable') IS NOT TRUE"
    )
    style.setName(ROW_STYLE)
    layer.conditionalStyles().setRowStyles([style])


def add_newer_columns(path: str) -> tuple[str, str]:
    with closing(sqlite3.connect(path)) as conn, conn:
        [(value,)] = conn.execute(
            "SELECT value FROM comapeo_meta WHERE key = 'layer:observations'"
        ).fetchall()
        info = json.loads(value)
        table = info["table"]
        # Defaults rather than an UPDATE: the GeoPackage triggers need SpatiaLite.
        add = f'ALTER TABLE "{table}" ADD COLUMN'
        conn.execute(f"{add} \"comapeo_forks\" TEXT DEFAULT '[]'")
        conn.execute(f'{add} "comapeo_editable" BOOLEAN DEFAULT 1')
    # Its table name comes from its own schema key, so differs from a rebuilt one.
    renamed = f"{table}_f2"
    connection = QgsProviderRegistry.instance().providerMetadata("ogr")
    connection.createConnection(path, {}).renameVectorTable("", table, renamed)
    info["table"] = renamed
    info["private"].update(forks="comapeo_forks", editable="comapeo_editable")
    key = json.loads(info["schemaKey"])
    info["schemaKey"] = json.dumps([*key[:3], "f:2", *key[3:]])
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "UPDATE comapeo_meta SET value = ? WHERE key = 'layer:observations'",
            (json.dumps(info),),
        )
    return table, renamed


def point_project_at(project_file: str, table: str, renamed: str) -> None:
    copy = project_file + ".tmp"
    with zipfile.ZipFile(project_file) as src, zipfile.ZipFile(copy, "w") as dst:
        for item in src.infolist():
            data = src.read(item)
            if item.filename.endswith(".qgs"):
                old, new = f"layername={table}", f"layername={renamed}"
                assert old.encode() in data
                data = data.replace(old.encode(), new.encode())
            dst.writestr(item, data)
    os.replace(copy, project_file)


def visible_columns(layer) -> set[str]:
    return {c.name for c in layer.attributeTableConfig().columns() if not c.hidden}


def values(layer) -> dict:
    names = [n for n in layer.fields().names() if n not in (*NEWER, "fid")]
    return {
        doc_id: {n: plain(f[n]) for n in names} for doc_id, f in rows(layer).items()
    }


def saved_by_the_editing_plugin(iface, fake, project_file):
    use_discovery(fake.write_discovery())
    plugin = CoMapeoPlugin(iface)
    plugin.initGui()
    controller = plugin.controller
    wait_until(lambda: controller.available_layers("p1"))
    layer = controller.add_layer("p1", "observations")
    wait_until(lambda: set(rows(layer)) == set(IDS))
    wait_until(lambda: not controller.project_status("p1").busy)
    before = values(layer)
    visible = visible_columns(layer)
    leave_as_the_editing_plugin_did(layer)
    assert QgsProject.instance().write(project_file)
    plugin.unload()
    QgsProject.instance().clear()
    process_events()
    [path] = cache_root().glob("*.gpkg")
    table, renamed = add_newer_columns(str(path))
    point_project_at(project_file, table, renamed)
    return before, visible, renamed


def assert_read_only_and_clean(layer, visible) -> None:
    assert layer.isValid()
    assert layer.readOnly()
    assert not layer.startEditing()
    assert visible_columns(layer) == visible
    assert layer.conditionalStyles().rowStyles() == []
    assert layer.editorWidgetSetup(layer.fields().indexOf("category")).type() != (
        "Hidden"
    )


def test_offline_the_old_cache_shows_without_its_newer_columns(
    iface, fake, project_file
):
    before, visible, table = saved_by_the_editing_plugin(iface, fake, project_file)
    fake.stop()
    plugin = CoMapeoPlugin(iface)
    plugin.initGui()
    try:
        assert QgsProject.instance().read(project_file)
        [layer] = comapeo_map_layers()
        assert layer.source().endswith(f"layername={table}")
        assert set(NEWER) <= set(layer.fields().names())
        assert values(layer) == before
        assert_read_only_and_clean(layer, visible)
        assert (
            plugin.controller.project_status("p1")
            .describe()
            .startswith("Not connected.")
        )
    finally:
        plugin.unload()


def test_online_the_old_cache_is_rebuilt_once(iface, fake, project_file):
    before, visible, table = saved_by_the_editing_plugin(iface, fake, project_file)
    plugin = CoMapeoPlugin(iface)
    plugin.initGui()
    try:
        assert QgsProject.instance().read(project_file)
        [layer] = comapeo_map_layers()
        wait_until(lambda: not layer.source().endswith(f"layername={table}"))
        wait_until(lambda: not plugin.controller.project_status("p1").busy)
        assert not set(NEWER) & set(layer.fields().names())
        assert values(layer) == before
        assert_read_only_and_clean(layer, visible)
        [path] = cache_root().glob("*.gpkg")
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
            names = {r[0] for r in conn.execute("SELECT table_name FROM gpkg_contents")}
        assert table not in names
    finally:
        plugin.unload()
