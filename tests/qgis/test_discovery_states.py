"""No app, or a broken discovery file: a clear message in the panel, no exception."""

from __future__ import annotations

import json
import os
import shutil
import tempfile

import pytest
from qgis.core import QgsBrowserModel
from qgis.PyQt.QtCore import QModelIndex

from comapeo_qgis.plugin import CoMapeoPlugin
from comapeo_qgis.qgis_tier.connection import NOT_RUNNING_MESSAGE, ServerState
from tests.qgis.conftest import process_events, use_discovery, wait_until
from tests.qgis.test_browser_model import child_index


@pytest.fixture
def folder():
    path = tempfile.mkdtemp(prefix="cq-disc-", dir="/tmp")
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def plugin(iface):
    instance = CoMapeoPlugin(iface)
    yield instance
    instance.unload()


def write(folder: str, text: str, mode: int = 0o600) -> None:
    path = os.path.join(folder, "comapeo-local-api.json")
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


def browser_messages() -> list[str]:
    model = QgsBrowserModel()
    model.initialize()
    try:
        root = child_index(model, QModelIndex(), "CoMapeo")
        if model.canFetchMore(root):
            model.fetchMore(root)
        wait_until(lambda: model.rowCount(root), message="CoMapeo children")
        return [model.index(r, 0, root).data() for r in range(model.rowCount(root))]
    finally:
        model.deleteLater()
        process_events()


def test_missing_folder_reads_as_not_running(plugin, folder):
    use_discovery(os.path.join(folder, "nowhere"))
    plugin.initGui()
    wait_until(lambda: plugin.dock.status_label.text() == NOT_RUNNING_MESSAGE)
    assert not plugin.controller.connected
    assert browser_messages() == ["CoMapeo Desktop is not running"]


def test_stale_file_from_a_crashed_app_reads_as_not_running(plugin, folder):
    write(
        folder,
        json.dumps(
            {
                "discoveryVersion": 1,
                "socketPath": os.path.join(folder, "gone.sock"),
                "secret": "x" * 43,
                "protocolVersions": [1],
                "pid": 2**31 - 2,
                "app": {"name": "CoMapeo Desktop", "version": "1.0.0"},
            }
        ),
    )
    use_discovery(folder)
    plugin.initGui()
    wait_until(lambda: NOT_RUNNING_MESSAGE in plugin.dock.status_label.text())
    [status] = plugin.controller.server_statuses
    assert status.state is ServerState.NOT_RUNNING


def test_socket_that_nobody_listens_on_reads_as_not_running(plugin, folder):
    socket_path = os.path.join(folder, "api.sock")
    open(socket_path, "w").close()
    write(
        folder,
        json.dumps(
            {
                "discoveryVersion": 1,
                "socketPath": socket_path,
                "secret": "x" * 43,
                "protocolVersions": [1],
                "app": {"name": "CoMapeo Desktop", "version": "1.0.0"},
            }
        ),
    )
    use_discovery(folder)
    plugin.initGui()
    wait_until(lambda: NOT_RUNNING_MESSAGE in plugin.dock.status_label.text())
    assert not plugin.controller.connected


def test_corrupt_file_is_reported(plugin, folder):
    write(folder, "{not json")
    use_discovery(folder)
    plugin.initGui()
    wait_until(lambda: "not valid JSON" in plugin.dock.status_label.text())
    [status] = plugin.controller.server_statuses
    assert status.state is ServerState.ERROR


def test_file_other_users_can_read_is_refused(plugin, folder):
    write(folder, json.dumps({"discoveryVersion": 1}), mode=0o644)
    use_discovery(folder)
    plugin.initGui()
    wait_until(lambda: "other users" in plugin.dock.status_label.text())
    assert not plugin.controller.connected
