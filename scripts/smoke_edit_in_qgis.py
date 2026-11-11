"""Runs inside a real QGIS window, started by `scripts/smoke_e2e.py --edit`.

Edits the observations layer with QGIS's own Toggle Editing and Save Layer Edits
actions, with the attribute table open: a field, a category, an attempted move
(undone at once), a delete, and a save of an observation the driver changes in
CoMapeo meanwhile. Also reads which rows the open table shows greyed.
Saves screenshots and a JSON report, then quits. COMAPEO_SMOKE_MEDIA holds the
document ids the driver picked.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from qgis.core import Qgis, QgsApplication, QgsGeometry, QgsPointXY, QgsProject
from qgis.gui import QgsAttributeTableModel
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QAbstractTableModel, QEventLoop, Qt, QTimer
from qgis.utils import iface, plugins

PLUGIN = "comapeo_qgis"
OUT = Path(os.environ.get("COMAPEO_SMOKE_OUT", "."))
WORK = Path(os.environ.get("COMAPEO_SMOKE_WORK", "."))
PREFIX = os.environ.get("COMAPEO_SMOKE_PREFIX", "edit")
PROJECT_ID = os.environ.get("COMAPEO_SMOKE_PROJECT", "")
TARGETS = json.loads(os.environ.get("COMAPEO_SMOKE_MEDIA", "{}"))
HOME = str(Path.home())

report: dict[str, Any] = {
    "mode": "edit",
    "qgisVersion": Qgis.version(),
    "steps": [],
    "exceptions": [],
    "messageLog": [],
    "failures": [],
    "images": [],
    "saves": {},
    "shown": [],
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
    if tag in ("Python error", "CoMapeo") or level == Qgis.MessageLevel.Critical:
        report["messageLog"].append(
            {"tag": tag, "level": getattr(level, "name", str(level)), "text": message}
        )


sys.excepthook = _hook
QgsApplication.messageLog().messageReceived.connect(_on_message)


def plugin() -> Any:
    return plugins.get(PLUGIN)


def save_window(name: str) -> None:
    pump(500)
    path = OUT / f"{PREFIX}-{name}.png"
    iface.mainWindow().grab().save(str(path))
    report["images"].append(path.name)


TABLE: list[Any] = []


def save_table(name: str) -> None:
    if not TABLE:
        return
    pump(500)
    path = OUT / f"{PREFIX}-{name}.png"
    TABLE[0].grab().save(str(path))
    report["images"].append(path.name)


def bar_texts() -> list[str]:
    return [item.text() for item in iface.messageBar().items()]


def clear_bar() -> None:
    iface.messageBar().clearWidgets()
    pump(100)


def layer() -> Any:
    from comapeo_qgis.qgis_tier.layers import comapeo_ids, comapeo_layers

    for found in comapeo_layers(QgsProject.instance()):
        if comapeo_ids(found)[1] == "observations":
            return found
    return None


def fid(doc_id: str) -> int:
    return next(f.id() for f in layer().getFeatures() if f["id"] == doc_id)


def row(doc_id: str) -> Any:
    return next(f for f in layer().getFeatures() if f["id"] == doc_id)


def category_field() -> str:
    found = layer()
    return next(
        found.fields().at(i).name()
        for i in range(found.fields().count())
        if found.attributeAlias(i) == "Category"
        and found.editorWidgetSetup(i).type() == "ValueMap"
    )


def table_rows() -> tuple[list[str], list[str]]:
    """Ids of the rows in the open attribute table, and of those shown grey."""
    if not TABLE:
        return [], []
    # QGIS's own table reaches Python as plain Qt classes.
    models = [
        sip.cast(m, QgsAttributeTableModel)
        for m in TABLE[0].findChildren(QAbstractTableModel)
        if m.metaObject().className() == "QgsAttributeTableModel"
    ]
    if not models:
        return [], []
    model = models[0]
    header = Qt.Orientation.Horizontal
    names = [
        model.headerData(c, header, Qt.ItemDataRole.DisplayRole)
        for c in range(model.columnCount())
    ]
    if "ID" not in names or "Notes" not in names:
        return [], []
    id_column, notes = names.index("ID"), names.index("Notes")
    shown, grey = [], []
    for r in range(model.rowCount()):
        doc_id = model.data(model.index(r, id_column), Qt.ItemDataRole.DisplayRole)
        brush = model.data(model.index(r, notes), Qt.ItemDataRole.ForegroundRole)
        colour = brush.color() if hasattr(brush, "color") else brush
        shown.append(doc_id)
        if colour is not None and colour.isValid() and colour.name() == "#8a8a8a":
            grey.append(doc_id)
    return shown, grey


def toggle_editing() -> None:
    iface.setActiveLayer(layer())
    iface.actionToggleEditing().trigger()
    pump(200)


def save_edits(name: str) -> list[str]:
    """Save Layer Edits, as the toolbar button does; what the bar then says."""
    clear_bar()
    iface.setActiveLayer(layer())
    iface.actionSaveActiveLayerEdits().trigger()
    pump(600)
    texts = bar_texts()
    report["saves"][name] = {
        "bar": texts,
        "stillModified": layer().isModified(),
        "editable": layer().isEditable(),
    }
    return texts


def ask_driver(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    temp = WORK / f"{name}.tmp"
    temp.write_text(json.dumps(payload), encoding="utf-8")
    temp.replace(WORK / f"{name}.json")
    done = WORK / f"{name}-done.json"
    wait_until(done.exists, f"the driver to do {name}", 60)
    return json.loads(done.read_text(encoding="utf-8"))


def prepare() -> None:
    window = iface.mainWindow()
    window.showNormal()
    window.resize(1600, 1000)
    window.move(0, 0)
    window.raise_()
    window.activateWindow()
    pump(300)


def run() -> None:
    controller = plugin().controller
    controller.messageShown.connect(
        lambda _level, text, details: report["shown"].append(f"{text}\n{details}")
    )
    step("connecting")
    wait_until(lambda: controller.connected, "the plugin to connect")
    wait_until(lambda: controller.available_layers(PROJECT_ID), "the layers")
    controller.add_project(PROJECT_ID)

    def synced() -> bool:
        status = controller.project_status(PROJECT_ID)
        return status.connected and not status.busy and status.last_refresh is not None

    wait_until(synced, "the first fetch", 120)
    wait_until(lambda: not layer().readOnly(), "the layer to be editable")
    iface.setActiveLayer(layer())
    iface.zoomToActiveLayer()
    table = iface.showAttributeTable(layer())
    report["attributeTableOpen"] = table is not None
    if table is not None:
        table.resize(1400, 500)
        TABLE.append(table)
    pump(500)
    report["tableRows"], report["greyRows"] = table_rows()
    ask_driver("role", {})
    wait_until(
        lambda: all(f["comapeo_editable"] for f in layer().getFeatures()),
        "every row to be editable as coordinator",
    )
    pump(500)
    report["greyAfterRole"] = table_rows()[1]

    step("field")
    toggle_editing()
    report["editingStarted"] = layer().isEditable()
    notes = layer().fields().indexOf("notes")
    layer().changeAttributeValue(fid(TARGETS["field"]), notes, "Checked from QGIS")
    save_window("field-before-save")
    save_table("table-while-editing")
    save_edits("field")
    report["fieldAfter"] = row(TARGETS["field"])["notes"]
    save_window("field-saved")

    step("category")
    index = layer().fields().indexOf(category_field())
    layer().changeAttributeValue(fid(TARGETS["category"]), index, TARGETS["plant"])
    save_edits("category")
    report["categoryAfter"] = row(TARGETS["category"])["category"]
    save_window("category-saved")

    step("move")
    before = row(TARGETS["move"]).geometry().asPoint()
    report["moveBefore"] = [before.x(), before.y()]
    layer().changeGeometry(
        fid(TARGETS["move"]),
        QgsGeometry.fromPointXY(QgsPointXY(before.x() + 0.05, before.y() + 0.05)),
    )
    pump(300)
    after = row(TARGETS["move"]).geometry().asPoint()
    report["moveAfter"] = [after.x(), after.y()]
    report["moveLeftUnsaved"] = layer().isModified()
    save_window("move-undone")

    step("delete")
    layer().deleteFeature(fid(TARGETS["delete"]))
    save_edits("delete")
    report["deletedStillShown"] = any(
        f["id"] == TARGETS["delete"] for f in layer().getFeatures()
    )

    step("stale")
    layer().changeAttributeValue(
        fid(TARGETS["stale"]), notes, "Edited in QGIS, too late"
    )
    ask_driver("stale", {"docId": TARGETS["stale"]})
    pump(1500)
    report["staleHeldNotes"] = row(TARGETS["stale"])["notes"]
    save_edits("stale")
    report["staleAfter"] = row(TARGETS["stale"])["notes"]
    save_window("stale-refused")
    save_table("table-after-stale")

    step("stop editing")
    toggle_editing()
    report["editingStopped"] = not layer().isEditable()


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
            path = OUT / f"{PREFIX}-edit.json"
            temp = path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(scrub(report), indent=2, default=str), encoding="utf-8"
            )
            temp.replace(path)
        finally:
            QgsProject.instance().setDirty(False)
            if layer() is not None and layer().isEditable():
                layer().rollBack()
            iface.actionExit().trigger()


QTimer.singleShot(1000, main)
