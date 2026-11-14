"""One GeoPackage per CoMapeo project, in the QGIS profile folder.

Each layer definition gets a table named after its schema key, so a rebuild
creates a new table. A staged table is filled while map layers stay on the old
one; only once it is complete is it committed and the old one dropped.
Besides the server's columns every table has private columns for the document
id, version, category id and attachments; their names are chosen per table so
they never collide with a column the server sends (SQLite names are compared
case-insensitively). Versions live in the row, so the id-to-version map that
reconciliation needs always matches what is actually stored.

`comapeo_meta` is a key/value table holding the project, its categories and,
per layer, the definition as served, its table name and the last refresh time
(`layer:<id>`), plus any staged table not yet committed (`staged:<id>`).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransformContext,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsLineString,
    QgsMessageLog,
    QgsPoint,
    QgsProviderRegistry,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from ..core.model import (
    GEOMETRY_LINESTRING,
    GEOMETRY_POINT,
    KIND_BOOLEAN,
    KIND_DATETIME,
    KIND_INTEGER,
    KIND_MAP,
    KIND_REAL,
    KIND_STRING_LIST,
    Attachment,
    Category,
    ColumnSpec,
    FeatureRecord,
    Geometry,
    LayerDefinition,
    Project,
    attachments_from_json,
    parse_categories,
    parse_layers,
)
from ..core.reconcile import SyncPlan
from .qt_compat import meta_type, to_qdatetime

META_TABLE = "comapeo_meta"
FILE_SUFFIX = ".gpkg"
# GDAL waits 5 s on a locked GeoPackage, on the main thread; the plugin reads with
# sqlite3 instead, and before a write waits this long for another writer.
WRITE_WAIT_S = 0.25
LOCKED_MESSAGE = (
    "Another program is using the cached data, so it could not be updated. "
    "The plugin tries again every few seconds."
)

_PRIVATE = (
    ("fid", "fid"),
    ("geometry", "geom"),
    ("doc_id", "comapeo_doc_id"),
    ("version", "comapeo_version"),
    ("category_id", "comapeo_category_id"),
    ("attachments", "comapeo_attachments"),
)


def default_cache_root() -> Path:
    return Path(QgsApplication.qgisSettingsDirPath()) / "comapeo" / "cache"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def cache_file_name(project_id: str) -> str:
    return f"{_digest(project_id)}{FILE_SUFFIX}"


def cache_file(root: Path, project_id: str) -> Path:
    return root / cache_file_name(project_id)


def is_cache_source(root: Path, source: str) -> bool:
    path = source.split("|", 1)[0]
    try:
        return Path(path).resolve().parent == root.resolve()
    except OSError:
        return False


def table_from_source(source: str) -> str | None:
    match = re.search(r"\|layername=([^|]+)", source)
    return match.group(1) if match else None


@dataclass(frozen=True)
class TableInfo:
    """A cached layer: where it is stored and the definition it was built from.

    Raises ValueError when the definition does not parse as this layer's."""

    layer_id: str
    table: str
    schema_key: str
    raw_definition: Mapping[str, Any]
    private: Mapping[str, str]
    last_refresh: str | None = None
    definition: LayerDefinition = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        layers = parse_layers({"layers": [dict(self.raw_definition)]})
        if not layers or layers[0].layer_id != self.layer_id:
            raise ValueError(f"cached definition of {self.layer_id!r} is unreadable")
        missing = {r for r, _ in _PRIVATE} - set(self.private)
        if missing:
            raise ValueError(f"cached table of {self.layer_id!r} lacks {missing}")
        object.__setattr__(self, "definition", layers[0])

    def field(self, role: str) -> str:
        return self.private[role]

    def to_json(self) -> dict[str, Any]:
        return {
            "layerId": self.layer_id,
            "table": self.table,
            "schemaKey": self.schema_key,
            "definition": dict(self.raw_definition),
            "private": dict(self.private),
            "lastRefresh": self.last_refresh,
        }

    @classmethod
    def from_json(cls, value: Any) -> TableInfo | None:
        if not isinstance(value, dict):
            return None
        try:
            info = cls(
                layer_id=str(value["layerId"]),
                table=str(value["table"]),
                schema_key=str(value["schemaKey"]),
                raw_definition=dict(value["definition"]),
                private={str(k): str(v) for k, v in value["private"].items()},
                last_refresh=value.get("lastRefresh"),
            )
        except (KeyError, TypeError, AttributeError, ValueError):
            return None
        return info


def private_names(columns: Sequence[ColumnSpec]) -> dict[str, str]:
    taken = {c.name.lower() for c in columns}
    names = {}
    for role, base in _PRIVATE:
        name, n = base, 1
        while name.lower() in taken:
            n += 1
            name = f"{base}_{n}"
        taken.add(name.lower())
        names[role] = name
    return names


def _qgs_field(column: ColumnSpec) -> QgsField:
    if column.kind == KIND_REAL:
        field = QgsField(column.name, meta_type("Double"))
    elif column.kind == KIND_INTEGER:
        field = QgsField(column.name, meta_type("LongLong"))
    elif column.kind == KIND_BOOLEAN:
        field = QgsField(column.name, meta_type("Bool"))
    elif column.kind == KIND_DATETIME:
        field = QgsField(column.name, meta_type("QDateTime"))
    elif column.kind == KIND_STRING_LIST:
        field = QgsField(
            column.name, meta_type("QStringList"), subType=meta_type("QString")
        )
    elif column.kind == KIND_MAP:
        field = QgsField(column.name, meta_type("QVariantMap"))
    else:
        field = QgsField(column.name, meta_type("QString"))
    field.setAlias(column.label)
    return field


def wkb_type(geometry_type: str) -> Any:
    if geometry_type == GEOMETRY_POINT:
        return Qgis.WkbType.Point
    if geometry_type == GEOMETRY_LINESTRING:
        return Qgis.WkbType.LineStringM
    return Qgis.WkbType.NoGeometry


def to_qgs_geometry(geometry: Geometry | None, geometry_type: str) -> QgsGeometry:
    """Convert to the table's type: 2D points, lines with M (NaN where unknown)."""
    if geometry is None or geometry.type != geometry_type:
        return QgsGeometry()
    if geometry_type == GEOMETRY_POINT:
        x, y = geometry.coordinates[0][:2]
        return QgsGeometry(QgsPoint(x, y))
    if geometry_type == GEOMETRY_LINESTRING:
        xs = [c[0] for c in geometry.coordinates]
        ys = [c[1] for c in geometry.coordinates]
        ms = list(geometry.m_values or [math.nan] * len(xs))
        return QgsGeometry(QgsLineString(xs, ys, [], ms))
    return QgsGeometry()


def to_qgs_value(value: Any, column: ColumnSpec) -> Any:
    if value is None:
        return None
    if column.kind == KIND_DATETIME and isinstance(value, datetime):
        return to_qdatetime(value)
    if column.kind == KIND_MAP and not isinstance(value, (dict, list)):
        return {"value": value}
    return value


def _attachments_json(record: FeatureRecord) -> str:
    return json.dumps(
        [
            {"type": a.type, "name": a.name, "key": a.key, "urls": dict(a.urls)}
            for a in record.attachments
        ],
        ensure_ascii=False,
    )


class CacheError(Exception):
    pass


class CacheLocked(CacheError):
    """Another program holds a lock on the cache file."""

    def __init__(self, message: str = LOCKED_MESSAGE) -> None:
        super().__init__(message)


def _is_lock(err: sqlite3.Error) -> bool:
    text = str(err).lower()
    return "locked" in text or "busy" in text


@contextlib.contextmanager
def _reading(path: Path) -> Iterator[sqlite3.Connection]:
    """A read-only connection that never waits on a lock: CacheLocked instead."""
    try:
        conn = sqlite3.connect(
            f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=0
        )
    except sqlite3.Error as err:
        if _is_lock(err):
            raise CacheLocked() from err
        raise
    try:
        yield conn
    except sqlite3.Error as err:
        if _is_lock(err):
            raise CacheLocked() from err
        raise
    finally:
        conn.close()


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class ProjectCache:
    def __init__(self, root: Path, project_id: str) -> None:
        self.root = Path(root)
        self.project_id = project_id
        self.path = cache_file(self.root, project_id)
        self._layers: dict[str, QgsVectorLayer] = {}
        self._meta: dict[str, Any] | None = None
        self._checked: tuple[Any, bool] | None = None
        # The last read of the metadata found the file locked by another program.
        self.locked = False

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def is_damaged(self) -> bool:
        """The file exists but is not a readable cache. Statuses ask often, so a
        definite answer is remembered until the file or its write-ahead log
        changes; while the file is locked the answer is no, and not remembered."""
        version = _file_version(self.path)
        if version is None:
            return False
        if self._checked is None or self._checked[0] != version:
            damaged = _damaged(self.path)
            if damaged is None:
                return False
            self._checked = (version, damaged)
        return self._checked[1]

    def set_aside_if_damaged(self) -> None:
        """Call before writing: metadata written into a damaged file would hide
        the damage. Windows will not rename an open file; the writes then repair
        it in place."""
        if not self.is_damaged():
            return
        try:
            aside = self.set_aside()
        except OSError as err:
            QgsMessageLog.logMessage(
                f"Cannot move the damaged cache {self.path} aside: {err}",
                "CoMapeo",
                Qgis.MessageLevel.Warning,
            )
            return
        QgsMessageLog.logMessage(
            f"The cache {self.path} could not be read; moved it to {aside.name} "
            "and downloading the project again.",
            "CoMapeo",
            Qgis.MessageLevel.Info,
        )

    def set_aside(self) -> Path:
        """Rename the file (and SQLite's side files) out of the way, so a fresh
        cache can be built; the old one is kept for anyone who wants to look."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"{self.path.name}.damaged-{stamp}"
        aside = self.path.with_name(name)
        count = 1
        while aside.exists():
            count += 1
            aside = self.path.with_name(f"{name}-{count}")
        self.close()
        os.replace(self.path, aside)
        for suffix in ("-wal", "-shm", "-journal"):
            side = Path(f"{self.path}{suffix}")
            if side.exists():
                with contextlib.suppress(OSError):
                    os.replace(side, Path(f"{aside}{suffix}"))
        self._checked = None
        return aside

    def uri(self, table: str) -> str:
        return f"{self.path}|layername={table}"

    def close(self) -> None:
        self._layers.clear()
        self._meta = None

    def remove(self) -> None:
        """Delete the cache with its side files and any damaged copies set aside."""
        self.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)
        for aside in self.path.parent.glob(f"{self.path.name}.damaged-*"):
            aside.unlink(missing_ok=True)

    # Metadata

    def _read_meta(self) -> dict[str, Any]:
        if self._meta is not None:
            return self._meta
        self.locked = False
        if not self.exists:
            return {}
        try:
            with _reading(self.path) as conn:
                found = conn.execute("SELECT key, value FROM comapeo_meta").fetchall()
        except CacheLocked:
            self.locked = True
            return {}
        except sqlite3.Error:
            return {}
        values = {}
        for key, value in found:
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            try:
                values[key] = json.loads(value)
            except ValueError:
                continue
        self._meta = values
        return values

    def _wait_for_writers(self) -> None:
        """Raise CacheLocked rather than let GDAL wait 5 s on another program's lock."""
        if not self.exists:
            return
        try:
            conn = sqlite3.connect(
                str(self.path), timeout=WRITE_WAIT_S, isolation_level=None
            )
        except sqlite3.Error:
            return
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
        except sqlite3.Error as err:
            if _is_lock(err):
                raise CacheLocked() from err
        finally:
            conn.close()

    def _write_meta(self, key: str, value: Any) -> None:
        self._wait_for_writers()
        layer = self._open(META_TABLE) or self._create_meta()
        text = json.dumps(value, ensure_ascii=False)
        provider = layer.dataProvider()
        matches = _meta_rows(layer, key)
        index = layer.fields().indexOf("value")
        if matches:
            ok = provider.changeAttributeValues({matches[0]: {index: text}})
            if len(matches) > 1:
                provider.deleteFeatures(matches[1:])
        else:
            feature = QgsFeature(layer.fields())
            feature["key"] = key
            feature["value"] = text
            ok, _ = provider.addFeatures([feature])
        if not ok:
            self._meta = None
            raise CacheError(f"cannot write cache metadata: {provider.lastError()}")
        if self._meta is not None:
            self._meta[key] = json.loads(text)

    def _create_meta(self) -> QgsVectorLayer:
        fields = QgsFields()
        fields.append(QgsField("key", meta_type("QString")))
        fields.append(QgsField("value", meta_type("QString")))
        self._create_table(META_TABLE, fields, Qgis.WkbType.NoGeometry, "fid", None)
        layer = self._open(META_TABLE)
        if layer is None:
            raise CacheError(f"cannot create {self.path}")
        return layer

    def project(self) -> Project | None:
        value = self._read_meta().get("project")
        if not isinstance(value, dict) or value.get("projectId") != self.project_id:
            return None
        return Project(
            self.project_id,
            value.get("name") if isinstance(value.get("name"), str) else None,
            value.get("color") if isinstance(value.get("color"), str) else None,
        )

    def build_type(self) -> str | None:
        """The app build that last served this project."""
        value = self._read_meta().get("project")
        build = value.get("buildType") if isinstance(value, dict) else None
        return build if isinstance(build, str) and build else None

    def set_project(self, project: Project, build_type: str | None = None) -> None:
        value: dict[str, Any] = {
            "projectId": project.project_id,
            "name": project.name,
            "color": project.color,
        }
        build = build_type or self.build_type()
        if build:
            value["buildType"] = build
        self._write_meta("project", value)

    def categories(self) -> list[Category]:
        return parse_categories(self._read_meta().get("categories"))

    def raw_categories(self) -> Any:
        return self._read_meta().get("categories")

    def set_categories(self, raw_result: Any) -> None:
        self._write_meta("categories", raw_result)

    def tables(self) -> dict[str, TableInfo]:
        tables = {}
        for key, value in self._read_meta().items():
            if not key.startswith("layer:"):
                continue
            info = TableInfo.from_json(value)
            if info is not None:
                tables[info.layer_id] = info
        return tables

    def table(self, layer_id: str) -> TableInfo | None:
        return self.tables().get(layer_id)

    def layer_for_table(self, table: str) -> str | None:
        """The layer whose committed or staged table this is."""
        for key, value in self._read_meta().items():
            if key.startswith(("layer:", "staged:")):
                info = TableInfo.from_json(value)
                if info is not None and info.table == table:
                    return info.layer_id
        return None

    def mark_refreshed(self, layer_id: str, when: datetime | None = None) -> TableInfo:
        info = self.table(layer_id)
        if info is None:
            raise CacheError(f"layer {layer_id!r} is not cached")
        stamp = (when or datetime.now(timezone.utc)).isoformat()
        updated = TableInfo(
            info.layer_id,
            info.table,
            info.schema_key,
            info.raw_definition,
            info.private,
            stamp,
        )
        self._write_meta(f"layer:{layer_id}", updated.to_json())
        return updated

    def forget_layer(self, layer_id: str) -> None:
        self._delete_meta(f"layer:{layer_id}")

    def _delete_meta(self, key: str) -> None:
        self._wait_for_writers()
        layer = self._open(META_TABLE)
        if layer is None:
            return
        matches = _meta_rows(layer, key)
        if matches:
            layer.dataProvider().deleteFeatures(matches)
        if self._meta is not None:
            self._meta.pop(key, None)

    # Tables

    def is_stored(self, info: TableInfo) -> bool:
        """Whether the table is in the file; assumed so while the file is locked."""
        if not self.exists:
            return False
        try:
            with _reading(self.path) as conn:
                found = conn.execute(
                    "SELECT 1 FROM gpkg_contents WHERE table_name = ?", (info.table,)
                ).fetchone()
                return found is not None and bool(
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (info.table,),
                    ).fetchone()
                )
        except CacheLocked:
            return True
        except sqlite3.Error:
            return False

    def ensure_table(
        self, raw_definition: Mapping[str, Any], schema_key: str, stage: bool = False
    ) -> tuple[TableInfo, bool]:
        """The table for this definition; True when it was (re)created.

        With `stage`, a new table replacing a stored one is left uncommitted:
        `table()` keeps returning the old one until `commit_table()`."""
        definition = parse_layers({"layers": [dict(raw_definition)]})
        if not definition:
            raise CacheError("unreadable layer definition")
        layer = definition[0]
        current = self.table(layer.layer_id)
        staged_key = f"staged:{layer.layer_id}"
        previous = TableInfo.from_json(self._read_meta().get(staged_key))
        if (
            current is not None
            and current.schema_key == schema_key
            and self.is_stored(current)
        ):
            if staged_key in self._read_meta():
                if previous is not None and previous.table != current.table:
                    with contextlib.suppress(CacheError):
                        self.drop_table(previous.table)
                self._delete_meta(staged_key)
            return current, False
        stage = stage and current is not None and self.is_stored(current)
        table = _table_name(layer.layer_id, schema_key)
        if previous is not None and previous.table not in (
            table,
            current and current.table,
        ):
            with contextlib.suppress(CacheError):
                self.drop_table(previous.table)
        private = private_names(layer.columns)
        fields = QgsFields()
        for column in layer.columns:
            fields.append(_qgs_field(column))
        for role in ("doc_id", "version", "category_id", "attachments"):
            fields.append(QgsField(private[role], meta_type("QString")))
        self._layers.pop(table, None)
        self._create_table(
            table,
            fields,
            wkb_type(layer.geometry_type),
            private["fid"],
            private["geometry"],
        )
        info = TableInfo(
            layer.layer_id, table, schema_key, dict(raw_definition), private
        )
        if stage:
            self._write_meta(f"staged:{layer.layer_id}", info.to_json())
        else:
            self._delete_meta(f"staged:{layer.layer_id}")
            self._write_meta(f"layer:{layer.layer_id}", info.to_json())
        return info, True

    def commit_table(self, info: TableInfo, when: datetime | None = None) -> TableInfo:
        """Make a filled staged table the layer's table, stamped as refreshed."""
        stamp = (when or datetime.now(timezone.utc)).isoformat()
        committed = TableInfo(
            info.layer_id,
            info.table,
            info.schema_key,
            info.raw_definition,
            info.private,
            stamp,
        )
        self._write_meta(f"layer:{info.layer_id}", committed.to_json())
        self._delete_meta(f"staged:{info.layer_id}")
        return committed

    def drop_table(self, table: str) -> None:
        self._wait_for_writers()
        self._layers.pop(table, None)
        metadata = QgsProviderRegistry.instance().providerMetadata("ogr")
        connection = metadata.createConnection(str(self.path), {})
        try:
            connection.dropVectorTable("", table)
        except Exception as err:  # QgsProviderConnectionException
            raise CacheError(f"cannot drop {table}: {err}") from err

    def table_names(self) -> list[str]:
        if not self.exists:
            return []
        metadata = QgsProviderRegistry.instance().providerMetadata("ogr")
        connection = metadata.createConnection(str(self.path), {})
        return [t.tableName() for t in connection.tables()]

    def versions(self, info: TableInfo) -> dict[str, str]:
        """Raises CacheLocked while another program holds the file."""
        if not self.exists:
            return {}
        id_field, version_field = info.field("doc_id"), info.field("version")
        names = ", ".join(_quoted(n) for n in (id_field, version_field))
        query = f"SELECT {names} FROM {_quoted(info.table)}"  # noqa: S608 - quoted
        try:
            with _reading(self.path) as conn:
                found = conn.execute(query).fetchall()
        except CacheLocked:
            raise
        except sqlite3.Error:
            return {}
        return {
            doc_id: version
            for doc_id, version in found
            if isinstance(doc_id, str) and isinstance(version, str)
        }

    def attachments(
        self, info: TableInfo, doc_id: str
    ) -> tuple[Attachment, ...] | None:
        """The attachments stored with a document; None if it is not cached, or
        cannot be read now.

        Their URLs date from when the row was written; never fetch from them."""
        if not self.exists:
            return None
        id_field, column = info.field("doc_id"), info.field("attachments")
        table, column, key = map(_quoted, (info.table, column, id_field))
        query = f"SELECT {column} FROM {table} WHERE {key} = ? LIMIT 1"  # noqa: S608
        try:
            with _reading(self.path) as conn:
                found = conn.execute(query, (doc_id,)).fetchone()
        except (CacheError, sqlite3.Error):
            return None
        return attachments_from_json(found[0]) if found is not None else None

    def apply(self, info: TableInfo, plan: SyncPlan) -> int:
        """Write a reconcile plan; returns the number of rows touched."""
        if plan.empty:
            return 0
        self._wait_for_writers()
        layer = self._open(info.table)
        if layer is None:
            raise CacheError(f"table {info.table} is missing")
        provider = layer.dataProvider()
        fields = layer.fields()
        definition = info.definition
        id_field = info.field("doc_id")
        existing: dict[str, list[int]] = {}
        request = QgsFeatureRequest()
        request.setFlags(Qgis.FeatureRequestFlag.NoGeometry)
        request.setSubsetOfAttributes([id_field], fields)
        for feature in layer.getFeatures(request):
            existing.setdefault(feature[id_field], []).append(feature.id())

        to_delete: list[int] = []
        for doc_id in plan.deletes:
            to_delete.extend(existing.pop(doc_id, []))
        changes: dict[int, dict[int, Any]] = {}
        geometries: dict[int, QgsGeometry] = {}
        to_add: list[QgsFeature] = []
        for record in plan.upserts:
            values = self._row(record, definition, info, fields)
            geometry = to_qgs_geometry(record.geometry, definition.geometry_type)
            fids = existing.get(record.id)
            if fids:
                to_delete.extend(fids[1:])
                changes[fids[0]] = values
                geometries[fids[0]] = geometry
            else:
                feature = QgsFeature(fields)
                for index, value in values.items():
                    feature.setAttribute(index, value)
                feature.setGeometry(geometry)
                to_add.append(feature)

        # Geometry first: if a write fails, the old version gets the row refetched.
        steps = [
            (bool(to_delete), lambda: provider.deleteFeatures(to_delete)),
            (bool(changes), lambda: provider.changeGeometryValues(geometries)),
            (bool(changes), lambda: provider.changeAttributeValues(changes)),
            (bool(to_add), lambda: provider.addFeatures(to_add)[0]),
        ]
        for needed, write in steps:
            if needed and not write():
                raise CacheError(
                    f"cannot write to {info.table}: {provider.lastError()}"
                )
        return len(to_delete) + len(changes) + len(to_add)

    def _row(
        self,
        record: FeatureRecord,
        definition: LayerDefinition,
        info: TableInfo,
        fields: QgsFields,
    ) -> dict[int, Any]:
        values: dict[int, Any] = {}
        for column in definition.columns:
            index = fields.indexOf(column.name)
            if index >= 0:
                values[index] = to_qgs_value(record.attributes.get(column.name), column)
        private = {
            "doc_id": record.id,
            "version": record.version,
            "category_id": record.category_id,
            "attachments": _attachments_json(record),
        }
        for role, value in private.items():
            values[fields.indexOf(info.field(role))] = value
        return values

    def _open(self, table: str) -> QgsVectorLayer | None:
        layer = self._layers.get(table)
        if layer is not None and layer.isValid():
            return layer
        if not self.exists:
            return None
        layer = QgsVectorLayer(self.uri(table), table, "ogr")
        if not layer.isValid():
            return None
        self._layers[table] = layer
        return layer

    def _create_table(
        self,
        table: str,
        fields: QgsFields,
        wkb: Any,
        fid: str,
        geometry: str | None,
    ) -> None:
        self._wait_for_writers()
        if not self.root.is_dir():
            # mkdir(parents=True) ignores `mode` for the parents it creates.
            self.root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.root.mkdir(mode=0o700, exist_ok=True)
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = table
        options.layerOptions = [f"FID={fid}"] + (
            [f"GEOMETRY_NAME={geometry}"] if geometry else []
        )
        if self.exists:
            options.actionOnExistingFile = (
                QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
            )
        writer = QgsVectorFileWriter.create(
            str(self.path),
            fields,
            wkb,
            QgsCoordinateReferenceSystem("EPSG:4326"),
            QgsCoordinateTransformContext(),
            options,
        )
        error = writer.hasError()
        message = writer.errorMessage()
        del writer
        if error:
            raise CacheError(f"cannot create table {table}: {message}")
        if os.name == "posix":
            os.chmod(self.path, 0o600)


def _table_name(layer_id: str, schema_key: str) -> str:
    safe = re.sub(r"[^a-z0-9_]", "_", layer_id.lower())[:40] or "layer"
    return f"{safe}_{_digest(layer_id + chr(0) + schema_key)[:8]}"


def _meta_rows(layer: QgsVectorLayer, key: str) -> list[int]:
    # Compared here, not in a filter expression, so any key matches as itself.
    return [f.id() for f in layer.getFeatures() if f["key"] == key]


def project_in_file(path: Path) -> Project | None:
    """The project a cache file belongs to, from its metadata.

    Raises CacheLocked while another program holds the file."""
    if not Path(path).is_file():
        return None
    try:
        with _reading(Path(path)) as conn:
            found = conn.execute(
                "SELECT value FROM comapeo_meta WHERE key = 'project'"
            ).fetchall()
    except CacheLocked:
        raise
    except sqlite3.Error:
        return None
    for (text,) in found:
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return None
        project_id = value.get("projectId") if isinstance(value, dict) else None
        if not isinstance(project_id, str) or not project_id:
            return None
        if cache_file(Path(path).parent, project_id) != Path(path):
            return None
        name = value.get("name")
        color = value.get("color")
        return Project(
            project_id,
            name if isinstance(name, str) else None,
            color if isinstance(color, str) else None,
        )
    return None


def _file_version(path: Path) -> tuple[Any, ...] | None:
    """Changes whenever the file's content may have: in write-ahead-log mode a
    write lands in the -wal file and leaves the main file as it was."""
    try:
        st = path.stat()
    except OSError:
        return None
    try:
        wal = Path(f"{path}-wal").stat()
        logged: tuple[int, int] | None = (wal.st_mtime_ns, wal.st_size)
    except OSError:
        logged = None
    return (st.st_mtime_ns, st.st_size, logged)


def _damaged(path: Path) -> bool | None:
    """Whether SQLite reports the file as no database, a malformed one or one
    without our metadata; None when it cannot tell (a lock, a permission or a
    disk problem), which is not damage."""
    try:
        with _reading(path) as conn:
            conn.execute("SELECT key, value FROM comapeo_meta LIMIT 1").fetchall()
    except CacheLocked:
        return None
    except sqlite3.DatabaseError as err:
        text = str(err).lower()
        if any(
            words in text
            for words in ("not a database", "malformed", "no such table: comapeo_meta")
        ):
            return True
        return None
    except sqlite3.Error:
        return None
    return False


def cached_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return []
    return sorted(root.glob(f"*{FILE_SUFFIX}"))
