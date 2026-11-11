"""The media panel against tests/fake_server.py and a real local HTTP server, for
cases the dev server cannot produce: late replies, a file that syncs later."""

from __future__ import annotations

import pytest
from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, Qt
from qgis.PyQt.QtGui import QColor, QImage

from comapeo_qgis.core.model import Attachment
from comapeo_qgis.plugin import CoMapeoPlugin
from tests.fake_server import FakeProject, FakeServer, observation
from tests.media_server import MediaServer
from tests.qgis.conftest import pump, use_discovery, wait_until
from tests.qgis.helpers import rows
from tests.qgis.media_helpers import (
    media_files,
    photo_items,
    select,
    shown_pixmap,
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


def test_download_all_media_can_be_cancelled(plugin, fake, http):
    a = observation("a")
    a["attachments"] = [photo(http, "one", "#ff0000"), photo(http, "two", "#00ff00")]
    start(plugin, fake, [a])
    dock = plugin.dock
    dock.projects.topLevelItem(0).setSelected(True)
    for variant in ("thumbnail", "preview", "original"):
        http.hold(f"/blobs/two/{variant}")
    dock.download_button.click()
    assert not dock.cancel_download_button.isHidden()
    wait_until(lambda: len(media_files()) == 3, message="the files not held back")
    dock.cancel_download_button.click()
    wait_until(lambda: dock.cancel_download_button.isHidden())
    assert dock.download_message.text().startswith(
        "Cancelled. Media files: 3 downloaded"
    )
    for variant in ("thumbnail", "preview", "original"):
        http.release(f"/blobs/two/{variant}")
    pump(300)
    assert (
        sorted(p.parent.name for p in media_files())
        == [media_files()[0].parent.name] * 3
    )
    assert not any(p.name.startswith(".part-") for p in media_files())


def test_a_download_cut_off_part_way_is_not_kept(plugin, fake, http):
    a = observation("a")
    attachment = photo(http, "cut", "#ff0000")
    a["attachments"] = [attachment]
    whole = jpeg("#ff0000")
    http.serve("/blobs/cut/thumbnail", whole, "image/jpeg", truncate=True)
    layer = start(plugin, fake, [a])
    panel = plugin.dock.media_area
    select(layer, ["a"])
    wait_until(lambda: [i.text() for i in photo_items(panel)] == ["Could not load"])
    assert media_files() == []

    http.serve("/blobs/cut/thumbnail", whole, "image/jpeg")
    plugin.dock.refresh_button.click()
    wait_until(lambda: [i.text() for i in photo_items(panel)] == [""])
    [saved] = media_files()
    assert saved.read_bytes() == whole


def test_a_failed_file_is_tried_again_on_refresh_and_when_reselected(
    plugin, fake, http
):
    a = observation("a")
    a["attachments"] = [photo(http, "flaky", "#ff0000")]
    layer = start(plugin, fake, [a, observation("b")])
    panel = plugin.dock.media_area
    http.serve("/blobs/flaky/thumbnail", b"oops", "text/plain", status=500)
    http.serve("/blobs/flaky/preview", b"oops", "text/plain", status=500)
    select(layer, ["a"])
    wait_until(lambda: [i.text() for i in photo_items(panel)] == ["Could not load"])
    panel.photos.itemClicked.emit(photo_items(panel)[0])
    wait_until(lambda: "could not be loaded" in panel.preview.text())

    http.serve("/blobs/flaky/thumbnail", jpeg("#ff0000"), "image/jpeg")
    plugin.dock.refresh_button.click()
    wait_until(lambda: [i.text() for i in photo_items(panel)] == [""])
    wait_until(lambda: "could not be loaded" in panel.preview.text())

    http.serve("/blobs/flaky/preview", jpeg("#ff0000"), "image/jpeg")
    select(layer, ["b"])
    select(layer, ["a"])
    wait_until(lambda: photo_items(panel) and photo_items(panel)[0].text() == "")
    panel.photos.itemClicked.emit(photo_items(panel)[0])
    wait_until(lambda: shows_image(panel.preview))


def test_the_panel_follows_the_media_server_to_a_new_port(plugin, fake, http):
    a = observation("a")
    a["attachments"] = [photo(http, "moved", "#ff0000")]
    layer = start(plugin, fake, [a])
    panel = plugin.dock.media_area
    select(layer, ["a"])
    wait_until(lambda: [i.text() for i in photo_items(panel)] == [""])

    moved = MediaServer().start()
    try:
        a["attachments"] = [photo(moved, "moved", "#ff0000")]
        http.stop()
        panel.photos.itemClicked.emit(photo_items(panel)[0])
        wait_until(lambda: "could not be loaded" in panel.preview.text())
        plugin.dock.refresh_button.click()
        wait_until(lambda: shows_image(panel.preview))
        assert moved.requests == ["/blobs/moved/preview"]
    finally:
        moved.stop()


def test_with_the_app_gone_the_panel_says_what_is_not_saved(plugin, fake, http):
    a = observation("a")
    a["attachments"] = [photo(http, "kept", "#ff0000")]
    layer = start(plugin, fake, [a])
    panel = plugin.dock.media_area
    select(layer, ["a"])
    wait_until(lambda: [i.text() for i in photo_items(panel)] == [""])
    fake.stop()
    wait_until(lambda: not plugin.controller.connected, message="disconnect")
    wait_until(lambda: "not connected" in panel.message.text())
    panel.photos.itemClicked.emit(photo_items(panel)[0])
    assert "not saved on this computer" in panel.preview.text()
    pump(300)
    assert "/blobs/kept/preview" not in http.requests


def test_cancelling_download_all_media_still_loads_the_selected_photo(
    plugin, fake, http
):
    a = observation("a")
    a["attachments"] = [photo(http, "shown", "#ff0000")]
    layer = start(plugin, fake, [a])
    dock = plugin.dock
    panel = dock.media_area
    http.hold("/blobs/shown/thumbnail")
    select(layer, ["a"])
    wait_until(lambda: "/blobs/shown/thumbnail" in http.requests)
    dock.projects.topLevelItem(0).setSelected(True)
    dock.download_button.click()
    wait_until(lambda: len(media_files()) == 2, message="preview and original")
    dock.cancel_download_button.click()
    wait_until(lambda: dock.cancel_download_button.isHidden())
    http.release("/blobs/shown/thumbnail")
    wait_until(lambda: [i.text() for i in photo_items(panel)] == [""])


def test_remove_cached_data_leaves_nothing_behind_while_photos_load(
    plugin, fake, http, monkeypatch
):
    from qgis.PyQt.QtWidgets import QMessageBox

    a = observation("a")
    names = [f"held{n}" for n in range(4)]
    a["attachments"] = [photo(http, name, "#ff0000") for name in names]
    layer = start(plugin, fake, [a])
    dock = plugin.dock
    panel = dock.media_area
    for name in names:
        http.hold(f"/blobs/{name}/thumbnail")
    select(layer, ["a"])
    wait_until(lambda: sum("thumbnail" in p for p in http.requests) == 4)
    dock.projects.topLevelItem(0).setSelected(True)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    dock.remove_button.click()
    assert "Select an observation" in panel.message.text()
    for name in names:
        http.release(f"/blobs/{name}/thumbnail")
    pump(500)
    assert media_files() == []


def test_the_larger_view_has_room_in_a_shown_window(plugin, fake, http, iface):
    a = observation("a")
    a["attachments"] = [photo(http, "big", "#ff0000")]
    layer = start(plugin, fake, [a])
    window = iface.mainWindow()
    window.resize(1200, 900)
    window.show()
    try:
        panel = plugin.dock.media_area
        select(layer, ["a"])
        wait_until(lambda: [i.text() for i in photo_items(panel)] == [""])
        panel.photos.itemClicked.emit(photo_items(panel)[0])
        wait_until(lambda: shows_image(panel.preview))
        height = shown_pixmap(panel.preview).height()
        wait_until(lambda: panel.preview.height() >= height, message="room")
    finally:
        window.hide()


def test_an_unknown_media_variant_is_never_fetched(plugin, fake, http):
    start(plugin, fake, [observation("a")])
    fetcher = plugin.controller.fetcher
    http.serve("/blobs/odd/huge", jpeg("#ff0000"), "image/jpeg")
    odd = Attachment(
        "photo", "odd", "drive/photo/odd", {"huge": http.url("/blobs/odd/huge")}
    )
    assert not fetcher.fetch("p1", odd, "huge")
    pump(300)
    assert not fetcher.is_fetching("p1", odd.key, "huge")
    assert "/blobs/odd/huge" not in http.requests
    assert media_files() == []


def test_download_all_media_skips_a_variant_it_does_not_know(plugin, fake, http):
    a = observation("a")
    attachment = photo(http, "odd", "#ff0000")
    http.serve("/blobs/odd/huge", jpeg("#ff0000"), "image/jpeg")
    attachment["urls"]["huge"] = http.url("/blobs/odd/huge")
    a["attachments"] = [attachment]
    start(plugin, fake, [a])
    dock = plugin.dock
    dock.projects.topLevelItem(0).setSelected(True)
    dock.download_button.click()
    wait_until(lambda: dock.cancel_download_button.isHidden(), message="finished")
    assert dock.download_message.text().startswith("Media files: 3 downloaded")
    assert "/blobs/odd/huge" not in http.requests
    assert len(media_files()) == 3
    assert not any(p.name.startswith(".part-") for p in media_files())
