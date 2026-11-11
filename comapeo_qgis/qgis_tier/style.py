"""Default CoMapeo style: one renderer category per CoMapeo category.

Categories are keyed on the private category id column, not the category
name, because names follow the interface language and can be renamed; the
name is only the legend label. Icons are embedded as `base64:` paths so a
saved QGIS project carries them.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence

from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsFeatureRenderer,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsRasterMarkerSymbolLayer,
    QgsRendererCategory,
    QgsSimpleMarkerSymbolLayer,
    QgsSvgMarkerSymbolLayer,
    QgsSymbol,
    QgsVectorLayer,
)
from qgis.PyQt.QtGui import QColor

from ..core.model import GEOMETRY_LINESTRING, Category
from .cache import TableInfo

FALLBACK_COLOR = "#7a7a7a"
FALLBACK_LABEL = "Other"
MARKER_SIZE = 6.0
ICON_SIZE = 4.0
LINE_WIDTH = 0.8


def _color(value: str | None) -> QColor:
    color = QColor(value) if value else QColor()
    return color if color.isValid() else QColor(FALLBACK_COLOR)


def icon_path(category: Category) -> str | None:
    if category.icon is None:
        return None
    return "base64:" + base64.b64encode(category.icon.data).decode("ascii")


def marker_symbol(category: Category | None) -> QgsMarkerSymbol:
    color = _color(category.color if category else None)
    symbol = QgsMarkerSymbol()
    symbol.deleteSymbolLayer(0)
    background = QgsSimpleMarkerSymbolLayer()
    background.setSize(MARKER_SIZE)
    icon = icon_path(category) if category else None
    if category is None or category.icon is None or icon is None:
        background.setColor(color)
        background.setStrokeColor(QColor("white"))
        background.setStrokeWidth(0.4)
        symbol.appendSymbolLayer(background)
        return symbol
    background.setColor(QColor("white"))
    background.setStrokeColor(color)
    background.setStrokeWidth(0.6)
    symbol.appendSymbolLayer(background)
    if category.icon.mime_type == "image/svg+xml":
        layer = QgsSvgMarkerSymbolLayer(icon, ICON_SIZE)
    else:
        layer = QgsRasterMarkerSymbolLayer(icon)
        layer.setSize(ICON_SIZE)
    symbol.appendSymbolLayer(layer)
    return symbol


def line_symbol(category: Category | None) -> QgsLineSymbol:
    symbol = QgsLineSymbol()
    symbol.setColor(_color(category.color if category else None))
    symbol.setWidth(LINE_WIDTH)
    return symbol


def _symbol(geometry_type: str, category: Category | None) -> QgsSymbol:
    if geometry_type == GEOMETRY_LINESTRING:
        return line_symbol(category)
    return marker_symbol(category)


def _category(geometry_type: str, category: Category) -> QgsRendererCategory:
    return QgsRendererCategory(
        category.category_id, _symbol(geometry_type, category), category.name
    )


def default_renderer(
    info: TableInfo, categories: Sequence[Category]
) -> QgsFeatureRenderer:
    geometry_type = info.definition.geometry_type
    entries = [_category(geometry_type, c) for c in categories]
    # A category whose value is NULL catches NULL and every unlisted value.
    entries.append(
        QgsRendererCategory(None, _symbol(geometry_type, None), FALLBACK_LABEL)
    )
    return QgsCategorizedSymbolRenderer(info.field("category_id"), entries)


def listed_categories(
    layer: QgsVectorLayer, info: TableInfo, categories: Sequence[Category]
) -> list[Category]:
    """The categories a layer's legend lists: all of them for points, only those
    in use for lines."""
    if info.definition.geometry_type != GEOMETRY_LINESTRING:
        return list(categories)
    # Most categories are for observations, and the protocol does not say which.
    index = layer.fields().indexOf(info.field("category_id"))
    used = set(layer.uniqueValues(index)) if index >= 0 else set()
    return [c for c in categories if c.category_id in used]


def apply_default_style(
    layer: QgsVectorLayer, info: TableInfo, categories: Sequence[Category]
) -> None:
    listed = listed_categories(layer, info, categories)
    layer.setRenderer(default_renderer(info, listed))
    layer.triggerRepaint()


def append_new_categories(
    layer: QgsVectorLayer, info: TableInfo, categories: Sequence[Category]
) -> int:
    """Add renderer entries for categories it lacks; never touches existing ones."""
    renderer = layer.renderer()
    if not isinstance(renderer, QgsCategorizedSymbolRenderer):
        return 0
    if renderer.classAttribute() != info.field("category_id"):
        return 0
    present = {c.value() for c in renderer.categories()}
    geometry_type = info.definition.geometry_type
    added = 0
    for category in listed_categories(layer, info, categories):
        if category.category_id not in present:
            renderer.addCategory(_category(geometry_type, category))
            added += 1
    if added:
        layer.triggerRepaint()
    return added


def follow_category_field(
    layer: QgsVectorLayer, old_field: str, info: TableInfo
) -> None:
    """After a rebuild renamed the private category column, keep the style on it."""
    renderer = layer.renderer()
    new_field = info.field("category_id")
    if (
        isinstance(renderer, QgsCategorizedSymbolRenderer)
        and old_field != new_field
        and renderer.classAttribute() == old_field
    ):
        renderer.setClassAttribute(new_field)
