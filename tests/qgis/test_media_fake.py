"""The media panel against tests/fake_server.py and a real local HTTP server, for
cases the dev server cannot produce: late replies, a file that syncs later."""

from __future__ import annotations

import pytest
from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, Qt
from qgis.PyQt.QtGui import QColor, QImage

from comapeo_qgis.plugin import CoMapeoPlugin
from tests.fake_server import FakeProject, FakeServer, observation
from tests.media_server import MediaServer
from tests.qgis.conftest import pump, use_discovery, wait_until
from tests.qgis.helpers import rows
from tests.qgis.media_helpers import (
    media_files,
    photo_items,
    select,
)


def shows_image(label) -> bool:
    pixmap = label.pixmap()
    return pixmap is not None and not pixmap.isNull() and label.text() == ""


def jpeg(color: str) -> bytes:
    image = QImage(40, 30, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "JPG")
    buffer.close()
    return bytes(data)


@pytest.fixture
def http():
    server = MediaServer().start()
    yield server
    server.stop()


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


def photo(http: MediaServer, name: str, color: str | None) -> dict:
    """An attachment; with `color` None its files are not on the server (404)."""
    urls = {}
    for variant in ("thumbnail", "preview", "original"):
        path = f"/blobs/{name}/{variant}"
        if color is not None:
            http.serve(path, jpeg(color), "image/jpeg")
        urls[variant] = http.url(path)
    return {"type": "photo", "name": name, "key": f"drive/photo/{name}", "urls": urls}


def start(plugin, fake, features):
    project = FakeProject("p1", "Fake project")
    project.features["observations"] = features
    fake.add_project(project)
    use_discovery(fake.write_discovery())
    plugin.initGui()
    controller = plugin.controller
    wait_until(lambda: controller.available_layers("p1"))
    layer = controller.add_layer("p1", "observations")
    expected = {f["id"] for f in features}
    wait_until(lambda: set(rows(layer)) == expected)
    wait_until(lambda: not controller.project_status("p1").busy)
    return layer


def test_a_late_reply_does_not_show_under_a_newer_selection(plugin, fake, http):
    a = observation("a")
    a["attachments"] = [photo(http, "slow", "#ff0000")]
    b = observation("b")
    b["attachments"] = [photo(http, "quick", "#0000ff")]
    layer = start(plugin, fake, [a, b])
    panel = plugin.dock.media_area
    http.hold("/blobs/slow/thumbnail")
    select(layer, ["a"])
    wait_until(lambda: "/blobs/slow/thumbnail" in http.requests)
    assert [i.text() for i in photo_items(panel)] == ["Loading…"]
    select(layer, ["b"])
    wait_until(lambda: [i.text() for i in photo_items(panel)] == [""])
    http.release("/blobs/slow/thumbnail")
    wait_until(lambda: sum("thumbnail" in p.name for p in media_files()) == 2)
    pump(300)
    [item] = photo_items(panel)
    assert item.text() == ""
    assert item.data(Qt.ItemDataRole.UserRole) == "drive/photo/quick"


def test_not_yet_synced_shows_a_placeholder_and_is_retried_on_refresh(
    plugin, fake, http, opened
):
    a = observation("a")
    attachment = photo(http, "later", None)
    a["attachments"] = [attachment]
    layer = start(plugin, fake, [a])
    panel = plugin.dock.media_area
    select(layer, ["a"])
    wait_until(lambda: [i.text() for i in photo_items(panel)] == ["Not yet synced"])
    panel.photos.itemClicked.emit(photo_items(panel)[0])
    wait_until(lambda: "not synced" in panel.preview.text())
    panel.open_original_button.click()
    wait_until(lambda: "not synced" in panel.action_message.text())
    assert opened == []
    assert media_files() == []

    for variant in ("thumbnail", "preview", "original"):
        http.serve(f"/blobs/later/{variant}", jpeg("#00ff00"), "image/jpeg")
    requests_before = len(http.requests)
    pump(300)
    assert len(http.requests) == requests_before, "404s are not retried by themselves"
    plugin.dock.refresh_button.click()
    wait_until(lambda: [i.text() for i in photo_items(panel)] == [""])
    wait_until(lambda: shows_image(panel.preview))
    panel.open_original_button.click()
    wait_until(lambda: opened)
    assert opened[0].name == "original.jpg"
    assert opened[0].read_bytes() == jpeg("#00ff00")


def test_selection_hints(plugin, fake, http):
    layer = start(plugin, fake, [observation("a"), observation("b")])
    panel = plugin.dock.media_area
    assert "Select an observation" in panel.message.text()
    select(layer, ["a", "b"])
    assert panel.message.text().startswith("2 observations selected")
    assert panel.photos.isHidden()
    select(layer, ["a"])
    wait_until(lambda: panel.message.text() == "No photos or audio.")
    layer.removeSelection()
    assert "Select an observation" in panel.message.text()


def test_a_url_off_this_computer_is_never_fetched(plugin, fake, http):
    a = observation("a")
    a["attachments"] = [
        {
            "type": "photo",
            "name": "x",
            "key": "drive/photo/x",
            "urls": {"thumbnail": "http://example.org/x", "preview": "file:///etc/x"},
        }
    ]
    layer = start(plugin, fake, [a])
    panel = plugin.dock.media_area
    select(layer, ["a"])
    wait_until(lambda: [i.text() for i in photo_items(panel)] == ["Could not load"])
    assert media_files() == []
