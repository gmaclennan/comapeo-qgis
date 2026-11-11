"""Neutral records for API results, independent of QGIS types.

Parsing is tolerant: unknown properties are ignored, malformed entries are
skipped or degrade to None rather than raising.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

KIND_STRING = "string"
KIND_REAL = "real"
KIND_INTEGER = "integer"
KIND_BOOLEAN = "boolean"
KIND_DATETIME = "datetime"
KIND_STRING_LIST = "string_list"
KIND_MAP = "map"

_COLUMN_KINDS = {
    "string": KIND_STRING,
    "number": KIND_REAL,
    "integer": KIND_INTEGER,
    "boolean": KIND_BOOLEAN,
    "datetime": KIND_DATETIME,
    "string_list": KIND_STRING_LIST,
    "json": KIND_MAP,
}

GEOMETRY_POINT = "Point"
GEOMETRY_LINESTRING = "LineString"
SUPPORTED_GEOMETRY_TYPES = (GEOMETRY_POINT, GEOMETRY_LINESTRING)

ROLE_BUILTIN = "builtin"
ROLE_FIELD = "field"
ROLE_TAGS = "tags"

MEDIA_VARIANTS = ("thumbnail", "preview", "original")
ICON_MIME_TYPES = ("image/svg+xml", "image/png")

# GeoPackage and QGIS integer fields are 64-bit.
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


@dataclass(frozen=True)
class Project:
    project_id: str
    name: str | None
    color: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class Option:
    value: Any
    label: str


@dataclass(frozen=True)
class ColumnSpec:
    """One attribute column. `type` is as sent; `kind` is what to store it as.

    `category_ids` is None when the column can be edited whatever the category."""

    name: str
    type: str
    kind: str
    label: str
    role: str | None
    options: tuple[Option, ...] = ()
    editable: bool = False
    category_ids: tuple[str, ...] | None = None


@dataclass(frozen=True)
class LayerDefinition:
    layer_id: str
    title: str
    doc_type: str | None
    geometry_type: str
    schema_version: str | None
    columns: tuple[ColumnSpec, ...]
    # None: the server predates editing and sent no `editable` member at all.
    editable: bool | None = None
    read_only_reason: str | None = None
    edit_operations: tuple[str, ...] = ()

    @property
    def geometry_supported(self) -> bool:
        return self.geometry_type in SUPPORTED_GEOMETRY_TYPES

    def column(self, name: str) -> ColumnSpec | None:
        return next((c for c in self.columns if c.name == name), None)


@dataclass(frozen=True)
class Geometry:
    """WGS 84 geometry. Coordinates are (x, y) or (x, y, z).

    For lines with per-vertex times, `m_values` holds seconds since the Unix
    epoch (UTC), one per vertex.
    """

    type: str
    coordinates: tuple[tuple[float, ...], ...]
    m_values: tuple[float, ...] | None = None

    @property
    def has_z(self) -> bool:
        return all(len(c) == 3 for c in self.coordinates)

    @property
    def has_m(self) -> bool:
        return self.m_values is not None

    def wkt(self) -> str:
        suffix = ("Z" if self.has_z else "") + ("M" if self.has_m else "")
        vertices = []
        for i, coord in enumerate(self.coordinates):
            values = list(coord if self.has_z else coord[:2])
            if self.m_values is not None:
                values.append(self.m_values[i])
            vertices.append(" ".join(_number(v) for v in values))
        return f"{self.type}{suffix} ({', '.join(vertices)})"


@dataclass(frozen=True)
class Attachment:
    type: str
    name: str | None
    key: str
    urls: Mapping[str, str]


@dataclass(frozen=True)
class FeatureRecord:
    id: str
    version: str
    geometry: Geometry | None
    attributes: Mapping[str, Any]
    category_id: str | None
    attachments: tuple[Attachment, ...] = ()
    forks: tuple[str, ...] = ()
    editable: bool = False

    @property
    def revision(self) -> str:
        return revision(self.version, self.forks)


def revision(version: str, forks: Sequence[str] = ()) -> str:
    """A document's state as far as an edit is concerned: a late fork changes
    `forks` and leaves `version` alone."""
    if not forks:
        return version
    return json.dumps([version, *sorted(forks)])


@dataclass(frozen=True)
class FeaturePage:
    """One `layers.getFeatures` result.

    `valid` is False when the result itself was malformed (no `features` list
    or no `nextCursor` member); such a page says nothing about what exists.
    `skipped` counts features dropped for lacking an id or version.
    """

    features: tuple[FeatureRecord, ...]
    removed: tuple[str, ...]
    next_cursor: Any
    skipped: int = 0
    valid: bool = True

    @property
    def is_last(self) -> bool:
        return self.next_cursor is None


@dataclass(frozen=True)
class Icon:
    mime_type: str
    data: bytes


@dataclass(frozen=True)
class Category:
    category_id: str
    name: str
    color: str | None = None
    icon: Icon | None = None
    # None: the server did not say, so the category may be offered anywhere.
    geometry_types: tuple[str, ...] | None = None

    def offered_for(self, geometry_type: str) -> bool:
        return self.geometry_types is None or geometry_type in self.geometry_types


def column_kind(column_type: Any) -> str:
    return _COLUMN_KINDS.get(column_type, KIND_STRING)


def parse_projects(result: Any) -> list[Project]:
    projects = []
    for item in _list(_get(result, "projects")):
        project_id = _get(item, "projectId")
        if not _nonempty_str(project_id):
            continue
        projects.append(
            Project(
                project_id,
                _str(_get(item, "name")),
                _str(_get(item, "color")),
                _str(_get(item, "description")),
            )
        )
    return projects


def parse_layers(result: Any) -> list[LayerDefinition]:
    layers = []
    seen: set[str] = set()
    for item in _list(_get(result, "layers")):
        layer_id = _get(item, "layerId")
        if not _nonempty_str(layer_id) or layer_id in seen:
            continue
        seen.add(layer_id)
        schema_version = _get(item, "schemaVersion")
        editable = _get(item, "editable") if "editable" in item else None
        layers.append(
            LayerDefinition(
                layer_id=layer_id,
                title=_str(_get(item, "title")) or layer_id,
                doc_type=_str(_get(item, "docType")),
                geometry_type=_str(_get(item, "geometryType")) or "",
                schema_version=None if schema_version is None else str(schema_version),
                columns=_parse_columns(_get(item, "columns")),
                editable=None if editable is None else editable is True,
                read_only_reason=_str(_get(item, "readOnlyReason")),
                edit_operations=_strings(_get(item, "editOperations")),
            )
        )
    return layers


def _parse_columns(value: Any) -> tuple[ColumnSpec, ...]:
    columns = []
    seen: set[str] = set()
    for item in _list(value):
        name = _get(item, "name")
        if not _nonempty_str(name) or name in seen:
            continue
        seen.add(name)
        raw_type = _get(item, "type")
        options = tuple(
            Option(_get(o, "value"), _str(_get(o, "label")) or _label(_get(o, "value")))
            for o in _list(_get(item, "options"))
            if isinstance(o, dict) and "value" in o
        )
        category_ids = _get(item, "categoryIds")
        columns.append(
            ColumnSpec(
                name=name,
                type=raw_type if isinstance(raw_type, str) else "",
                kind=column_kind(raw_type),
                label=_str(_get(item, "label")) or name,
                role=_str(_get(item, "role")),
                options=options,
                editable=_get(item, "editable") is True,
                category_ids=None
                if not isinstance(category_ids, list)
                else _strings(category_ids),
            )
        )
    return tuple(columns)


def parse_feature_page(result: Any, layer: LayerDefinition) -> FeaturePage:
    features = []
    skipped = 0
    for item in _list(_get(result, "features")):
        record = parse_feature(item, layer)
        if record is None:
            skipped += 1
        else:
            features.append(record)
    removed = tuple(r for r in _list(_get(result, "removed")) if _nonempty_str(r))
    valid = (
        isinstance(result, dict)
        and isinstance(result.get("features"), list)
        and "nextCursor" in result
    )
    return FeaturePage(
        tuple(features),
        removed,
        _get(result, "nextCursor"),
        skipped=skipped,
        valid=valid,
    )


@dataclass(frozen=True)
class LayerChanged:
    project_id: str
    layer_id: str
    ids: tuple[str, ...]


@dataclass(frozen=True)
class SchemaChanged:
    project_id: str
    layer_id: str


def parse_layer_changed(params: Any) -> LayerChanged | None:
    """`layer.changed` params; None if they cannot be acted on."""
    project_id = _get(params, "projectId")
    layer_id = _get(params, "layerId")
    ids = _get(params, "ids")
    if not (_nonempty_str(project_id) and _nonempty_str(layer_id)):
        return None
    if not isinstance(ids, list):
        return None
    unique = tuple(dict.fromkeys(i for i in ids if _nonempty_str(i)))
    return LayerChanged(project_id, layer_id, unique)


def parse_schema_changed(params: Any) -> SchemaChanged | None:
    """`layer.schemaChanged` params; None if they cannot be acted on."""
    project_id = _get(params, "projectId")
    layer_id = _get(params, "layerId")
    if not (_nonempty_str(project_id) and _nonempty_str(layer_id)):
        return None
    return SchemaChanged(project_id, layer_id)


def parse_feature(item: Any, layer: LayerDefinition) -> FeatureRecord | None:
    feature_id = _get(item, "id")
    version = _get(item, "version")
    if not _nonempty_str(feature_id) or not _nonempty_str(version):
        return None
    properties = _get(item, "properties")
    properties = properties if isinstance(properties, dict) else {}
    geometry = parse_geometry(_get(item, "geometry"), _get(item, "times"))
    if geometry is not None and geometry.type != layer.geometry_type:
        geometry = None
    return FeatureRecord(
        id=feature_id,
        version=version,
        geometry=geometry,
        attributes={
            c.name: convert_value(properties.get(c.name), c.kind) for c in layer.columns
        },
        category_id=_str(_get(item, "categoryId")),
        attachments=tuple(
            a
            for a in (_parse_attachment(x) for x in _list(_get(item, "attachments")))
            if a is not None
        ),
        forks=tuple(sorted(set(_strings(_get(item, "forks"))))),
        editable=_get(item, "editable") is True,
    )


def parse_geometry(value: Any, times: Any = None) -> Geometry | None:
    """`times` is the feature's own `times` member, parallel to line vertices."""
    geometry_type = _get(value, "type")
    coordinates = _get(value, "coordinates")
    if geometry_type == GEOMETRY_POINT:
        coord = _coordinate(coordinates)
        return None if coord is None else Geometry(GEOMETRY_POINT, (coord,))
    if geometry_type == GEOMETRY_LINESTRING:
        coords = [_coordinate(c) for c in _list(coordinates)]
        if len(coords) < 2 or any(c is None for c in coords):
            return None
        vertices = tuple(c for c in coords if c is not None)
        if len({len(c) for c in vertices}) != 1:
            vertices = tuple(c[:2] for c in vertices)
        return Geometry(GEOMETRY_LINESTRING, vertices, _m_values(times, len(vertices)))
    return None


def _m_values(times: Any, count: int) -> tuple[float, ...] | None:
    if not isinstance(times, list) or len(times) != count:
        return None
    values = []
    for t in times:
        parsed = parse_datetime(t)
        if parsed is None:
            return None
        try:
            values.append(parsed.timestamp())
        except (OverflowError, ValueError):
            return None
    return tuple(values)


def _coordinate(value: Any) -> tuple[float, ...] | None:
    if not isinstance(value, list) or len(value) < 2:
        return None
    numbers = []
    for v in value[:3]:
        number = _finite_float(v)
        if number is None:
            return None
        numbers.append(number)
    return tuple(numbers)


def _finite_float(value: Any) -> float | None:
    if not _is_number(value):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _parse_attachment(item: Any) -> Attachment | None:
    key = _get(item, "key")
    if not _nonempty_str(key):
        return None
    urls = _get(item, "urls")
    urls = (
        {k: v for k, v in urls.items() if k in MEDIA_VARIANTS and _nonempty_str(v)}
        if isinstance(urls, dict)
        else {}
    )
    return Attachment(
        type=_str(_get(item, "type")) or "",
        name=_str(_get(item, "name")),
        key=key,
        urls=urls,
    )


def attachments_from_json(text: Any) -> tuple[Attachment, ...]:
    """Attachments as the cache stores them (a JSON list), tolerant of junk."""
    if not isinstance(text, str):
        return ()
    try:
        return parse_attachments(json.loads(text))
    except ValueError:
        return ()


def parse_attachments(items: Any) -> tuple[Attachment, ...]:
    found = (_parse_attachment(item) for item in _list(items))
    return tuple(a for a in found if a is not None)


def parse_categories(result: Any) -> list[Category]:
    categories = []
    for item in _list(_get(result, "categories")):
        category_id = _get(item, "categoryId")
        if not _nonempty_str(category_id):
            continue
        geometry_types = _get(item, "geometryTypes")
        if isinstance(geometry_types, list):
            geometry_types = _strings(geometry_types)
        categories.append(
            Category(
                category_id,
                _str(_get(item, "name")) or "",
                _str(_get(item, "color")),
                _parse_icon(_get(item, "icon")),
                geometry_types if isinstance(geometry_types, tuple) else None,
            )
        )
    return categories


def _parse_icon(value: Any) -> Icon | None:
    mime_type = _get(value, "mimeType")
    data = _get(value, "data")
    if mime_type not in ICON_MIME_TYPES or not _nonempty_str(data):
        return None
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None
    return Icon(mime_type, decoded) if decoded else None


def convert_value(value: Any, kind: str) -> Any:
    """Coerce a property to the Python type for `kind`; None if it cannot be.

    KIND_MAP values pass through as whatever JSON value was sent (usually a
    dict, but lists and scalars are possible).
    """
    if value is None:
        return None
    if kind == KIND_STRING:
        return _to_string(value)
    if kind == KIND_REAL:
        if _is_number(value):
            return _finite_float(value)
        return _parse_number(value, float)
    if kind == KIND_INTEGER:
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, int) and not isinstance(value, bool):
            return value if _INT64_MIN <= value <= _INT64_MAX else None
        result = _parse_number(value, int)
        return result if result is None or _INT64_MIN <= result <= _INT64_MAX else None
    if kind == KIND_BOOLEAN:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        return None
    if kind == KIND_DATETIME:
        return parse_datetime(value)
    if kind == KIND_STRING_LIST:
        items = value if isinstance(value, list) else [value]
        return [s for s in (_to_string(v) for v in items) if s is not None]
    return value


def _to_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _number(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _parse_number(value: Any, cast: type) -> Any:
    if not isinstance(value, str):
        return None
    try:
        result = cast(value.strip())
    except ValueError:
        return None
    if isinstance(result, float) and not math.isfinite(result):
        return None
    return result


_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?"
    r"(?:([Zz])|([+-])(\d{2}):?(\d{2}))?$"
)


def parse_datetime(value: Any) -> datetime | None:
    """Parse ISO 8601 to an aware datetime; a missing offset is taken as UTC."""
    if not isinstance(value, str):
        return None
    match = _ISO_RE.match(value.strip())
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, _z, sign, oh, om = match.groups()
    try:
        tz = timezone.utc
        if sign:
            if int(om) > 59:
                return None
            offset = timedelta(hours=int(oh), minutes=int(om))
            tz = timezone(-offset if sign == "-" else offset)
        return datetime(
            int(year),
            int(month),
            int(day),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
            int((fraction or "0")[:6].ljust(6, "0")),
            tzinfo=tz,
        )
    except ValueError:
        return None


def _number(value: float) -> str:
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _label(value: Any) -> str:
    return _to_string(value) or ""


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else None


def _list(value: Any) -> Sequence[Any]:
    return value if isinstance(value, list) else []


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(v for v in _list(value) if _nonempty_str(v))


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
