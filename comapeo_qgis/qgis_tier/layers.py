"""Map layers backed by the cache, and the properties that let the plugin re-attach."""

from __future__ import annotations

import re

from qgis.core import (
    QgsDataProvider,
    QgsEditorWidgetSetup,
    QgsLayerTreeGroup,
    QgsMapLayer,
    QgsProject,
    QgsProviderRegistry,
    QgsVectorLayer,
)

from ..core.model import KIND_MAP, KIND_STRING_LIST, ColumnSpec
from .cache import ProjectCache, TableInfo, cache_file_name

PROP_PROJECT = "comapeo/projectId"
PROP_LAYER = "comapeo/layerId"
GROUP_PROP_PROJECT = "comapeo/projectId"

PRIVATE_ROLES = ("doc_id", "version", "category_id", "attachments")


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


def configure(layer: QgsVectorLayer, project_id: str, info: TableInfo) -> None:
    """Mark as ours, read-only, with labels from the definition, option labels for
    select fields and private columns hidden. Safe to repeat; never touches the
    renderer."""
    layer.setCustomProperty(PROP_PROJECT, project_id)
    layer.setCustomProperty(PROP_LAYER, info.layer_id)
    layer.setReadOnly(True)
    fields = layer.fields()
    for column in info.definition.columns:
        index = fields.indexOf(column.name)
        if index >= 0:
            layer.setFieldAlias(index, column.label)
            setup = _value_labels(column)
            if setup is not None:
                layer.setEditorWidgetSetup(index, setup)
            elif layer.editorWidgetSetup(index).type() == "ValueMap":
                layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup())
    hidden = {info.field(role) for role in PRIVATE_ROLES}
    config = layer.attributeTableConfig()
    config.update(fields)
    columns = config.columns()
    for column in columns:
        if column.name in hidden:
            column.hidden = True
    config.setColumns(columns)
    layer.setAttributeTableConfig(config)
    for name in hidden:
        index = fields.indexOf(name)
        if index >= 0:
            layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup("Hidden", {}))


def _value_labels(column: ColumnSpec) -> QgsEditorWidgetSetup | None:
    # QGIS's value map shows labels for single values only, not for lists.
    if column.kind in (KIND_STRING_LIST, KIND_MAP):
        return None
    pairs = [{o.label: o.value} for o in column.options if o.value is not None]
    if not pairs:
        return None
    return QgsEditorWidgetSetup("ValueMap", {"map": pairs})


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
