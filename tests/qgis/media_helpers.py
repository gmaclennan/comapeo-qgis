"""Shared pieces for the media panel tests."""

from __future__ import annotations

from pathlib import Path

from qgis.core import QgsApplication
from qgis.PyQt.QtWidgets import QPushButton

from tests.qgis.helpers import rows


def media_root() -> Path:
    return Path(QgsApplication.qgisSettingsDirPath()) / "comapeo" / "media"


def media_files() -> list[Path]:
    root = media_root()
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file())


def select(layer, doc_ids) -> None:
    found = rows(layer)
    layer.selectByIds([found[d].id() for d in doc_ids])


def photo_items(panel) -> list:
    return [panel.photos.item(i) for i in range(panel.photos.count())]


def buttons(widget, name: str) -> list[QPushButton]:
    return [b for b in widget.findChildren(QPushButton, name) if not b.isHidden()]
