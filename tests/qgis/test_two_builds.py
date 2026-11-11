"""Several CoMapeo Desktop builds at once, found through their own app folders."""

from __future__ import annotations

import os
import shutil
import sys
from urllib.parse import unquote

import pytest
from qgis.core import QgsBrowserModel, QgsSettings
from qgis.PyQt.QtCore import QModelIndex
from qgis.PyQt.QtWidgets import QInputDialog

from comapeo_qgis.core.discovery import extra_key
from comapeo_qgis.plugin import CoMapeoPlugin
from comapeo_qgis.qgis_tier.browser import ROOT_PATH
from comapeo_qgis.qgis_tier.controller import extra_discovery_locations
from tests.fake_server import FakeProject, FakeServer, observation, short_tempdir
from tests.qgis.conftest import DISCOVERY_SETTING, process_events, pump, wait_until
from tests.qgis.helpers import rows
from tests.qgis.test_browser_model import child_index
from tests.qgis.test_discovery_states import browser_messages


@pytest.fixture
def home(monkeypatch):
    path = short_tempdir()
    monkeypatch.setenv("HOME", path)
    monkeypatch.setenv("APPDATA", os.path.join(path, "AppData", "Roaming"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("COMAPEO_LOCAL_API_DISCOVERY", raising=False)
    monkeypatch.delenv("COMAPEO_LOCAL_API_DISCOVERY_EXTRA", raising=False)
    QgsSettings().remove(DISCOVERY_SETTING)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def app_folder(home: str, name: str) -> str:
    """Where Electron puts each build's userData folder on this platform."""
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", name)
    if sys.platform == "win32":
        return os.path.join(home, "AppData", "Roaming", name)
    return os.path.join(home, ".config", name)


@pytest.fixture
def servers(home):
    started = []
    for folder in ("CoMapeo Desktop", "CoMapeo Desktop Internal"):
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


def test_project_of_a_closed_build_is_not_reported_gone(plugin, servers, home):
    production, internal = servers
    p1 = production.add_project(FakeProject("p1", "Production project"))
    p1.features["observations"] = [observation("a")]
    internal.add_project(FakeProject("p2", "Internal project"))
    plugin.initGui()
    controller = plugin.controller
    wait_until(lambda: controller.available_layers("p1"))
    wait_until(lambda: controller.available_layers("p2"))
    layer = controller.add_layer("p1", "observations")
    wait_until(lambda: set(rows(layer)) == {"a"})
    wait_until(lambda: not controller.project_status("p1").busy)

    production.stop()
    wait_until(lambda: not controller.project_status("p1").connected)
    internal.add_project(FakeProject("p3", "Joined later"))
    plugin.dock.refresh_button.click()
    wait_until(
        lambda: "p3" in {p.project_id for p in controller.served_projects("internal")},
        message="the internal build answered after Refresh",
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


def test_project_left_in_its_own_build_is_reported_gone(plugin, servers):
    production, internal = servers
    production.add_project(FakeProject("p1", "Production project"))
    internal.add_project(FakeProject("p2", "Internal project"))
    plugin.initGui()
    controller = plugin.controller
    wait_until(lambda: controller.available_layers("p1"))
    controller.add_layer("p1", "observations")
    wait_until(lambda: controller.project_status("p1").last_refresh is not None)

    production.projects.pop("p1")
    plugin.dock.refresh_button.click()
    wait_until(lambda: controller.project_status("p1").gone)
    assert "No longer available" in controller.project_status("p1").describe()


@pytest.fixture
def dev_build(home):
    """A development build: its data folder is in the desktop checkout."""
    data = os.path.join(home, "checkout", "data", "default")
    os.makedirs(os.path.join(data, "comapeo"))
    server = FakeServer(app_name="CoMapeo Desktop Dev").start()
    server.add_project(FakeProject("p9", "Dev project"))
    server.write_discovery(os.path.join(data, "comapeo"))
    yield server, data
    server.stop()


def test_a_build_outside_the_app_folders_is_found_once_added_in_the_panel(
    plugin, dev_build, servers, monkeypatch
):
    dev, data = dev_build
    servers[0].add_project(FakeProject("p1", "Production project"))
    plugin.initGui()
    controller = plugin.controller
    wait_until(lambda: controller.available_layers("p1"))
    pump(300)
    assert not controller.available_layers("p9")

    asked = []

    def answer(_parent, _title, _label, text):
        asked.append(text)
        return f"{data}\n", True

    monkeypatch.setattr(QInputDialog, "getMultiLineText", answer)
    plugin.dock.locations_button.click()
    wait_until(lambda: controller.available_layers("p9"), message="dev build found")
    assert asked == [""]
    assert extra_discovery_locations() == [data]
    assert controller.available_layers("p1")
    text = plugin.dock.status_label.text()
    assert "Connected to CoMapeo Desktop Dev" in text, text

    dev.stop()
    expected = (
        "CoMapeo Desktop Dev is not running, or QGIS access is switched off in "
        "its settings."
    )
    wait_until(lambda: expected in plugin.dock.status_label.text())
    assert "CoMapeo Desktop Dev: " not in plugin.dock.status_label.text()


def test_extra_locations_from_the_environment_are_searched(
    plugin, dev_build, monkeypatch
):
    _dev, data = dev_build
    monkeypatch.setenv("COMAPEO_LOCAL_API_DISCOVERY_EXTRA", data)
    plugin.initGui()
    wait_until(lambda: plugin.controller.available_layers("p9"))
    assert browser_messages() == ["CoMapeo Desktop Dev"]


def dev_statuses(controller) -> list:
    return [
        s for s in controller.server_statuses if s.app_name == "CoMapeo Desktop Dev"
    ]


def test_removing_an_extra_location_disconnects_its_build(
    plugin, dev_build, servers, monkeypatch
):
    dev, data = dev_build
    answers = [f"{data}\n", "", f"{data}{os.sep}\n{data}\n"]
    monkeypatch.setattr(
        QInputDialog,
        "getMultiLineText",
        lambda *_args: (answers.pop(0), True),
    )
    plugin.initGui()
    controller = plugin.controller
    plugin.dock.locations_button.click()
    wait_until(lambda: controller.available_layers("p9"), message="dev build found")
    assert dev.open_connections == 1

    plugin.dock.locations_button.click()
    wait_until(lambda: dev.open_connections == 0, message="dev build let go")
    wait_until(lambda: not dev_statuses(controller))
    assert "CoMapeo Desktop Dev" not in browser_messages()
    assert "CoMapeo Desktop Dev" not in plugin.dock.status_label.text()

    plugin.dock.locations_button.click()
    wait_until(lambda: controller.available_layers("p9"), message="found again")
    pump(500)
    assert dev.open_connections == 1
    assert len(dev_statuses(controller)) == 1
    assert browser_messages().count("CoMapeo Desktop Dev") == 1


def test_an_extra_location_is_one_step_of_its_browser_path(
    plugin, dev_build, monkeypatch
):
    _dev, data = dev_build
    monkeypatch.setenv("COMAPEO_LOCAL_API_DISCOVERY_EXTRA", data)
    plugin.initGui()
    wait_until(lambda: plugin.controller.available_layers("p9"))
    model = QgsBrowserModel()
    model.initialize()
    try:
        root = child_index(model, QModelIndex(), "CoMapeo")
        server = child_index(model, root, "CoMapeo Desktop Dev")
        project = child_index(model, server, "Dev project")
        server_path = model.dataItem(server).path()
        prefix, key, state = server_path.split("/")
        assert (prefix, unquote(key), state) == (
            ROOT_PATH,
            extra_key(data),
            "connected",
        )
        assert model.dataItem(project).path() == f"{server_path}/p9"
    finally:
        model.deleteLater()
        process_events()
