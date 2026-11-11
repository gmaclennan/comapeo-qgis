"""A cache GeoPackage that another program holds an exclusive lock on."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import time
from typing import Any

import pytest
from qgis.core import QgsProject
from qgis.PyQt.QtCore import QTimer

from comapeo_qgis.plugin import CoMapeoPlugin
from comapeo_qgis.qgis_tier import controller as controller_module
from tests.fake_server import FakeProject, FakeServer, observation, track
from tests.qgis.conftest import pump, wait_until
from tests.qgis.helpers import comapeo_map_layers, held_lock, rows
from tests.qgis.test_damaged_cache import moved_aside, save_project_with_layers

# GDAL waits 5 s on a lock for each open, so a single wait by the plugin shows.
MARGIN = 2.0


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


def journal_mode(path: str) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()


class SlotTime:
    """Time spent in the slots connected to a project's signals between two
    connections of this probe: here, the plugin's own."""

    def __init__(self, project: QgsProject) -> None:
        self.project = project
        self.total = 0.0
        self._started: dict[str, float] = {}
        self._connections: list[tuple[Any, Any]] = []

    def _connect(self, signal: Any, slot: Any) -> None:
        signal.connect(slot)
        self._connections.append((signal, slot))

    def before(self) -> None:
        for name in ("layersAdded", "readProject"):
            self._connect(getattr(self.project, name), self._start(name))

    def after(self) -> None:
        for name in ("layersAdded", "readProject"):
            self._connect(getattr(self.project, name), self._stop(name))

    def _start(self, name: str) -> Any:
        def slot(*_args: Any) -> None:
            self._started[name] = time.monotonic()

        return slot

    def _stop(self, name: str) -> Any:
        def slot(*_args: Any) -> None:
            self.total += time.monotonic() - self._started.pop(name)

        return slot

    def disconnect(self) -> None:
        for signal, slot in self._connections:
            signal.disconnect(slot)


class Stalls:
    """The longest the event loop went without running a 20 ms timer."""

    def __init__(self) -> None:
        self.longest = 0.0
        self._last = time.monotonic()
        self._timer = QTimer()
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self) -> None:
        now = time.monotonic()
        self.longest = max(self.longest, now - self._last)
        self._last = now

    def stop(self) -> float:
        self._timer.stop()
        self._tick()
        return self.longest


def panel_text(plugin) -> str:
    item = plugin.dock.projects.topLevelItem(0)
    return item.text(1) if item is not None else ""


def test_a_locked_cache_does_not_hold_up_qgis_and_is_read_once_free(
    iface, fake, folder, monkeypatch
):
    monkeypatch.setattr(controller_module, "PROJECT_POLL_MS", 200)
    project_file, cache, _styling = save_project_with_layers(iface, fake, folder)
    fake.stop()
    # An exclusive lock keeps readers out only outside write-ahead-log mode.
    assert journal_mode(cache) == "delete"
    project = QgsProject.instance()
    plugin = CoMapeoPlugin(iface)
    slots = SlotTime(project)
    try:
        with held_lock(cache):
            slots.before()
            started = time.monotonic()
            plugin.initGui()
            init_gui = time.monotonic() - started
            slots.after()
            assert project.read(project_file)
            stalls = Stalls()
            pump(1000)
            plugin.dock.refresh_button.click()
            pump(500)
            longest = stalls.stop()
            assert init_gui < MARGIN, init_gui
            assert slots.total < MARGIN, slots.total
            assert longest < MARGIN, longest
            assert len(comapeo_map_layers()) == 2
            text = panel_text(plugin)
            assert "another program" in text.lower(), text
        wait_until(
            lambda: panel_text(plugin).startswith("Not connected. Showing data cached"),
            message="the cache read once the lock is gone",
        )
        assert moved_aside() == []
    finally:
        slots.disconnect()
        plugin.unload()


def test_selecting_a_feature_offline_while_another_program_locks_the_cache(
    iface, fake, folder, no_uncaught_exceptions
):
    project_file, cache, _styling = save_project_with_layers(iface, fake, folder)
    fake.stop()
    plugin = CoMapeoPlugin(iface)
    try:
        plugin.initGui()
        assert QgsProject.instance().read(project_file)
        wait_until(
            lambda: panel_text(plugin).startswith("Not connected. Showing data cached")
        )
        [observations] = comapeo_map_layers("observations")
        found = rows(observations)
        panel = plugin.dock.media_area
        with held_lock(cache):
            observations.selectByIds([found["a"].id()])
            pump(200)
            assert no_uncaught_exceptions == []
            assert panel.message.text() == "No photos or audio."
    finally:
        plugin.unload()


def test_a_locked_cache_with_the_app_connected_is_updated_on_refresh(
    iface, fake, folder
):
    project_file, cache, _styling = save_project_with_layers(iface, fake, folder)
    fake.projects["p1"].features["observations"].append(observation("c"))
    project = QgsProject.instance()
    plugin = CoMapeoPlugin(iface)
    slots = SlotTime(project)
    try:
        with held_lock(cache):
            slots.before()
            started = time.monotonic()
            plugin.initGui()
            init_gui = time.monotonic() - started
            slots.after()
            assert project.read(project_file)
            stalls = Stalls()
            controller = plugin.controller
            wait_until(lambda: controller.connected, message="connected")
            wait_until(
                lambda: "another program" in panel_text(plugin).lower(),
                message="the lock reported",
            )
            pump(500)
            longest = stalls.stop()
            assert init_gui < MARGIN, init_gui
            assert slots.total < MARGIN, slots.total
            assert longest < MARGIN, longest
        [observations] = comapeo_map_layers("observations")
        assert set(rows(observations)) == {"a", "b"}
        plugin.dock.refresh_button.click()
        wait_until(lambda: set(rows(observations)) == {"a", "b", "c"})
        wait_until(lambda: panel_text(plugin).startswith("Up to date"))
        assert moved_aside() == []
    finally:
        slots.disconnect()
        plugin.unload()
