"""Map layers backed by the cache, and the properties that let the plugin re-attach."""

from __future__ import annotations

import re
from collections.abc import Sequence

from qgis.core import (
    QgsConditionalStyle,
    QgsDataProvider,
    QgsEditorWidgetSetup,
    QgsLayerTreeGroup,
    QgsMapLayer,
    QgsProject,
    QgsProviderRegistry,
    QgsValueMapFieldFormatter,
    QgsVectorLayer,
)
from qgis.gui import QgsDualView
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QApplication

from ..core.editing import OP_CATEGORY, column_offered
from ..core.model import (
    KIND_MAP,
    KIND_STRING_LIST,
    ROLE_BUILTIN,
    Category,
    ColumnSpec,
    LayerDefinition,
)
from .cache import ProjectCache, TableInfo, cache_file_name

PROP_PROJECT = "comapeo/projectId"
PROP_LAYER = "comapeo/layerId"
GROUP_PROP_PROJECT = "comapeo/projectId"

PRIVATE_ROLES = ("doc_id", "version", "category_id", "attachments", "forks", "editable")
NO_VALUE_LABEL = "(no value)"
CATEGORY_ALIAS = "Category"
NOT_EDITABLE_STYLE = "CoMapeo: created on another device"


def comapeo_ids(layer: QgsMapLayer) -> tuple[str, str] | None:
    """(project id, layer id) of a map layer on a CoMapeo cache table, else None."""
    project_id = layer.customProperty(PROP_PROJECT)
    layer_id = layer.customProperty(PROP_LAYER)
    if not (isinstance(project_id, str) and project_id and isinstance(layer_id, str)):
        return None
    # Pasting a CoMapeo layer's style copies these properties onto any layer.
    if layer.providerType() != "ogr":
        return None
    # The file name, not the folder: the project may be from another computer.
    path = _decoded(layer.source())[0]
    if re.split(r"[\\/]", path)[-1] != cache_file_name(project_id):
        return None
    return project_id, layer_id


def comapeo_layers(
    project: QgsProject, project_id: str | None = None, layer_id: str | None = None
) -> list[QgsVectorLayer]:
    found = []
    for layer in project.mapLayers().values():
        ids = comapeo_ids(layer)
        if ids is None or not isinstance(layer, QgsVectorLayer):
            continue
        if project_id is not None and ids[0] != project_id:
            continue
        if layer_id is not None and ids[1] != layer_id:
            continue
        found.append(layer)
    return found


def configure(
    layer: QgsVectorLayer,
    project_id: str,
    info: TableInfo,
    editing: LayerDefinition | None = None,
    categories: Sequence[Category] = (),
) -> None:
    """Mark as ours, with labels from the definition, option labels for select
    fields and private columns hidden. Safe to repeat; never touches the
    renderer.

    `editing` is the layer as the connected server describes it, given only
    while the plugin lets it be edited: then the columns it may change are
    unlocked, choices get a "no value" entry and the category gets a column of
    its own. Otherwise the layer is read-only. The read-only flag cannot change
    during an edit session, so it is left alone then."""
    layer.setCustomProperty(PROP_PROJECT, project_id)
    layer.setCustomProperty(PROP_LAYER, info.layer_id)
    if not layer.isEditable():
        layer.setReadOnly(editing is None)
    live = {c.name: c for c in editing.columns} if editing is not None else {}
    fields = layer.fields()
    form = layer.editFormConfig()
    for index in range(fields.count()):
        form.setReadOnly(index, True)
    for column in info.definition.columns:
        index = fields.indexOf(column.name)
        if index < 0:
            continue
        layer.setFieldAlias(index, column.label)
        served = live.get(column.name)
        offered = editing is not None and served is not None
        offered = offered and column_offered(editing, served)
        form.setReadOnly(index, not offered)
        setup = _value_labels(served if offered else column, offered)
        if setup is not None:
            layer.setEditorWidgetSetup(index, setup)
        elif layer.editorWidgetSetup(index).type() == "ValueMap":
            layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup())
    hidden = {info.private[role] for role in PRIVATE_ROLES if role in info.private}
    category_field = info.field("category_id")
    category_index = fields.indexOf(category_field)
    geometry_type = editing.geometry_type if editing is not None else ""
    choose_category = (
        editing is not None and OP_CATEGORY in editing.edit_operations
    ) and category_index >= 0
    names = {
        c.name
        for c in info.definition.columns
        if c.role == ROLE_BUILTIN and c.name.endswith("category")
    }
    if choose_category:
        hidden.discard(category_field)
        # The chooser shows the same name; two "Category" columns would confuse.
        hidden |= names
    config = layer.attributeTableConfig()
    config.update(fields)
    columns = config.columns()
    for column in columns:
        if column.name in hidden:
            column.hidden = True
        elif column.name == category_field or column.name in names:
            column.hidden = False
    config.setColumns(columns)
    layer.setAttributeTableConfig(config)
    for name in hidden:
        index = fields.indexOf(name)
        if index >= 0:
            layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup("Hidden", {}))
    for name in names - hidden:
        index = fields.indexOf(name)
        if index >= 0 and layer.editorWidgetSetup(index).type() == "Hidden":
            layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup())
    if choose_category:
        layer.setFieldAlias(category_index, CATEGORY_ALIAS)
        offered = [c for c in categories if c.offered_for(geometry_type)]
        pairs = [{c.name or c.category_id: c.category_id} for c in offered]
        layer.setEditorWidgetSetup(
            category_index, QgsEditorWidgetSetup("ValueMap", {"map": pairs})
        )
        form.setReadOnly(category_index, False)
    elif category_index >= 0:
        layer.removeFieldAlias(category_index)
    layer.setEditFormConfig(form)
    _mark_not_editable(layer, info, editing is not None)


def _value_labels(column: ColumnSpec, editable: bool) -> QgsEditorWidgetSetup | None:
    # QGIS's value map shows labels for single values only, not for lists.
    if column.kind in (KIND_STRING_LIST, KIND_MAP):
        return None
    pairs = [{o.label: o.value} for o in column.options if o.value is not None]
    if not pairs:
        return None
    if editable:
        # Without an explicit entry the widget offers no way to clear a value.
        pairs.insert(0, {NO_VALUE_LABEL: QgsValueMapFieldFormatter.NULL_VALUE})
    return QgsEditorWidgetSetup("ValueMap", {"map": pairs})


def _mark_not_editable(layer: QgsVectorLayer, info: TableInfo, editing: bool) -> None:
    """Grey out, in the attribute table, the rows this device may not change."""
    styles = layer.conditionalStyles()
    kept = [s for s in styles.rowStyles() if s.name() != NOT_EDITABLE_STYLE]
    if editing and "editable" in info.private:
        name = info.private["editable"].replace("'", "''")
        # The attribute table does not load hidden columns, so the flag is read
        # from the full feature.
        style = QgsConditionalStyle(
            f"attribute(get_feature_by_id(@layer, $id), '{name}') IS NOT TRUE"
        )
        style.setName(NOT_EDITABLE_STYLE)
        style.setTextColor(QColor("#8a8a8a"))
        kept.append(style)
    styles.setRowStyles(kept)


def refresh_not_editable(layer: QgsVectorLayer) -> None:
    """Have open attribute tables work out the grey rows again: they keep the
    row styles they have computed across a reload, and offer no signal to clear
    them."""
    if not any(
        s.name() == NOT_EDITABLE_STYLE for s in layer.conditionalStyles().rowStyles()
    ):
        return
    for top in QApplication.topLevelWidgets():
        views = top.findChildren(QgsDualView)
        if isinstance(top, QgsDualView):
            views.append(top)
        for view in views:
            model = view.masterModel()
            shown = model.layer() if model is not None else None
            if shown is not None and shown.id() == layer.id():
                model.fieldConditionalStyleChanged(None)


def _decoded(source: str) -> tuple[str, str, str]:
    parts = QgsProviderRegistry.instance().decodeUri("ogr", source)
    return (
        str(parts.get("path") or ""),
        str(parts.get("layerName") or ""),
        str(parts.get("subset") or ""),
    )


def point_at(layer: QgsVectorLayer, cache: ProjectCache, info: TableInfo) -> None:
    """Switch a layer to `info`'s table, keeping its renderer, properties and filter.

    Also revives a layer that loaded invalid because its table did not exist."""
    uri = cache.uri(info.table)
    path, table, subset = _decoded(layer.source())
    if (path, table) != _decoded(uri)[:2] or not layer.isValid():
        subset = layer.subsetString() or subset
        layer.setDataSource(uri, layer.name(), "ogr", QgsDataProvider.ProviderOptions())
        # A filter the new table cannot evaluate is dropped rather than kept broken.
        if subset and layer.isValid() and not layer.setSubsetString(subset):
            layer.setSubsetString("")
    configure(layer, cache.project_id, info)


def create_layer(cache: ProjectCache, info: TableInfo) -> QgsVectorLayer:
    layer = QgsVectorLayer(cache.uri(info.table), info.definition.title, "ogr")
    configure(layer, cache.project_id, info)
    return layer


def project_group(project: QgsProject, project_id: str, name: str) -> QgsLayerTreeGroup:
    root = project.layerTreeRoot()
    for group in root.findGroups(True):
        if group.customProperty(GROUP_PROP_PROJECT) == project_id:
            return group
    group = root.insertGroup(0, name)
    group.setCustomProperty(GROUP_PROP_PROJECT, project_id)
    return group
