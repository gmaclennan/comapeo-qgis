"""Runs inside a real QGIS window, started by `scripts/smoke_e2e.py --media`.

Selects observations with photos, with a photo not yet synced and with audio,
saves screenshots of the CoMapeo panel and writes a JSON report, then quits.
COMAPEO_SMOKE_MEDIA holds the document ids the driver picked from the server.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from qgis.core import Qgis, QgsApplication, QgsProject, QgsRectangle
from qgis.PyQt.QtCore import QEventLoop, QObject, Qt, QTimer, QUrl, pyqtSlot
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QPushButton
from qgis.utils import iface, plugins

PLUGIN = "comapeo_qgis"
OUT = Path(os.environ.get("COMAPEO_SMOKE_OUT", "."))
PREFIX = os.environ.get("COMAPEO_SMOKE_PREFIX", "media")
PROJECT_ID = os.environ.get("COMAPEO_SMOKE_PROJECT", "")
TARGETS = json.loads(os.environ.get("COMAPEO_SMOKE_MEDIA", "{}"))
HOME = str(Path.home())

report: dict[str, Any] = {
    "mode": "media",
    "qgisVersion": Qgis.version(),
    "qtPlatform": QgsApplication.platformName(),
    "steps": [],
    "exceptions": [],
    "messageLog": [],
    "failures": [],
    "images": [],
}


def scrub(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace(HOME, "~")
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value


def step(text: str) -> None:
    report["steps"].append(f"{time.strftime('%H:%M:%S')} {text}")


def pump(milliseconds: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def wait_until(predicate: Any, what: str, timeout: float = 60) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out after {timeout:.0f} s waiting for {what}")
        pump(50)


_previous_hook = sys.excepthook


def _hook(kind: Any, value: Any, tb: Any) -> None:
    report["exceptions"].append("".join(traceback.format_exception(kind, value, tb)))
    _previous_hook(kind, value, tb)


def _on_message(message: str, tag: str, level: Any) -> None:
    if level in (Qgis.MessageLevel.Warning, Qgis.MessageLevel.Critical) or tag in (
        "Python error",
        "CoMapeo",
    ):
        report["messageLog"].append(
            {"tag": tag, "level": getattr(level, "name", str(level)), "text": message}
        )


sys.excepthook = _hook
QgsApplication.messageLog().messageReceived.connect(_on_message)


class Opened(QObject):
    """Stands in for the system viewer so the smoke run never launches one."""

    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    @pyqtSlot(QUrl)
    def handle(self, url: QUrl) -> None:
        self.paths.append(url.toLocalFile())


OPENED = Opened()


def plugin() -> Any:
    return plugins.get(PLUGIN)


def panel() -> Any:
    return plugin().dock.media_area


def save_window(name: str) -> None:
    pump(400)
    path = OUT / f"{PREFIX}-{name}.png"
    iface.mainWindow().grab().save(str(path))
    report["images"].append(path.name)


def save_dock(name: str) -> None:
    pump(400)
    path = OUT / f"{PREFIX}-{name}.png"
    plugin().dock.grab().save(str(path))
    report["images"].append(path.name)


def captions() -> list[str]:
    photos = panel().photos
    return [photos.item(i).text() for i in range(photos.count())]


def visible_buttons(name: str) -> list[QPushButton]:
    return [b for b in panel().findChildren(QPushButton, name) if not b.isHidden()]


def observations_layer() -> Any:
    from comapeo_qgis.qgis_tier.layers import comapeo_ids, comapeo_layers

    for layer in comapeo_layers(QgsProject.instance()):
        if comapeo_ids(layer)[1] == "observations":
            return layer
    return None


def select(doc_id: str) -> None:
    controller = plugin().controller
    layer = observations_layer()
    field = controller.cache(PROJECT_ID).table("observations").field("doc_id")
    ids = [f.id() for f in layer.getFeatures() if f[field] == doc_id]
    iface.setActiveLayer(layer)
    layer.selectByIds(ids)
    feature = layer.getFeature(ids[0])
    if not feature.geometry().isNull():
        point = feature.geometry().asPoint()
        canvas = iface.mapCanvas()
        canvas.setExtent(
            QgsRectangle(
                point.x() - 0.02, point.y() - 0.02, point.x() + 0.02, point.y() + 0.02
            )
        )
        canvas.refresh()


def prepare() -> None:
    window = iface.mainWindow()
    window.showNormal()
    window.resize(1600, 1000)
    window.move(0, 0)
    window.raise_()
    window.activateWindow()
    dock = plugin().dock
    dock.show()
    dock.raise_()
    window.resizeDocks([dock], [460], Qt.Orientation.Horizontal)
    pump(300)


def run() -> None:
    QDesktopServices.setUrlHandler("file", OPENED, "handle")
    controller = plugin().controller
    step("waiting for the plugin to connect")
    wait_until(lambda: controller.connected, "the plugin to connect")
    wait_until(lambda: controller.available_layers(PROJECT_ID), "the layers")
    controller.add_project(PROJECT_ID)

    def synced() -> bool:
        status = controller.project_status(PROJECT_ID)
        return status.connected and not status.busy and status.last_refresh is not None

    wait_until(synced, "the first fetch", 120)
    dock = plugin().dock
    for row in range(dock.projects.topLevelItemCount()):
        item = dock.projects.topLevelItem(row)
        item.setSelected(item.data(0, Qt.ItemDataRole.UserRole) == PROJECT_ID)
    report["hintNoSelection"] = panel().message.text()

    step("photos")
    select(TARGETS["photos"])
    expected = TARGETS["photoCount"]
    wait_until(lambda: captions() == [""] * expected, "the thumbnails")
    report["thumbnails"] = {
        "captions": captions(),
        "iconsNull": [panel().photos.item(i).icon().isNull() for i in range(expected)],
        "message": panel().message.text(),
    }
    save_window("thumbnails-window")
    save_dock("thumbnails")

    photos = panel().photos
    item = photos.item(0)
    rect = photos.visualItemRect(item)
    QTest.mouseClick(photos.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())

    def preview_shown() -> bool:
        pixmap = panel().preview.pixmap()
        return pixmap is not None and not pixmap.isNull()

    wait_until(preview_shown, "the larger view")
    pixmap = panel().preview.pixmap()
    report["preview"] = {"width": pixmap.width(), "height": pixmap.height()}
    save_dock("preview")
    QTest.mouseClick(panel().open_original_button, Qt.MouseButton.LeftButton)
    wait_until(lambda: OPENED.paths, "the original to be opened")
    opened = Path(OPENED.paths[0])
    report["openedOriginal"] = {"name": opened.name, "bytes": opened.stat().st_size}

    step("not yet synced")
    select(TARGETS["notSynced"])
    wait_until(lambda: "Not yet synced" in captions(), "the not-synced placeholder")
    report["notSynced"] = {"captions": captions(), "message": panel().message.text()}
    save_dock("not-synced")

    step("audio")
    select(TARGETS["audio"])
    wait_until(lambda: visible_buttons("playButton"), "the audio control")
    pump(500)
    play = visible_buttons("playButton")[0]
    QTest.mouseClick(play, Qt.MouseButton.LeftButton)
    player = panel().player
    samples = []
    started = time.monotonic()
    shot = False
    while time.monotonic() - started < 8:
        pump(250)
        samples.append(
            {
                "t": round(time.monotonic() - started, 2),
                "state": player.state,
                "position": player.position(),
                "duration": player.duration(),
                "mediaStatus": player.media_status(),
            }
        )
        if not shot and player.position() > 300:
            save_dock("audio")
            shot = True
        if player.media_status() == "EndOfMedia":
            break
    if not shot:
        save_dock("audio")
    report["audio"] = {
        "file": player.path.name if player.path else None,
        "samples": samples,
        "message": panel().action_message.text(),
    }

    step("several selected")
    layer = observations_layer()
    layer.selectByIds([f.id() for f in layer.getFeatures()][:3])
    report["hintSeveral"] = panel().message.text()

    step("download all media")
    dock.download_button.click()
    wait_until(lambda: controller.download(PROJECT_ID) is None, "the download", 180)
    report["downloadAll"] = dock.download_message.text()
    save_dock("download")
    report["projectRows"] = [
        [dock.projects.topLevelItem(i).text(0), dock.projects.topLevelItem(i).text(1)]
        for i in range(dock.projects.topLevelItemCount())
    ]


def main() -> None:
    try:
        report["pluginLoaded"] = PLUGIN in plugins
        if not report["pluginLoaded"]:
            raise RuntimeError("QGIS did not load the comapeo_qgis plugin")
        prepare()
        run()
    except Exception:
        report["failures"].append(traceback.format_exc())
    finally:
        try:
            QDesktopServices.unsetUrlHandler("file")
            path = OUT / f"{PREFIX}-media.json"
            temp = path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(scrub(report), indent=2, default=str), encoding="utf-8"
            )
            temp.replace(path)
        finally:
            QgsProject.instance().setDirty(False)
            iface.actionExit().trigger()


QTimer.singleShot(1000, main)
