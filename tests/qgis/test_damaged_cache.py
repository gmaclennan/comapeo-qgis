"""A cache GeoPackage that exists but cannot be opened."""

from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from qgis.core import QgsBrowserModel, QgsProject
from qgis.PyQt.QtCore import QModelIndex
from qgis.PyQt.QtWidgets import QMessageBox

from comapeo_qgis.plugin import CoMapeoPlugin
from comapeo_qgis.qgis_tier import cache as cache_module
from comapeo_qgis.qgis_tier.cache import ProjectCache
from tests.fake_server import FakeProject, FakeServer, observation, track
from tests.qgis.conftest import (
    cache_root,
    process_events,
    pump,
    use_discovery,
    wait_until,
)
from tests.qgis.helpers import comapeo_map_layers, rows
from tests.qgis.test_browser_model import child_index

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


def damage(path: str, kind: str = "not a database") -> bytes:
    """Damage the cache as SQLite reports it: as no database, as malformed, or
    readable but without the plugin's metadata."""
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    if kind == "not a database":
        with open(path, "wb") as f:
            f.write(DAMAGED)
    elif kind == "malformed":
        # Byte 100 is the type of the schema table's first page; 0 is no valid type.
        with open(path, "r+b") as f:
            f.seek(100)
            f.write(b"\x00")
    else:
        conn = sqlite3.connect(path)
        conn.execute("DROP TABLE comapeo_meta")
        conn.commit()
        conn.close()
    with open(path, "rb") as f:
        damaged = f.read()
    error = sqlite_error(path)
    assert error is not None and kind.split()[-1] in error, error
    return damaged


def sqlite_error(path: str) -> str | None:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0)
    except sqlite3.Error as err:
        return str(err)
    try:
        conn.execute("SELECT key FROM comapeo_meta").fetchall()
    except sqlite3.Error as err:
        return str(err)
    finally:
        conn.close()
    return None


def moved_aside() -> list[str]:
    return sorted(name for name in os.listdir(cache_root()) if ".damaged-" in name)


@pytest.mark.parametrize("kind", ["not a database", "malformed", "no comapeo_meta"])
def test_a_damaged_cache_is_rebuilt_when_connected_keeping_layers_and_style(
    iface, fake, folder, monkeypatch, kind
):
    project_file, cache, styling = save_project_with_layers(iface, fake, folder)
    damaged = damage(cache, kind)

    plugin = CoMapeoPlugin(iface)
    try:
        assert QgsProject.instance().read(project_file)
        plugin.initGui()
        observations = next(
            layer for layer in comapeo_map_layers() if layer.name() == "My observations"
        )
        [tracks] = comapeo_map_layers("tracks")
        controller = plugin.controller
        [aside] = wait_until(moved_aside, message="damaged cache set aside")
        assert aside.startswith(os.path.basename(cache))
        with open(os.path.join(cache_root(), aside), "rb") as f:
            assert f.read() == damaged
        wait_until(lambda: observations.isValid() and tracks.isValid(), timeout=30)
        wait_until(lambda: set(rows(observations)) == {"a", "b"})
        wait_until(lambda: set(rows(tracks)) == {"t1"})
        wait_until(lambda: controller.project_status("p1").last_refresh is not None)
        assert sqlite_error(cache) is None
        assert observations.renderer().dump() == styling["renderer"]
        assert observations.opacity() == styling["opacity"]
        assert moved_aside() == [aside]
        assert controller.project_status("p1").error is None

        monkeypatch.setattr(
            QMessageBox,
            "question",
            lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
        )
        plugin.dock.projects.topLevelItem(0).setSelected(True)
        plugin.dock.remove_button.click()
        assert not os.path.exists(cache)
        assert moved_aside() == []
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

        model = QgsBrowserModel()
        model.initialize()
        try:
            root = child_index(model, QModelIndex(), "CoMapeo")
            offline = child_index(model, root, "Cached projects (offline)")
            project = child_index(model, offline, "Fake project")
            if model.canFetchMore(project):
                model.fetchMore(project)
            pump(100)
            shown = [
                model.data(model.index(row, 0, project))
                for row in range(model.rowCount(project))
            ]
            assert not any("GPKG" in str(t) or "OGR" in str(t) for t in shown), shown
        finally:
            model.deleteLater()
            process_events()

        plugin.dock.remove_button.click()
        assert comapeo_map_layers() == []
        assert not os.path.exists(cache)
    finally:
        plugin.unload()


@contextlib.contextmanager
def locked(path: str) -> Iterator[None]:
    holder = sqlite3.connect(path, isolation_level=None)
    try:
        holder.execute("PRAGMA locking_mode=EXCLUSIVE")
        holder.execute("BEGIN EXCLUSIVE")
        holder.execute("UPDATE comapeo_meta SET value = value")
        yield
    finally:
        holder.rollback()
        holder.close()


@contextlib.contextmanager
def unreadable(path: str) -> Iterator[None]:
    os.chmod(path, 0)
    try:
        yield
    finally:
        os.chmod(path, 0o600)


@pytest.mark.parametrize(
    "hold",
    [
        locked,
        pytest.param(
            unreadable,
            marks=pytest.mark.skipif(os.name != "posix", reason="POSIX permissions"),
        ),
    ],
)
def test_a_cache_another_program_holds_is_not_taken_for_damage(
    iface, fake, folder, hold
):
    _project_file, cache, _styling = save_project_with_layers(iface, fake, folder)
    with hold(cache):
        assert sqlite_error(cache) is not None
        started = time.monotonic()
        assert not ProjectCache(cache_root(), "p1").is_damaged()
        # A lock must not hold up the main thread for SQLite's default busy timeout.
        assert time.monotonic() - started < 2
    assert moved_aside() == []
    assert sqlite_error(cache) is None


def test_copies_set_aside_within_one_second_are_all_kept(monkeypatch, folder):
    class Frozen(cache_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 11, 9, 30, 0, tzinfo=tz)

    monkeypatch.setattr(cache_module, "datetime", Frozen)
    cache = ProjectCache(Path(folder), "p1")
    names = []
    for copy in range(3):
        cache.path.write_bytes(b"copy %d" % copy)
        names.append(cache.set_aside().name)
    assert len(set(names)) == 3
    kept = {(cache.path.parent / name).read_bytes() for name in names}
    assert kept == {b"copy 0", b"copy 1", b"copy 2"}
    cache.remove()
    assert os.listdir(folder) == []
