"""Runs inside a real QGIS window, started by scripts/smoke_e2e.py with --code.

Settings come from COMAPEO_SMOKE_* environment variables. Writes a JSON report
and screenshots, then quits QGIS. Never raises: every failure ends in the report.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsCoordinateTransform,
    QgsLayerTreeLayer,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.gui import QgsBrowserTreeView
from qgis.PyQt.QtCore import QEventLoop, QPoint, Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QDockWidget
from qgis.utils import iface, plugins

PLUGIN = "comapeo_qgis"
WORK = Path(os.environ.get("COMAPEO_SMOKE_WORK", "."))
OUT = Path(os.environ.get("COMAPEO_SMOKE_OUT", "."))
MODE = os.environ.get("COMAPEO_SMOKE_MODE", "live")
PREFIX = os.environ.get("COMAPEO_SMOKE_PREFIX", "smoke")
PROJECT_ID = os.environ.get("COMAPEO_SMOKE_PROJECT", "")
TIMEOUT = float(os.environ.get("COMAPEO_SMOKE_STEP_TIMEOUT", "90"))
HOME = str(Path.home())

report: dict[str, Any] = {
    "mode": MODE,
    "qgisVersion": Qgis.version(),
    "qtPlatform": QgsApplication.platformName(),
    "steps": [],
    "exceptions": [],
    "messageLog": [],
    "failures": [],
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


def fail(text: str) -> None:
    report["failures"].append(text)


def write_json(path: Path, data: Any) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(scrub(data), indent=2, default=str), encoding="utf-8")
    temp.replace(path)


def pump(milliseconds: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def wait_until(predicate: Any, what: str, timeout: float = TIMEOUT) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out after {timeout:.0f} s waiting for {what}")
        pump(50)


# Exceptions and log messages


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


# What the plugin shows


def plugin() -> Any:
    return plugins.get(PLUGIN)


def comapeo_layers() -> list[QgsVectorLayer]:
    from comapeo_qgis.qgis_tier.layers import comapeo_layers as owned

    return owned(QgsProject.instance())


def layer_by_id(layer_id: str) -> QgsVectorLayer | None:
    from comapeo_qgis.qgis_tier.layers import comapeo_ids

    for layer in comapeo_layers():
        ids = comapeo_ids(layer)
        if ids and ids[1] == layer_id:
            return layer
    return None


def describe_layer(layer: QgsVectorLayer) -> dict[str, Any]:
    from comapeo_qgis.qgis_tier.layers import comapeo_ids

    renderer = layer.renderer()
    info: dict[str, Any] = {
        "name": layer.name(),
        "layerId": comapeo_ids(layer)[1],
        "valid": layer.isValid(),
        "readOnly": layer.readOnly(),
        "provider": layer.providerType(),
        "geometryType": layer.geometryType().name,
        "featureCount": layer.featureCount(),
        "featuresIterated": sum(1 for _ in layer.getFeatures()),
        "renderer": renderer.type() if renderer else None,
    }
    if isinstance(renderer, QgsCategorizedSymbolRenderer):
        field = renderer.classAttribute()
        values = {c.value() for c in renderer.categories()}
        unmatched = 0
        per_category: dict[str, int] = {}
        for feature in layer.getFeatures():
            value = feature[field]
            if value in values:
                per_category[value] = per_category.get(value, 0) + 1
            else:
                unmatched += 1
        categories = []
        for category in renderer.categories():
            symbol = category.symbol()
            symbol_layers = [
                symbol.symbolLayer(i) for i in range(symbol.symbolLayerCount())
            ]
            categories.append(
                {
                    "label": category.label(),
                    "features": per_category.get(category.value(), 0),
                    "symbolLayers": [s.layerType() for s in symbol_layers],
                    "embeddedIcon": any(
                        getattr(s, "path", lambda: "")().startswith("base64:")
                        for s in symbol_layers
                    ),
                }
            )
        info.update(
            {
                "classAttribute": field,
                "categoryCount": len(categories),
                "categoriesWithIcon": sum(1 for c in categories if c["embeddedIcon"]),
                "featuresInNoCategory": unmatched,
                "categories": categories,
            }
        )
    return info


def status_report() -> dict[str, Any]:
    p = plugin()
    controller = p.controller
    result: dict[str, Any] = {
        "panelStatus": p.dock.status_label.text() if p.dock else None,
        "servers": [
            {"build": s.build_type, "state": s.state.value, "message": s.message}
            for s in controller.server_statuses
        ],
    }
    if PROJECT_ID:
        status = controller.project_status(PROJECT_ID)
        result["project"] = {
            "name": status.name,
            "connected": status.connected,
            "stale": status.stale,
            "busy": status.busy,
            "describe": status.describe(),
        }
    view = iface.layerTreeView()
    badges = {}
    for layer in comapeo_layers():
        node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
        if node is not None:
            badges[layer.name()] = [i.toolTip() for i in view.indicators(node)]
    result["layerTreeBadges"] = badges
    return result


# Window and screenshots


def prepare_window() -> None:
    window = iface.mainWindow()
    window.showNormal()
    window.resize(1600, 1000)
    window.move(0, 0)
    window.raise_()
    window.activateWindow()
    p = plugin()
    if p is not None and p.dock is not None:
        p.dock.show()
        p.dock.raise_()
    docks = {d.objectName(): d for d in window.findChildren(QDockWidget)}
    left = [docks[n] for n in ("Browser", "Layers") if n in docks]
    if left:
        window.resizeDocks(left, [420] * len(left), Qt.Orientation.Horizontal)
        window.resizeDocks(left, [330, 600][: len(left)], Qt.Orientation.Vertical)
    if p is not None and p.dock is not None:
        window.resizeDocks([p.dock], [420], Qt.Orientation.Horizontal)
    pump(300)


def browser_view() -> QgsBrowserTreeView | None:
    views = iface.mainWindow().findChildren(QgsBrowserTreeView)
    report["browserViews"] = [
        f"{v.objectName()} visible={v.isVisible()}" for v in views
    ]
    visible = [v for v in views if v.isVisible()]
    return (visible or views or [None])[0]


def painted_fraction(path: Path) -> float:
    from qgis.PyQt.QtGui import QImage

    image = QImage(str(path))
    background = QColor(iface.mapCanvas().canvasColor()).rgb()
    total = painted = 0
    for y in range(0, image.height(), 4):
        for x in range(0, image.width(), 4):
            total += 1
            if image.pixel(x, y) != background:
                painted += 1
    return painted / total if total else 0.0


def render_canvas(extent: QgsRectangle) -> None:
    canvas = iface.mapCanvas()
    done = []
    canvas.mapCanvasRefreshed.connect(lambda: done.append(True))
    canvas.setExtent(extent)
    canvas.refresh()
    wait_until(lambda: done, "the map canvas to render", 60)
    canvas.waitWhileRendering()
    pump(300)


def save_canvas(name: str) -> str:
    path = OUT / f"{PREFIX}-{name}.png"
    iface.mapCanvas().saveAsImage(str(path))
    for extra in (path.with_suffix(".pgw"), Path(str(path) + ".aux.xml")):
        if extra.exists():
            extra.unlink()
    report.setdefault("canvasPaintedFraction", {})[name] = round(
        painted_fraction(path), 4
    )
    return path.name


def save_window(name: str) -> str:
    path = OUT / f"{PREFIX}-{name}.png"
    iface.mainWindow().grab().save(str(path))
    return path.name


def full_extent() -> QgsRectangle:
    canvas = iface.mapCanvas()
    extent = QgsRectangle()
    extent.setNull()
    for layer in comapeo_layers():
        if layer.featureCount() == 0:
            continue
        transform = QgsCoordinateTransform(
            layer.crs(), canvas.mapSettings().destinationCrs(), QgsProject.instance()
        )
        extent.combineExtentWith(transform.transformBoundingBox(layer.extent()))
    extent.scale(1.08)
    return extent


def close_extent(observations: QgsVectorLayer, full: QgsRectangle) -> QgsRectangle:
    feature = next(observations.getFeatures())
    point = feature.geometry().asPoint()
    zoomed = QgsRectangle(full)
    zoomed.scale(0.12, point)
    return zoomed


def show_legends() -> None:
    root = QgsProject.instance().layerTreeRoot()
    for group in root.findGroups(True):
        group.setExpanded(True)
    # Only the observations legend fits on screen; tracks list the same categories.
    observations = layer_by_id("observations")
    for node in root.findLayers():
        if isinstance(node, QgsLayerTreeLayer):
            node.setExpanded(node.layer() is observations)
    iface.messageBar().clearWidgets()
    pump(200)


# Live run: add layers through the Browser


def find_item(model: Any, parent: Any, predicate: Any) -> Any:
    def search() -> Any:
        if model.canFetchMore(parent):
            model.fetchMore(parent)
        for row in range(model.rowCount(parent)):
            index = model.index(row, 0, parent)
            item = model.dataItem(index)
            if item is not None and predicate(item):
                return index
        return None

    return search


def browser_layer_items() -> list[Any]:
    """Index of each CoMapeo layer item of the project, through the GUI's model."""
    model = iface.browserModel()
    root = wait_until(
        find_item(model, model.index(-1, -1), lambda i: i.path() == "comapeo:"),
        "the CoMapeo Browser entry",
    )
    project = wait_until(
        lambda: _project_index(model, root), "the project in the Browser"
    )

    def layer_rows() -> list[Any] | None:
        rows = _children(model, project)
        return rows if len(rows) >= 2 else None

    layers = wait_until(layer_rows, "the project's layers in the Browser")
    report["browserItemClasses"] = sorted(
        {type(model.dataItem(i)).__name__ for i in [root, project, *layers]}
    )
    return layers


def _children(model: Any, parent: Any) -> list[Any]:
    if model.canFetchMore(parent):
        model.fetchMore(parent)
    return [model.index(r, 0, parent) for r in range(model.rowCount(parent))]


def _project_index(model: Any, root: Any) -> Any:
    for server in _children(model, root):
        for project in _children(model, server):
            item = model.dataItem(project)
            if item is not None and item.path().endswith("/" + PROJECT_ID):
                return project
    return None


def index_for_path(model: Any, path: str) -> Any:
    """Indexes go stale whenever the plugin refreshes the Browser; look up afresh."""
    pending = [model.index(-1, -1)]
    while pending:
        parent = pending.pop()
        for row in range(model.rowCount(parent)):
            index = model.index(row, 0, parent)
            item = model.dataItem(index)
            if item is None:
                continue
            if item.path() == path:
                return index
            if path.startswith(item.path()):
                pending.append(index)
    return None


def double_click(view: QgsBrowserTreeView, path: str) -> dict[str, Any]:
    model = iface.browserModel()
    proxy = view.model()
    index = proxy.mapFromSource(index_for_path(model, path))
    view.scrollTo(index)
    pump(200)
    index = proxy.mapFromSource(index_for_path(model, path))
    rect = view.visualRect(index)
    position = QPoint(rect.left() + 40, rect.center().y())
    hit = proxy.mapToSource(view.indexAt(position))
    hit_item = model.dataItem(hit) if hit.isValid() else None
    viewport = view.viewport()
    button = Qt.MouseButton.LeftButton
    QTest.mousePress(viewport, button, pos=position)
    QTest.mouseRelease(viewport, button, pos=position)
    QTest.mouseDClick(viewport, button, pos=position)
    QTest.mouseRelease(viewport, button, pos=position)
    return {
        "rect": [rect.x(), rect.y(), rect.width(), rect.height()],
        "itemUnderPointer": hit_item.name() if hit_item is not None else None,
    }


def add_layers_through_browser() -> None:
    view = browser_view()
    if view is None:
        raise RuntimeError("the Browser panel has no tree view")
    model = iface.browserModel()
    items = [model.dataItem(i) for i in browser_layer_items()]
    paths = [item.path() for item in items]
    names = [item.name() for item in items]
    report["browserLayers"] = names
    view.expandPath(paths[0])
    pump(300)
    report["addedBy"] = {}
    report["clicks"] = {}
    for name, path in zip(names, paths):
        before = len(comapeo_layers())
        report["clicks"][name] = double_click(view, path)
        try:
            wait_until(lambda b=before: len(comapeo_layers()) > b, "a layer", 5)
            report["addedBy"][name] = "mouse double-click in the Browser"
            continue
        except TimeoutError:
            pass
        # Same slot QGIS runs on a double-click, if the synthetic one missed.
        index = proxy_index(view, path)
        view.doubleClicked.emit(index)
        try:
            wait_until(lambda b=before: len(comapeo_layers()) > b, "a layer", 5)
            report["addedBy"][name] = "Browser view doubleClicked signal"
        except TimeoutError:
            model.dataItem(index_for_path(model, path)).handleDoubleClick()
            report["addedBy"][name] = "LayerItem.handleDoubleClick()"
    wait_until(lambda: len(comapeo_layers()) >= len(paths), "both layers on the map")


def _expand_to(view: QgsBrowserTreeView, path: str) -> bool:
    model = iface.browserModel()
    root = index_for_path(model, "comapeo:")
    for index in [root, index_for_path(model, "comapeo:/cached")]:
        if index is not None and model.canFetchMore(index):
            model.fetchMore(index)
    target = index_for_path(model, path)
    if target is None:
        return False
    view.expandPath(path)
    view.scrollTo(view.model().mapFromSource(target))
    report["browserShows"] = model.dataItem(target).name()
    return True


def proxy_index(view: QgsBrowserTreeView, path: str) -> Any:
    return view.model().mapFromSource(index_for_path(iface.browserModel(), path))


def synced() -> bool:
    status = plugin().controller.project_status(PROJECT_ID)
    return status.connected and not status.busy and status.last_refresh is not None


def run_live() -> None:
    step("waiting for the plugin to connect")
    controller = plugin().controller
    wait_until(lambda: controller.connected, "the plugin to connect")
    wait_until(
        lambda: len(controller.available_layers(PROJECT_ID)) >= 2,
        "the project's layers",
    )
    step("adding layers through the Browser")
    add_layers_through_browser()
    wait_until(synced, "the first fetch to finish", 120)
    pump(500)
    show_legends()
    report["layers"] = [describe_layer(layer) for layer in comapeo_layers()]
    report["status"] = status_report()

    step("rendering")
    full = full_extent()
    render_canvas(full)
    report["images"] = [save_canvas("canvas")]
    report["images"].append(save_window("window"))
    observations = layer_by_id("observations")
    if observations is not None and observations.featureCount():
        render_canvas(close_extent(observations, full))
        report["images"].append(save_canvas("canvas-close"))
    tracks = layer_by_id("tracks")
    if observations is not None and tracks is not None and tracks.featureCount():
        root = QgsProject.instance().layerTreeRoot()
        node = root.findLayer(observations.id())
        node.setItemVisibilityChecked(False)
        extent = QgsRectangle(tracks.extent())
        extent.scale(1.15)
        render_canvas(extent)
        report["images"].append(save_canvas("canvas-tracks"))
        node.setItemVisibilityChecked(True)
    render_canvas(full)

    project_file = WORK / "smoke.qgz"
    report["projectSaved"] = QgsProject.instance().write(str(project_file))
    write_json(WORK / "phase1.json", report)

    step("waiting for the driver to create an observation")
    mutated_file = WORK / "mutated.json"
    wait_until(mutated_file.exists, "the driver's mutation", 120)
    doc_id = json.loads(mutated_file.read_text(encoding="utf-8"))["docId"]
    report["createdDocId"] = doc_id
    before = observations.featureCount() if observations else 0
    started = time.monotonic()

    def arrived() -> bool:
        layer = layer_by_id("observations")
        info = plugin().controller.cache(PROJECT_ID).table("observations")
        if layer is None or info is None:
            return False
        expression = f"\"{info.field('doc_id')}\" = '{doc_id}'"
        return any(True for _ in layer.getFeatures(expression))

    wait_until(arrived, "the created observation on the map", 60)
    report["liveUpdateSeconds"] = round(time.monotonic() - started, 2)
    observations = layer_by_id("observations")
    report["observationsBefore"] = before
    report["observationsAfter"] = observations.featureCount() if observations else 0
    report["layersAfterUpdate"] = [describe_layer(layer) for layer in comapeo_layers()]
    report["statusAfterUpdate"] = status_report()


# Offline run: reopen the saved project with the app closed


def run_offline() -> None:
    step("waiting for the saved project's layers")
    wait_until(lambda: len(comapeo_layers()) >= 2, "the saved CoMapeo layers")
    indicators = plugin().indicators
    wait_until(
        lambda: indicators is not None and indicators.stale_layers(),
        "the stale badges",
    )
    show_legends()
    view = browser_view()
    path = f"comapeo:/cached/{PROJECT_ID}"
    if view is not None:
        wait_until(
            lambda: _expand_to(view, path), "the cached project in the Browser", 20
        )
    dock = plugin().dock
    for row in range(dock.projects.topLevelItemCount()):
        item = dock.projects.topLevelItem(row)
        if item.data(0, Qt.ItemDataRole.UserRole) == PROJECT_ID:
            item.setSelected(True)
    report["layers"] = [describe_layer(layer) for layer in comapeo_layers()]
    report["status"] = status_report()
    render_canvas(full_extent())
    report["images"] = [save_canvas("offline-canvas"), save_window("offline-window")]


def main() -> None:
    try:
        report["pluginLoaded"] = PLUGIN in plugins
        if not report["pluginLoaded"]:
            raise RuntimeError("QGIS did not load the comapeo_qgis plugin")
        prepare_window()
        if MODE == "offline":
            run_offline()
        else:
            run_live()
    except Exception:
        fail(traceback.format_exc())
    finally:
        try:
            report["settingsFile"] = QgsApplication.qgisSettingsDirPath()
            write_json(OUT / f"{PREFIX}-{MODE}.json", report)
            (WORK / f"done-{MODE}").write_text("1", encoding="utf-8")
        finally:
            # As a user quits; QgsApplication.exit() here crashed QGIS during teardown.
            QgsProject.instance().setDirty(False)
            iface.actionExit().trigger()


QTimer.singleShot(1000, main)
