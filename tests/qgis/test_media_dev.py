"""The media panel against the real dev server and its media server."""

# urlopen only ever gets the dev server's own http URLs on 127.0.0.1.
# ruff: noqa: S310

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from comapeo_qgis.plugin import CoMapeoPlugin
from tests.qgis.conftest import server_or_skip, use_discovery, wait_until
from tests.qgis.helpers import Oracle, rows
from tests.qgis.media_helpers import (
    buttons,
    media_files,
    media_root,
    photo_items,
    shown_pixmap,
)
from tests.qgis.media_helpers import select as select_docs

pytestmark = pytest.mark.real_server


@pytest.fixture(scope="module")
def server():
    dev = server_or_skip(observations=60, tracks=1, photos=6, projects=1)
    yield dev
    dev.stop()


@pytest.fixture(scope="module")
def served(server):
    """Each observation's attachments, and the HTTP status of every variant."""
    oracle = Oracle(server.discovery_dir)
    try:
        features = oracle.features("observations")
        project_id = oracle.project_id
    finally:
        oracle.close()
    status = {}
    for feature in features.values():
        for attachment in feature["attachments"]:
            for variant, url in attachment["urls"].items():
                status[attachment["key"], variant] = http_status(url)
    return project_id, features, status


def http_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=20) as reply:
            return reply.status
    except urllib.error.HTTPError as err:
        return err.code


def http_body(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=20) as reply:
        return reply.read()


def find(served, kind: str, available: bool) -> dict:
    _project_id, features, status = served
    for feature in features.values():
        matching = [a for a in feature["attachments"] if a["type"] == kind]
        if matching and all(
            (status[a["key"], "original"] == 200) is available for a in matching
        ):
            return feature
    raise AssertionError(f"the dev server served no observation with {kind}")


@pytest.fixture
def plugin(iface, server):
    use_discovery(server.discovery_dir)
    instance = CoMapeoPlugin(iface)
    instance.initGui()
    yield instance
    instance.unload()


def observations_layer(plugin, served):
    project_id, features, _status = served
    controller = plugin.controller
    wait_until(lambda: controller.available_layers(project_id), message="catalog")
    layer = controller.add_layer(project_id, "observations")
    wait_until(lambda: set(rows(layer)) == set(features), message="rows")
    wait_until(lambda: not controller.project_status(project_id).busy)
    return layer


def select(layer, feature) -> None:
    select_docs(layer, [feature["id"]])


def test_photos_show_as_thumbnails_with_a_larger_view_and_the_original(
    plugin, served, opened
):
    layer = observations_layer(plugin, served)
    panel = plugin.dock.media_area
    feature = find(served, "photo", available=True)
    photos = [a for a in feature["attachments"] if a["type"] == "photo"]
    select(layer, feature)
    wait_until(lambda: [i.text() for i in photo_items(panel)] == [""] * len(photos))
    for item in photo_items(panel):
        assert not item.icon().isNull()
    thumbnails = [p for p in media_files() if p.name.startswith("thumbnail")]
    assert sorted(p.read_bytes() for p in thumbnails) == sorted(
        http_body(a["urls"]["thumbnail"]) for a in photos
    )
    assert all(p.suffix == ".jpg" for p in thumbnails)
    assert not [p for p in media_files() if p.name.startswith(("preview", "original"))]

    panel.photos.itemClicked.emit(photo_items(panel)[0])
    wait_until(lambda: shown_pixmap(panel.preview))
    previews = [p for p in media_files() if p.name.startswith("preview")]
    assert [p.read_bytes() for p in previews] == [
        http_body(photos[0]["urls"]["preview"])
    ]
    assert shown_pixmap(panel.preview).width() > 96
    assert not [p for p in media_files() if p.name.startswith("original")]

    panel.open_original_button.click()
    wait_until(lambda: opened)
    assert opened[0].name == "original.jpg"
    assert opened[0].read_bytes() == http_body(photos[0]["urls"]["original"])


def test_a_photo_not_yet_synced_shows_a_placeholder(plugin, served):
    layer = observations_layer(plugin, served)
    panel = plugin.dock.media_area
    feature = find(served, "photo", available=False)
    select(layer, feature)
    wait_until(lambda: "Not yet synced" in [i.text() for i in photo_items(panel)])


def test_audio_is_cached_with_its_type_and_played_in_the_panel(plugin, served, opened):
    layer = observations_layer(plugin, served)
    panel = plugin.dock.media_area
    feature = find(served, "audio", available=True)
    [audio] = [a for a in feature["attachments"] if a["type"] == "audio"][:1]
    select(layer, feature)
    wait_until(lambda: buttons(panel, "openAudioButton"), message="audio control")
    if not panel.player.available:
        # A QGIS build without QtMultimedia offers only the system player.
        assert not buttons(panel, "playButton")
        buttons(panel, "openAudioButton")[0].click()
        wait_until(lambda: opened, message="opened in the system player")
        assert opened[0].name == "original.wav"
        assert opened[0].read_bytes() == http_body(audio["urls"]["original"])
        return
    play = buttons(panel, "playButton")[0]
    play.click()
    wait_until(lambda: panel.player.path is not None, message="playback started")
    assert panel.player.path.name == "original.wav"
    assert panel.player.path.read_bytes() == http_body(audio["urls"]["original"])
    assert panel.player.path.read_bytes()[:4] == b"RIFF"
    assert buttons(panel, "openAudioButton")


def test_media_is_shown_from_the_cache_when_the_app_is_closed(iface, opened):
    dev = server_or_skip(observations=60, tracks=1, photos=6, projects=1)
    use_discovery(dev.discovery_dir)
    plugin = CoMapeoPlugin(iface)
    plugin.initGui()
    try:
        oracle = Oracle(dev.discovery_dir)
        features = oracle.features("observations")
        oracle.close()
        here = (oracle.project_id, features, {})
        layer = observations_layer(plugin, here)
        feature = next(
            f
            for f in features.values()
            if len([a for a in f["attachments"] if a["type"] == "photo"]) >= 1
            and all(http_status(a["urls"]["original"]) == 200 for a in f["attachments"])
        )
        panel = plugin.dock.media_area
        select(layer, feature)
        count = len([a for a in feature["attachments"] if a["type"] == "photo"])
        wait_until(lambda: [i.text() for i in photo_items(panel)] == [""] * count)
        dev.stop()
        wait_until(lambda: not plugin.controller.connected, message="disconnect")
        layer.removeSelection()
        select(layer, feature)
        wait_until(lambda: "not connected" in panel.message.text())
        assert [i.text() for i in photo_items(panel)] == [""] * count
        panel.photos.itemClicked.emit(photo_items(panel)[0])
        assert "not saved on this computer" in panel.preview.text()
        panel.open_original_button.click()
        assert "not saved on this computer" in panel.action_message.text()
        assert opened == []
    finally:
        plugin.unload()
        dev.stop()


def test_download_all_media_then_remove_cached_data(plugin, served, monkeypatch):
    status = served[2]
    observations_layer(plugin, served)
    dock = plugin.dock
    for i in range(dock.projects.topLevelItemCount()):
        dock.projects.topLevelItem(i).setSelected(True)
    wait_until(lambda: dock.download_button.isEnabled())
    dock.download_button.click()
    wait_until(lambda: "downloaded" in dock.download_message.text(), timeout=60)
    available = sorted(k for k, s in status.items() if s == 200)
    missing = sorted(k for k, s in status.items() if s == 404)
    assert len(media_files()) == len(available)
    assert f"{len(available)} downloaded" in dock.download_message.text()
    assert f"{len(missing)} not yet synced" in dock.download_message.text()
    assert dock.download_progress.isHidden()

    from qgis.PyQt.QtWidgets import QMessageBox

    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    dock.remove_button.click()
    assert media_files() == []
    assert not any(media_root().glob("*/*"))
