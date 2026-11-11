"""A cache GeoPackage that exists but cannot be opened."""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest
from qgis.core import QgsProject
from qgis.PyQt.QtWidgets import QMessageBox

from comapeo_qgis.plugin import CoMapeoPlugin
from tests.fake_server import FakeProject, FakeServer, observation, track
from tests.qgis.conftest import cache_root, use_discovery, wait_until
from tests.qgis.helpers import comapeo_map_layers, rows

DAMAGED = b"this is not a GeoPackage\n" * 200


@pytest.fixture
def fake():
    server = FakeServer().start()
    project = server.add_project(FakeProject("p1", "Fake project"))
    project.features["observations"] = [observation("a"), observation("b")]
    project.features["tracks"] = [track("t1")]
    yield server
    server.stop()


@pytest.fixture
def folder():
    path = tempfile.mkdtemp(prefix="cq-proj-")
    yield path
    shutil.rmtree(path, ignore_errors=True)


def save_project_with_layers(iface, fake, folder) -> tuple[str, str, dict]:
    """Adds the project's layers, styles one by hand, saves the QGIS project and
    closes everything. Returns the project file, the cache file and the styling."""
    use_discovery(fake.write_discovery())
    plugin = CoMapeoPlugin(iface)
    try:
        plugin.initGui()
        controller = plugin.controller
        wait_until(lambda: controller.available_layers("p1"))
        observations, tracks = controller.add_project("p1")
        wait_until(lambda: set(rows(observations)) == {"a", "b"})
        wait_until(lambda: set(rows(tracks)) == {"t1"})
        wait_until(lambda: not controller.project_status("p1").busy)
        observations.setName("My observations")
        observations.setOpacity(0.4)
        styling = {
            "renderer": observations.renderer().dump(),
            "opacity": observations.opacity(),
        }
        project_file = os.path.join(folder, "survey.qgz")
        assert QgsProject.instance().write(project_file)
        cache = str(controller.cache("p1").path)
    finally:
        plugin.unload()
    QgsProject.instance().clear()
    return project_file, cache, styling


def damage(path: str) -> None:
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    with open(path, "wb") as f:
        f.write(DAMAGED)


def moved_aside() -> list[str]:
    return sorted(name for name in os.listdir(cache_root()) if ".damaged-" in name)


def test_a_damaged_cache_is_rebuilt_when_connected_keeping_layers_and_style(
    iface, fake, folder
):
    project_file, cache, styling = save_project_with_layers(iface, fake, folder)
    damage(cache)

    plugin = CoMapeoPlugin(iface)
    try:
        assert QgsProject.instance().read(project_file)
        plugin.initGui()
        observations = next(
            layer for layer in comapeo_map_layers() if layer.name() == "My observations"
        )
        [tracks] = comapeo_map_layers("tracks")
        wait_until(lambda: observations.isValid() and tracks.isValid(), timeout=30)
        wait_until(lambda: set(rows(observations)) == {"a", "b"})
        wait_until(lambda: set(rows(tracks)) == {"t1"})
        assert observations.renderer().dump() == styling["renderer"]
        assert observations.opacity() == styling["opacity"]
        [aside] = moved_aside()
        assert aside.startswith(os.path.basename(cache))
        with open(os.path.join(cache_root(), aside), "rb") as f:
            assert f.read() == DAMAGED
        wait_until(lambda: not plugin.controller.project_status("p1").busy)
        assert plugin.controller.project_status("p1").error is None
    finally:
        plugin.unload()


def test_a_damaged_cache_offline_names_the_way_out(iface, fake, folder, monkeypatch):
    project_file, cache, _styling = save_project_with_layers(iface, fake, folder)
    fake.stop()
    damage(cache)

    plugin = CoMapeoPlugin(iface)
    try:
        assert QgsProject.instance().read(project_file)
        plugin.initGui()
        wait_until(lambda: plugin.dock.projects.topLevelItem(0) is not None)
        item = plugin.dock.projects.topLevelItem(0)
        status = plugin.controller.project_status("p1")
        text = status.describe()
        assert "Remove cached data" in text, text
        assert "GPKG" not in text and "OGR" not in text and "sqlite" not in text
        assert item.text(1) == text
        item.setSelected(True)
        assert plugin.dock.remove_button.isEnabled()
        assert moved_aside() == []
        with open(cache, "rb") as f:
            assert f.read() == DAMAGED
        monkeypatch.setattr(
            QMessageBox,
            "question",
            lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
        )
        plugin.dock.remove_button.click()
        assert comapeo_map_layers() == []
        assert not os.path.exists(cache)
    finally:
        plugin.unload()
