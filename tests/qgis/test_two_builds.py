"""Several CoMapeo Desktop builds at once, found through their own app folders."""

from __future__ import annotations

import os
import shutil
import sys

import pytest
from qgis.core import QgsSettings

from comapeo_qgis.plugin import CoMapeoPlugin
from tests.fake_server import FakeProject, FakeServer, observation, short_tempdir
from tests.qgis.conftest import DISCOVERY_SETTING, wait_until
from tests.qgis.helpers import rows


@pytest.fixture
def home(monkeypatch):
    path = short_tempdir()
    monkeypatch.setenv("HOME", path)
    monkeypatch.delenv("COMAPEO_LOCAL_API_DISCOVERY", raising=False)
    QgsSettings().remove(DISCOVERY_SETTING)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def app_folder(home: str, name: str) -> str:
    return os.path.join(home, "Library", "Application Support", name)


@pytest.fixture
def servers(home):
    started = []
    for folder in ("CoMapeo Desktop", "CoMapeo Desktop Dev"):
        os.makedirs(app_folder(home, folder))
        server = FakeServer().start()
        server.write_discovery(app_folder(home, folder))
        started.append(server)
    yield started
    for server in started:
        server.stop()


@pytest.fixture
def plugin(iface):
    instance = CoMapeoPlugin(iface)
    yield instance
    instance.unload()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS app folders")
def test_project_of_a_closed_build_is_not_reported_gone(plugin, servers, home):
    production, development = servers
    p1 = production.add_project(FakeProject("p1", "Production project"))
    p1.features["observations"] = [observation("a")]
    development.add_project(FakeProject("p2", "Dev project"))
    plugin.initGui()
    controller = plugin.controller
    wait_until(lambda: controller.available_layers("p1"))
    wait_until(lambda: controller.available_layers("p2"))
    layer = controller.add_layer("p1", "observations")
    wait_until(lambda: set(rows(layer)) == {"a"})
    wait_until(lambda: not controller.project_status("p1").busy)

    production.stop()
    wait_until(lambda: not controller.project_status("p1").connected)
    development.add_project(FakeProject("p3", "Joined later"))
    plugin.dock.refresh_button.click()
    wait_until(
        lambda: (
            "p3" in {p.project_id for p in controller.served_projects("development")}
        ),
        message="the dev app answered after Refresh",
    )
    status = controller.project_status("p1")
    assert not status.gone
    assert status.describe().startswith("Not connected.")
    assert set(rows(layer)) == {"a"}

    # The build that has the project is started again, and still has it.
    again = FakeServer().start()
    servers.append(again)
    again.add_project(p1)
    again.write_discovery(app_folder(home, "CoMapeo Desktop"))
    wait_until(lambda: controller.project_status("p1").connected, timeout=30)
    assert not controller.project_status("p1").gone


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS app folders")
def test_project_left_in_its_own_build_is_reported_gone(plugin, servers):
    production, development = servers
    production.add_project(FakeProject("p1", "Production project"))
    development.add_project(FakeProject("p2", "Dev project"))
    plugin.initGui()
    controller = plugin.controller
    wait_until(lambda: controller.available_layers("p1"))
    controller.add_layer("p1", "observations")
    wait_until(lambda: controller.project_status("p1").last_refresh is not None)

    production.projects.pop("p1")
    plugin.dock.refresh_button.click()
    wait_until(lambda: controller.project_status("p1").gone)
    assert "No longer available" in controller.project_status("p1").describe()
