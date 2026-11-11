"""Less common situations, served by tests/fake_server.py."""

from __future__ import annotations

import base64
import os
import struct

import pytest
from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsMarkerSymbol,
    QgsRasterMarkerSymbolLayer,
    QgsSingleSymbolRenderer,
)
from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice
from qgis.PyQt.QtGui import QColor, QImage
from qgis.PyQt.QtWidgets import QMessageBox

from comapeo_qgis.plugin import CoMapeoPlugin
from comapeo_qgis.qgis_tier.connection import ServerState
from tests.fake_server import FakeProject, FakeServer, observation
from tests.qgis.conftest import cache_root, pump, use_discovery, wait_until
from tests.qgis.helpers import rows, shown_text, symbol_for


@pytest.fixture
def fake():
    server = FakeServer().start()
    yield server
    server.stop()


@pytest.fixture
def plugin(iface):
    instance = CoMapeoPlugin(iface)
    yield instance
    instance.unload()


def start(plugin, fake, project: FakeProject | None = None):
    fake.add_project(project or FakeProject("p1", "Fake project"))
    use_discovery(fake.write_discovery())
    plugin.initGui()
    wait_until(lambda: plugin.controller.available_layers("p1"))
    return plugin.controller


def synced(controller, layer_id: str, expected: set):
    layer = controller.add_layer("p1", layer_id)
    assert layer is not None
    wait_until(lambda: set(rows(layer)) == expected)
    wait_until(lambda: not controller.project_status("p1").busy)
    return layer


def feature_requests(fake: FakeServer) -> int:
    return sum(1 for r in fake.requests if r.get("method") == "layers.getFeatures")


def png_icon() -> bytes:
    image = QImage(4, 4, QImage.Format.Format_ARGB32)
    image.fill(QColor("#00aa00"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def test_several_pages_are_fetched_in_full(plugin, fake):
    project = FakeProject("p1", "Fake project")
    ids = {f"o{i:04d}" for i in range(2500)}
    project.features["observations"] = [observation(i) for i in sorted(ids)]
    controller = start(plugin, fake, project)
    synced(controller, "observations", ids)
    assert feature_requests(fake) >= 3


def test_empty_project_gives_empty_valid_layers(plugin, fake):
    controller = start(plugin, fake)
    layers = controller.add_project("p1")
    assert len(layers) == 2
    wait_until(lambda: controller.project_status("p1").last_refresh is not None)
    wait_until(lambda: not controller.project_status("p1").busy)
    for layer in layers:
        assert layer.isValid()
        assert layer.featureCount() == 0
    assert controller.project_status("p1").describe().startswith("Up to date")


def test_repeating_cursor_stops_the_fetch_with_an_error(plugin, fake):
    project = FakeProject("p1", "Fake project")
    project.features["observations"] = [observation(i) for i in "abcde"]
    fake.stuck_cursor = "same-every-time"
    controller = start(plugin, fake, project)
    controller.add_layer("p1", "observations")
    wait_until(lambda: "same page" in (controller.project_status("p1").error or ""))
    wait_until(lambda: not controller.project_status("p1").busy)
    seen = feature_requests(fake)
    pump(500)
    assert feature_requests(fake) == seen
    assert seen <= 6


def test_incompatible_app_is_not_retried_until_its_file_changes(plugin):
    old = FakeServer(protocol_versions=(2,)).start()
    new = FakeServer().start()
    try:
        use_discovery(old.write_discovery())
        plugin.initGui()
        controller = plugin.controller
        states: list[ServerState] = []
        controller.changed.connect(
            lambda: states.extend(s.state for s in controller.server_statuses)
        )
        wait_until(lambda: ServerState.INCOMPATIBLE in states)
        pump(3500)
        assert old.accepted == 1
        first = states.index(ServerState.INCOMPATIBLE)
        assert set(states[first:]) == {ServerState.INCOMPATIBLE}

        # The app is updated: a new discovery file in the same place.
        new.add_project(FakeProject("p1", "Fake project"))
        new.write_discovery(old.dir)
        plugin.dock.refresh_button.click()
        wait_until(lambda: controller.connected)
    finally:
        old.stop()
        new.stop()


def test_protocol_violation_is_described_in_plain_words(plugin, fake):
    controller = start(plugin, fake)
    messages: list[str] = []
    controller.changed.connect(
        lambda: messages.extend(s.message for s in controller.server_statuses)
    )
    fake.broadcast_raw(struct.pack("<I", 0xFFFFFFF0))
    wait_until(lambda: any("unexpected reply" in m for m in messages))
    [message] = {m for m in messages if "unexpected reply" in m}
    assert "ProtocolViolation" not in message
    assert "bytes" in message


def test_reset_to_comapeo_style(plugin, fake, iface):
    controller = start(plugin, fake)
    layer = synced(controller, "observations", set())
    layer.setRenderer(QgsSingleSymbolRenderer(QgsMarkerSymbol()))
    [action] = [a for a in iface.layer_actions if a.text() == "Reset to CoMapeo style"]
    assert layer.id() in iface.layer_actions[action][2]
    iface.active_layer = layer
    action.trigger()
    renderer = layer.renderer()
    assert isinstance(renderer, QgsCategorizedSymbolRenderer)
    assert "cat-school" in {c.value() for c in renderer.categories()}


def test_select_field_shows_option_labels(plugin, fake):
    project = FakeProject("p1", "Fake project")
    project.features["observations"] = [
        observation("a", building_type="school"),
        observation("b", building_type="clinic"),
    ]
    controller = start(plugin, fake, project)
    layer = synced(controller, "observations", {"a", "b"})
    shown = rows(layer)
    assert shown["a"]["building_type"] == "school"
    assert shown_text(layer, "building_type", "school") == "School"
    assert "clinic" in shown_text(layer, "building_type", shown["b"]["building_type"])
    assert shown_text(layer, "category", shown["a"]["category"]) == "School"


def test_selection_on_a_comapeo_layer_is_announced(plugin, fake):
    project = FakeProject("p1", "Fake project")
    project.features["observations"] = [observation("a"), observation("b")]
    controller = start(plugin, fake, project)
    layer = synced(controller, "observations", {"a", "b"})
    seen = []
    controller.selectionChanged.connect(lambda *args: seen.append(args))
    layer.selectByIds([rows(layer)["b"].id()])
    assert seen[-1] == ("p1", "observations", ["b"])


def test_png_category_icon_is_drawn_from_the_embedded_image(plugin, fake):
    icon = png_icon()
    project = FakeProject("p1", "Fake project")
    project.categories[0]["icon"] = {
        "mimeType": "image/png",
        "data": base64.b64encode(icon).decode(),
    }
    project.features["observations"] = [observation("a")]
    controller = start(plugin, fake, project)
    layer = synced(controller, "observations", {"a"})
    symbol = symbol_for(layer, rows(layer)["a"])
    [raster] = [
        s for s in symbol.symbolLayers() if isinstance(s, QgsRasterMarkerSymbolLayer)
    ]
    assert raster.path() == "base64:" + base64.b64encode(icon).decode()


def test_unreadable_feature_withholds_deletes_and_warns(plugin, fake):
    project = FakeProject("p1", "Fake project")
    project.features["observations"] = [observation("a"), observation("b")]
    controller = start(plugin, fake, project)
    layer = synced(controller, "observations", {"a", "b"})

    broken = observation("x")
    del broken["version"]
    project.features["observations"] = [observation("a"), broken]
    plugin.dock.refresh_button.click()
    wait_until(lambda: controller.project_status("p1").warning)
    wait_until(lambda: not controller.project_status("p1").busy)
    assert set(rows(layer)) == {"a", "b"}
    item = plugin.dock.projects.topLevelItem(0)
    assert "could not read" in item.toolTip(1)


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="needs directory permissions that bind this user (not root)",
)
def test_cached_data_that_cannot_be_deleted_is_reported(plugin, fake, monkeypatch):
    project = FakeProject("p1", "Fake project")
    project.features["observations"] = [observation("a")]
    controller = start(plugin, fake, project)
    synced(controller, "observations", {"a"})
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
    dock = plugin.dock
    dock.projects.topLevelItem(0).setSelected(True)
    os.chmod(cache_root(), 0o500)
    try:
        dock.remove_button.click()
    finally:
        os.chmod(cache_root(), 0o700)
    [(_, title, text)] = warnings
    assert title == "Remove cached data"
    assert "Could not delete" in text
    assert controller.cache("p1").exists
