"""Table rebuilds (category or schema changes) keep the user's filter and the last
good data, including when the refetch fails."""

from __future__ import annotations

import os
import shutil

import pytest
from qgis.core import QgsProject

from comapeo_qgis.plugin import CoMapeoPlugin
from tests.fake_server import FakeProject, FakeServer, observation, short_tempdir
from tests.qgis.conftest import use_discovery, wait_until
from tests.qgis.helpers import comapeo_map_layers, rows

FILTER = "\"id\" IN ('a', 'b')"
IDS = ["a", "b", "c", "d", "e", "f", "g"]


@pytest.fixture
def fake():
    server = FakeServer().start()
    project = server.add_project(FakeProject("p1", "Fake project"))
    project.features["observations"] = [observation(i) for i in IDS]
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
def project_file():
    folder = short_tempdir()
    yield os.path.join(folder, "saved.qgz")
    shutil.rmtree(folder, ignore_errors=True)


def synced_layer(plugin):
    controller = plugin.controller
    wait_until(lambda: controller.available_layers("p1"))
    layer = controller.add_layer("p1", "observations")
    assert layer is not None
    wait_until(lambda: set(rows(layer)) == set(IDS))
    wait_until(lambda: not controller.project_status("p1").busy)
    return layer


def change_categories(fake: FakeServer) -> None:
    """What the app does when someone adds a category: a schema change."""
    wait_until(lambda: any(c.subscriptions for c in fake.connections))
    fake.projects["p1"].categories.append(
        {"categoryId": "cat-river", "name": "River", "color": "#0000ff"}
    )
    fake.notify("layer.schemaChanged", {"projectId": "p1", "layerId": "observations"})


def renderer_values(layer) -> set:
    return {c.value() for c in layer.renderer().categories()}


def test_filter_survives_saving_and_reopening(plugin, project_file):
    layer = synced_layer(plugin)
    assert layer.setSubsetString(FILTER)
    assert set(rows(layer)) == {"a", "b"}
    assert QgsProject.instance().write(project_file)

    QgsProject.instance().clear()
    assert QgsProject.instance().read(project_file)
    [reopened] = comapeo_map_layers()
    wait_until(lambda: plugin.controller.project_status("p1").connected)
    wait_until(lambda: not plugin.controller.project_status("p1").busy)
    assert reopened.subsetString() == FILTER
    assert set(rows(reopened)) == {"a", "b"}


def test_filter_survives_a_live_rebuild(plugin, fake):
    layer = synced_layer(plugin)
    assert layer.setSubsetString(FILTER)
    source = layer.source()
    change_categories(fake)
    wait_until(lambda: layer.source() != source, message="switched to new table")
    assert layer.subsetString() == FILTER
    assert set(rows(layer)) == {"a", "b"}
    assert "cat-river" in renderer_values(layer)


def test_filter_survives_reopening_a_project_whose_table_was_rebuilt(
    plugin, fake, project_file
):
    layer = synced_layer(plugin)
    assert layer.setSubsetString(FILTER)
    assert QgsProject.instance().write(project_file)
    QgsProject.instance().clear()

    # Another QGIS project shows the same data while the category changes.
    other = plugin.controller.add_layer("p1", "observations")
    source = other.source()
    change_categories(fake)
    wait_until(lambda: other.source() != source, message="table rebuilt")
    QgsProject.instance().clear()

    assert QgsProject.instance().read(project_file)
    [reopened] = comapeo_map_layers()
    wait_until(lambda: reopened.isValid())
    assert reopened.subsetString() == FILTER
    assert set(rows(reopened)) == {"a", "b"}


def test_failed_refetch_after_a_rebuild_keeps_the_cached_rows(plugin, fake):
    layer = synced_layer(plugin)
    controller = plugin.controller
    before = controller.project_status("p1").last_refresh
    assert before is not None
    source = layer.source()

    for layer_id in ("observations", "tracks"):
        fake.fail_next("layers.getFeatures", "INTERNAL_ERROR", layerId=layer_id)
    change_categories(fake)
    wait_until(lambda: controller.project_status("p1").error, message="error shown")
    wait_until(lambda: not fake.failures, message="both refetches failed")
    assert layer.source() == source
    assert set(rows(layer)) == set(IDS)
    assert controller.project_status("p1").last_refresh == before

    fake.stop()
    wait_until(lambda: plugin.indicators.stale_layers() == [layer.id()])
    status = controller.project_status("p1")
    assert status.last_refresh == before
    assert "unknown time" not in status.describe()
    assert set(rows(layer)) == set(IDS)

    QgsProject.instance().clear()
    reopened = controller.add_layer("p1", "observations")
    assert reopened is not None
    assert set(rows(reopened)) == set(IDS)


def test_refresh_after_a_failed_refetch_completes_the_rebuild(plugin, fake):
    layer = synced_layer(plugin)
    controller = plugin.controller
    source = layer.source()
    fake.fail_next("layers.getFeatures", "INTERNAL_ERROR", layerId="observations")
    change_categories(fake)
    wait_until(lambda: controller.project_status("p1").error)

    fake.projects["p1"].features["observations"].append(observation("h"))
    plugin.dock.refresh_button.click()
    wait_until(lambda: layer.source() != source, message="switched to new table")
    wait_until(lambda: set(rows(layer)) == {*IDS, "h"})
    status = controller.project_status("p1")
    assert status.error is None
    assert "cat-river" in renderer_values(layer)
