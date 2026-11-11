"""The user's own layers stay theirs, even when they carry a CoMapeo layer's style
(Copy/Paste Style or a QML file copies its custom properties too)."""

from __future__ import annotations

import os
import shutil

import pytest
from qgis.core import (
    QgsCoordinateTransformContext,
    QgsFeature,
    QgsGeometry,
    QgsMapLayer,
    QgsPointXY,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
)
from qgis.PyQt.QtWidgets import QMessageBox
from qgis.PyQt.QtXml import QDomDocument

from comapeo_qgis.plugin import CoMapeoPlugin
from tests.fake_server import (
    FakeProject,
    FakeServer,
    observation,
    short_tempdir,
    track,
)
from tests.qgis.conftest import use_discovery, wait_until
from tests.qgis.helpers import rows

ALL = QgsMapLayer.StyleCategory.AllStyleCategories


@pytest.fixture
def fake():
    server = FakeServer().start()
    project = server.add_project(FakeProject("p1", "Fake project"))
    project.features["observations"] = [observation("a"), observation("b")]
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
def folder():
    path = short_tempdir()
    yield path
    shutil.rmtree(path, ignore_errors=True)


def point_layer(name: str) -> QgsVectorLayer:
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string", name, "memory")
    feature = QgsFeature(layer.fields())
    feature["name"] = "my village"
    feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(1, 2)))
    layer.dataProvider().addFeatures([feature])
    return layer


def geopackage_layer(folder: str) -> QgsVectorLayer:
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = "villages"
    path = os.path.join(folder, "mine.gpkg")
    QgsVectorFileWriter.writeAsVectorFormatV3(
        point_layer("tmp"), path, QgsCoordinateTransformContext(), options
    )
    layer = QgsVectorLayer(f"{path}|layername=villages", "My villages", "ogr")
    assert layer.isValid()
    return layer


def paste_style(source: QgsVectorLayer, target: QgsVectorLayer) -> None:
    doc = QDomDocument()
    source.exportNamedStyle(doc, categories=ALL)
    ok, message = target.importNamedStyle(doc, ALL)
    assert ok, message
    assert target.customProperty("comapeo/projectId") == "p1"


def change_categories(fake: FakeServer) -> None:
    wait_until(lambda: any(c.subscriptions for c in fake.connections))
    fake.projects["p1"].categories.append(
        {"categoryId": "cat-river", "name": "River", "color": "#0000ff"}
    )
    fake.notify("layer.schemaChanged", {"projectId": "p1", "layerId": "observations"})


def assert_untouched(
    layer: QgsVectorLayer, provider: str, path: str | None, read_only: bool
) -> None:
    assert layer.providerType() == provider
    if path is not None:
        assert layer.source().split("|")[0] == path
    # The pasted style itself may carry the read-only flag; it must not change.
    assert layer.readOnly() == read_only
    assert [f["name"] for f in layer.getFeatures()] == ["my village"]


def test_layers_with_a_pasted_comapeo_style_keep_their_own_data(
    plugin, fake, folder, monkeypatch
):
    controller = plugin.controller
    wait_until(lambda: controller.available_layers("p1"))
    ours = controller.add_layer("p1", "observations")
    wait_until(lambda: set(rows(ours)) == {"a", "b"})
    wait_until(lambda: not controller.project_status("p1").busy)

    scratch = point_layer("My own points")
    QgsProject.instance().addMapLayer(scratch)
    mine = geopackage_layer(folder)
    QgsProject.instance().addMapLayer(mine)
    mine_path = mine.source().split("|")[0]
    for layer in (scratch, mine):
        paste_style(ours, layer)
    read_only = {layer.name(): layer.readOnly() for layer in (scratch, mine)}

    source = ours.source()
    change_categories(fake)
    wait_until(lambda: ours.source() != source, message="CoMapeo layer rebuilt")
    assert_untouched(scratch, "memory", None, read_only["My own points"])
    assert_untouched(mine, "ogr", mine_path, read_only["My villages"])

    fake.stop()
    wait_until(lambda: plugin.indicators.stale_layers() == [ours.id()])

    project_file = os.path.join(folder, "survey.qgz")
    assert QgsProject.instance().write(project_file)
    QgsProject.instance().clear()
    assert QgsProject.instance().read(project_file)
    [reopened] = [
        layer
        for layer in QgsProject.instance().mapLayers().values()
        if layer.name() == "My villages"
    ]
    assert_untouched(reopened, "ogr", mine_path, read_only["My villages"])
    assert not controller.reset_style(reopened)
    scratch = point_layer("My own points")
    QgsProject.instance().addMapLayer(scratch)
    paste_style(reopened, scratch)

    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    dock = plugin.dock
    dock.projects.topLevelItem(0).setSelected(True)
    dock.remove_button.click()
    remaining = {layer.name() for layer in QgsProject.instance().mapLayers().values()}
    assert remaining == {"My villages", "My own points"}
    assert_untouched(reopened, "ogr", mine_path, read_only["My villages"])


def test_one_comapeo_layer_with_another_ones_style_keeps_its_own_table(
    plugin, fake, folder
):
    fake.projects["p1"].features["tracks"] = [track("t1")]
    controller = plugin.controller
    wait_until(lambda: controller.available_layers("p1"))
    observations = controller.add_layer("p1", "observations")
    tracks = controller.add_layer("p1", "tracks")
    wait_until(lambda: set(rows(observations)) == {"a", "b"})
    wait_until(lambda: set(rows(tracks)) == {"t1"})
    wait_until(lambda: not controller.project_status("p1").busy)

    # Only this category: QGIS refuses a point symbology on a line layer.
    properties = QgsMapLayer.StyleCategory.CustomProperties
    doc = QDomDocument()
    observations.exportNamedStyle(doc, categories=properties)
    assert tracks.importNamedStyle(doc, properties)[0]
    assert tracks.customProperty("comapeo/layerId") == "observations"

    sources = (observations.source(), tracks.source())
    change_categories(fake)
    wait_until(lambda: observations.source() != sources[0])
    wait_until(lambda: tracks.source() != sources[1])
    wait_until(lambda: not controller.project_status("p1").busy)
    assert observations.source() != tracks.source()
    assert set(rows(tracks)) == {"t1"}
    assert set(rows(observations)) == {"a", "b"}

    project_file = os.path.join(folder, "survey.qgz")
    assert QgsProject.instance().write(project_file)
    QgsProject.instance().clear()
    assert QgsProject.instance().read(project_file)
    by_name = {
        layer.name(): layer for layer in QgsProject.instance().mapLayers().values()
    }
    wait_until(lambda: not controller.project_status("p1").busy)
    assert set(rows(by_name["Tracks"])) == {"t1"}
    assert set(rows(by_name["Observations"])) == {"a", "b"}
