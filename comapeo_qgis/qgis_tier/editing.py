"""Saving a CoMapeo layer's edit session to CoMapeo Desktop.

QGIS's own commit never writes to the cache. In `beforeCommitChanges` the hook
reads the edit buffer, empties it by undoing everything, sends one request per
feature and blocks on the plugin's connection until the replies are in; the
cache is then written only from what the server returned. When the save cannot
go ahead at all the commit is made to fail instead, so the edits stay in the
session: QGIS checks the geometry type of added features before it writes
anything, so a feature of the wrong type stops it.
"""

from __future__ import annotations

import contextlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from qgis.core import (
    Qgis,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsMessageLog,
    QgsVariantUtils,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QDateTime, QObject, Qt, QTimer
from qgis.PyQt.QtWidgets import QMessageBox

from ..core.editing import (
    CLOSED_CODES,
    CONCURRENT_DELETE_TEXT,
    CONCURRENT_TEXT,
    CONFLICT,
    DELETE,
    FEATURE_NOT_FOUND,
    INTERNAL_TEXT,
    KEPT,
    NOT_CONNECTED_TEXT,
    UNCHANGED,
    UNCONFIRMED_CODES,
    UNSENDABLE,
    EditRequest,
    RowBase,
    RowChange,
    SavePlan,
    busy_text,
    checked_text,
    column_fits,
    describe_request,
    discard_messages,
    landed,
    large_save_question,
    lost_text,
    plan_save,
    refusal_text,
    summary_text,
    waiting_text,
)
from ..core.model import LayerDefinition
from .cache import TableInfo, stored_forks

if TYPE_CHECKING:
    from .controller import Controller

# How long a save blocks QGIS waiting for replies; later ones are applied as
# they arrive.
SAVE_WAIT_S = 15.0
# Above this many requests the user is asked first.
LARGE_SAVE = 500

_BLOCKER_WKT = "POLYGON((0 0,1 0,1 1,0 0))"


@dataclass
class _Save:
    requests: list[EditRequest]
    labels: dict[str, str]
    discard_lines: list[str]
    key: str
    answered: int = 0
    saved: int = 0
    concurrent: int = 0
    overlapped: int = 0
    closed: bool = False
    refused: list[tuple[EditRequest, str]] = field(default_factory=list)
    unconfirmed: list[EditRequest] = field(default_factory=list)
    rows: list[tuple[Any, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    refetch: list[str] = field(default_factory=list)
    waited_out: bool = False
    finished: bool = False

    @property
    def done(self) -> bool:
        return self.answered >= len(self.requests)


def feature_label(doc_id: str) -> str:
    return f"Observation {doc_id[:8]}"


class EditHook(QObject):
    """Turns one map layer's saves into `features.update` / `features.delete`."""

    def __init__(
        self, layer: QgsVectorLayer, controller: Controller, project_id: str
    ) -> None:
        super().__init__(layer)
        self.layer = layer
        self.controller = controller
        self.project_id = project_id
        self._key = f"edit:{layer.id()}"
        self._bases: dict[int, RowBase] = {}
        self._muted = 0
        self._warned: set[str] = set()
        self._saves: list[_Save] = []
        self._count = 0
        self._unconfirmed: list[EditRequest] = []
        self._checking = False
        self._allowed_at_start = False
        # Holds this hook has on the sync: key to layer id.
        self._held: dict[str, str] = {}
        self._signals = [
            (layer.beforeCommitChanges, self._before_commit),
            (layer.editingStarted, self._on_started),
            (layer.editingStopped, self._on_stopped),
            (layer.layerModified, self._on_modified),
            (layer.attributeValueChanged, self._on_attribute),
            (layer.geometryChanged, self._on_geometry),
            (layer.featureDeleted, self._on_deleted),
            (layer.featureAdded, self._on_added),
            (layer.willBeDeleted, self.detach),
        ]
        for signal, slot in self._signals:
            signal.connect(slot)

    def detach(self) -> None:
        for signal, slot in self._signals:
            with contextlib.suppress(TypeError, RuntimeError):
                signal.disconnect(slot)
        self._signals = []
        for key in list(self._held):
            self._release(key)

    def _hold(self, key: str) -> None:
        sync, layer_id = self._sync(), self._layer_id()
        if sync is None or layer_id is None:
            return
        self._held[key] = layer_id
        sync.hold(layer_id, key)

    def _release(self, key: str) -> None:
        layer_id = self._held.pop(key, None)
        sync = self._sync()
        if sync is not None and layer_id is not None:
            sync.release(layer_id, key)

    @property
    def outstanding(self) -> int:
        return sum(len(s.requests) - s.answered for s in self._saves)

    # Context

    def _layer_id(self) -> str | None:
        try:
            ids = self.controller.resolved_ids(self.layer)
        except RuntimeError:  # the map layer has been deleted
            return None
        return ids[1] if ids is not None else None

    def _sync(self) -> Any:
        return self.controller.sync_for(self.project_id)

    def _info(self) -> TableInfo | None:
        layer_id = self._layer_id()
        if layer_id is None:
            return None
        cache = self.controller.cache(self.project_id)
        return cache.table(layer_id) if cache.exists else None

    def _notify(self, level: Any, text: str, details: list[str] | None = None) -> None:
        self.controller.notify(level, text, "\n".join(details or []))

    def editing_lost(self, message: str | None) -> None:
        """Editing stopped being possible while this session was open."""
        if self.layer.isModified() and self._allowed_at_start:
            self._warn_once(
                "lost",
                f"{message or ''} Your unsaved changes cannot be saved to CoMapeo "
                "for now; they are still in the edit session.".strip(),
            )

    def _warn_once(self, kind: str, text: str) -> None:
        if kind in self._warned:
            return
        self._warned.add(kind)
        self._notify(Qgis.MessageLevel.Warning, text)

    # Edit-time signals

    def _on_started(self) -> None:
        if self._muted:
            return
        self._bases.clear()
        self._warned.clear()
        allowed, message, _live = self.controller.edit_state(self.layer)
        self._allowed_at_start = allowed
        if allowed:
            return
        layer = self.layer

        def end() -> None:
            if layer.isEditable() and not layer.isModified():
                layer.rollBack()
            layer.setReadOnly(True)

        QTimer.singleShot(0, end)
        self._notify(Qgis.MessageLevel.Warning, message or "")

    def _on_stopped(self) -> None:
        if self._muted:
            return
        self._bases.clear()
        self._warned.clear()
        self._release(self._key)
        self.controller.update_editing(self.layer)

    def _on_modified(self) -> None:
        if self._muted:
            return
        if self.layer.isEditable() and self.layer.isModified():
            self._hold(self._key)
        else:
            self._release(self._key)
            self._bases.clear()

    def _on_attribute(self, fid: int, index: int, _value: Any) -> None:
        if self._muted:
            return
        base = self._record(fid)
        if base is None:
            return
        _allowed, _message, live = self.controller.edit_state(self.layer)
        if live is None:
            return
        if not base.editable:
            self._warn_once(
                "not-own",
                "This observation was created on another device. Your role in this "
                "project lets you change only observations created on this device, "
                "so this change will not be saved.",
            )
        fields = self.layer.fields()
        if not 0 <= index < fields.count():
            return
        column = live.column(fields.at(index).name())
        if column is None or column.category_ids is None:
            return
        if not column_fits(column, self._category_now(fid, base)):
            self._warn_once(
                f"category:{column.name}",
                f"{column.label} is not a field of this observation's category, so "
                "this change will not be saved.",
            )

    def _on_geometry(self, fid: int, _geometry: Any) -> None:
        if self._muted or fid < 0:
            return
        self._record(fid)
        layer = self.layer
        stack = layer.undoStack()
        # The change is on the stack once the command or macro that made it ends.
        pushed = stack.index() + 1

        def undo() -> None:
            if not layer.isEditable() or fid not in self._moved():
                return
            self._muted += 1
            try:
                if stack.index() == pushed:
                    stack.undo()
            finally:
                self._muted -= 1
            self._on_modified()
            if fid in self._moved():
                return  # left for the save to drop, with its message
            self._warn_once(
                "geometry",
                "Points cannot be moved from QGIS yet, so the change of location "
                "was undone.",
            )

        QTimer.singleShot(0, undo)

    def _moved(self) -> set[int]:
        """Features whose location in the edit session differs from the cache's."""
        changed = self.layer.editBuffer().changedGeometries()
        if not changed:
            return set()
        request = QgsFeatureRequest().setFilterFids(list(changed))
        stored = {
            f.id(): _wkb(f.geometry())
            for f in self.layer.dataProvider().getFeatures(request)
        }
        return {fid for fid, g in changed.items() if _wkb(g) != stored.get(fid)}

    def _on_deleted(self, fid: int) -> None:
        if not self._muted:
            self._record(fid)

    def _on_added(self, _fid: int) -> None:
        if self._muted:
            return
        self._warn_once(
            "created",
            "Observations cannot be created from QGIS. New features are discarded "
            "when you save.",
        )

    def _category_now(self, fid: int, base: RowBase) -> str | None:
        info = self._info()
        if info is None:
            return base.category_id
        index = self.layer.fields().indexOf(info.field("category_id"))
        changed = self.layer.editBuffer().changedAttributeValues().get(fid, {})
        if index in changed:
            return _plain(changed[index])
        return base.category_id

    def _record(self, fid: int) -> RowBase | None:
        if fid < 0:
            return None
        base = self._bases.get(fid)
        if base is None:
            base = self._provider_base(fid)
            if base is not None:
                self._bases[fid] = base
        return base

    def _provider_base(self, fid: int) -> RowBase | None:
        info = self._info()
        if info is None:
            return None
        request = QgsFeatureRequest(fid)
        request.setFlags(Qgis.FeatureRequestFlag.NoGeometry)
        row = next(iter(self.layer.dataProvider().getFeatures(request)), None)
        if row is None or not row.isValid():
            return None
        return _base_of(row, info)

    # Saving

    def _before_commit(self, stop_editing: bool) -> None:
        if self._muted or not self.layer.isModified():
            return
        # Raising in this slot does not stop QGIS's commit, which would then
        # write the buffer to the cache: every failure before it is emptied
        # must refuse.
        schema_changed = False
        try:
            prepared = self._prepare()
            if not isinstance(prepared, str):
                plan, live, schema_changed = prepared
                labels = {c.name: c.label for c in live.columns}
                lines = discard_messages(plan.discards, labels)
                prepared = self._start_save(plan.requests, labels, lines)
        except Exception as err:
            QgsMessageLog.logMessage(
                f"Could not prepare the edits to save: {err!r}",
                "CoMapeo",
                Qgis.MessageLevel.Critical,
            )
            prepared = INTERNAL_TEXT
        if isinstance(prepared, str):
            self._refuse(prepared)
            return
        self._send(*prepared)
        if schema_changed:
            self._tidy_after_schema_change(stop_editing)

    def _prepare(self) -> tuple[SavePlan, LayerDefinition, bool] | str:
        """The save to send, or why the whole save must wait."""
        changes, created, schema_changed = self._snapshot()
        live = self._ready()
        if isinstance(live, str):
            return live
        plan = self._plan(live, changes, created, schema_changed)
        if isinstance(plan, str) or len(plan.requests) <= LARGE_SAVE:
            return plan if isinstance(plan, str) else (plan, live, schema_changed)
        if not self._confirm(len(plan.requests)):
            return f"Not saved. {KEPT}"
        # The question ran the event loop: the connection or the app's
        # description of the layer may have changed meanwhile.
        live = self._ready()
        if isinstance(live, str):
            return live
        plan = self._plan(live, changes, created, schema_changed)
        return plan if isinstance(plan, str) else (plan, live, schema_changed)

    def _ready(self) -> LayerDefinition | str:
        refusal = self._refusal()
        if refusal is not None:
            return refusal
        allowed, message, live = self.controller.edit_state(self.layer)
        if not allowed or live is None:
            return f"Not saved. {message} {KEPT}"
        return live

    def _plan(
        self,
        live: LayerDefinition,
        changes: list[RowChange],
        created: int,
        schema_changed: bool,
    ) -> SavePlan | str:
        plan = plan_save(
            live,
            self.project_id,
            changes,
            created=created,
            schema_changed=schema_changed,
            lang=self.controller.lang(),
        )
        try:
            json.dumps([r.params for r in plan.requests])
        except (TypeError, ValueError) as err:
            QgsMessageLog.logMessage(
                f"Could not encode the edits: {err!r}",
                "CoMapeo",
                Qgis.MessageLevel.Critical,
            )
            return INTERNAL_TEXT
        return plan

    def _refusal(self) -> str | None:
        """Why the whole save must wait, keeping the edits; None to go ahead."""
        client = self.controller.client_for(self.project_id)
        sync = self._sync()
        if client is None or sync is None or not sync.connected:
            return NOT_CONNECTED_TEXT
        if self.outstanding:
            return busy_text(self.outstanding)
        return None

    def _confirm(self, count: int) -> bool:
        answer = QMessageBox.question(
            self.controller.dialog_parent(),
            "CoMapeo",
            large_save_question(count),
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Save

    def _snapshot(self) -> tuple[list[RowChange], int, bool]:
        """The edits as plain values; no side effects."""
        layer = self.layer
        info = self._info()
        if info is None:
            raise RuntimeError("the layer's table is not in the cache")
        buffer = layer.editBuffer()
        provider = layer.dataProvider()
        provider_fields = provider.fields()
        fields = layer.fields()
        schema_changed = bool(
            buffer.addedAttributes()
            or buffer.deletedAttributeIds()
            or fields.names() != provider_fields.names()
        )
        changed = buffer.changedAttributeValues()
        moved = buffer.changedGeometries()
        deleted = set(buffer.deletedFeatureIds())
        created = len(buffer.addedFeatures())
        fids = sorted(f for f in set(changed) | set(moved) | deleted if f >= 0)
        rows = {}
        if fids:
            request = QgsFeatureRequest().setFilterFids(fids)
            rows = {f.id(): QgsFeature(f) for f in provider.getFeatures(request)}
        category_field = info.field("category_id")
        definition = info.definition
        changes = []
        for fid in fids:
            row = rows.get(fid)
            if row is None:
                continue
            base = self._bases.get(fid) or _base_of(row, info)
            change = RowChange(base)
            if fid in deleted:
                change.deleted = True
                changes.append(change)
                continue
            for index, value in changed.get(fid, {}).items():
                if not 0 <= index < fields.count():
                    continue
                name = fields.at(index).name()
                old_index = provider_fields.indexOf(name)
                if old_index < 0:
                    continue
                old = _plain(row.attribute(old_index))
                new = _plain(value)
                if name == category_field:
                    change.category_id = (
                        new if isinstance(new, str) or new is None else str(new)
                    )
                    continue
                if _same(new, old):
                    continue
                column = definition.column(name)
                kind = column.kind if column is not None else ""
                change.values[name] = _json_value(new, kind)
            if fid in moved and _wkb(moved[fid]) != _wkb(row.geometry()):
                change.geometry = True
            if change.values or change.geometry or change.category_id is not UNCHANGED:
                changes.append(change)
        return changes, created, schema_changed

    def _empty_buffer(self) -> None:
        stack = self.layer.undoStack()
        while stack.canUndo():
            stack.undo()

    def _refuse(self, message: str) -> None:
        """Keep the user's edits and make QGIS's own commit fail."""
        layer = self.layer
        buffer = layer.editBuffer()
        keep = (
            {f: dict(v) for f, v in buffer.changedAttributeValues().items()},
            {f: QgsGeometry(g) for f, g in buffer.changedGeometries().items()},
            list(buffer.deletedFeatureIds()),
            [QgsFeature(f) for f in buffer.addedFeatures().values()],
        )
        self._muted += 1
        try:
            self._empty_buffer()
            blocker = QgsFeature(layer.fields())
            blocker.setGeometry(QgsGeometry.fromWkt(_BLOCKER_WKT))
            layer.addFeature(blocker)
        finally:
            self._muted -= 1

        def restore() -> None:
            self._muted += 1
            try:
                if not layer.isEditable():
                    layer.startEditing()
                self._empty_buffer()
                layer.beginEditCommand("Unsaved CoMapeo changes")
                for fid, values in keep[0].items():
                    for index, value in values.items():
                        layer.changeAttributeValue(fid, index, value)
                for fid, geometry in keep[1].items():
                    layer.changeGeometry(fid, geometry)
                if keep[2]:
                    layer.deleteFeatures(keep[2])
                for feature in keep[3]:
                    layer.addFeature(feature)
                layer.endEditCommand()
            finally:
                self._muted -= 1
            self._on_modified()
            self._notify(Qgis.MessageLevel.Warning, message)

        QTimer.singleShot(0, restore)

    def _tidy_after_schema_change(self, stop_editing: bool) -> None:
        # Undoing a column rename leaves an entry that makes QGIS's commit fail.
        layer = self.layer

        def tidy() -> None:
            if layer.isEditable():
                self._muted += 1
                try:
                    layer.rollBack()
                    if not stop_editing:
                        layer.startEditing()
                finally:
                    self._muted -= 1

        QTimer.singleShot(0, tidy)

    def _start_save(
        self,
        requests: Sequence[EditRequest],
        labels: dict[str, str],
        discards: list[str],
    ) -> tuple[Any, _Save] | str:
        client = self.controller.client_for(self.project_id)
        if client is None:
            return NOT_CONNECTED_TEXT
        self._count += 1
        key = f"save:{self.layer.id()}:{self._count}"
        save = _Save(list(requests), labels, discards, key)
        # Before emptying the buffer: that releases the edit session's hold.
        self._hold(save.key)
        return client, save

    def _send(self, client: Any, save: _Save) -> None:
        self._muted += 1
        try:
            self._empty_buffer()
        finally:
            self._muted -= 1
        self._release(self._key)
        self._bases.clear()
        self._saves.append(save)
        for request in save.requests:
            client.request(
                request.method, dict(request.params), self._answer(save, request)
            )
        if not save.done:
            client.wait(lambda: save.done, SAVE_WAIT_S)
        if save.done:
            self._finish(save)
        else:
            save.waited_out = True
            self._flush(save)
            self._notify(
                Qgis.MessageLevel.Warning,
                waiting_text(len(save.requests) - save.answered),
            )

    def _answer(self, save: _Save, request: EditRequest) -> Any:
        def callback(result: Any, error: Any) -> None:
            self._take(save, request, result, error)
            save.answered += 1
            if save.waited_out:
                if save.done:
                    self._finish(save)
                else:
                    QTimer.singleShot(0, lambda: self._flush(save))

        return callback

    def _take(self, save: _Save, request: EditRequest, result: Any, error: Any) -> None:
        doc_id = request.doc_id
        if error is None:
            if isinstance(result, dict) and result.get("concurrent") is True:
                # Another write overlapped; only a fresh fetch says what stands.
                if request.method == DELETE:
                    save.overlapped += 1
                else:
                    save.saved += 1
                    save.concurrent += 1
                save.refetch.append(doc_id)
                return
            save.saved += 1
            if request.method == DELETE:
                save.removed.append(doc_id)
                return
            feature = result.get("feature") if isinstance(result, dict) else None
            if isinstance(feature, dict) and feature.get("id") == doc_id:
                save.rows.append((feature, request.base.revision))
                self._move_base(doc_id, request.base, feature)
            else:
                save.refetch.append(doc_id)
            return
        code = error.code
        if code in UNCONFIRMED_CODES:
            save.closed = save.closed or code in CLOSED_CODES
            save.unconfirmed.append(request)
            save.refetch.append(doc_id)
            return
        save.refused.append((request, refusal_text(code, error.data, save.labels)))
        data = error.data if isinstance(error.data, dict) else {}
        if code == CONFLICT:
            feature = data.get("feature")
            if isinstance(feature, dict) and feature.get("id") == doc_id:
                save.rows.append((feature, request.base.revision))
            else:
                save.refetch.append(doc_id)
        elif code == FEATURE_NOT_FOUND:
            save.removed.append(doc_id)

    def _move_base(self, doc_id: str, sent: RowBase, feature: dict[str, Any]) -> None:
        """Edits made after a save timed out were made on top of the user's own
        change, so they are based on the version that change produced."""
        version = feature.get("version")
        if not isinstance(version, str):
            return
        forks = feature.get("forks")
        forks = (
            tuple(sorted(f for f in forks if isinstance(f, str)))
            if isinstance(forks, list)
            else ()
        )
        for fid, base in list(self._bases.items()):
            if base.doc_id == doc_id and base.revision == sent.revision:
                category = feature.get("categoryId")
                self._bases[fid] = RowBase(
                    doc_id,
                    version,
                    forks,
                    category if isinstance(category, str) else None,
                    feature.get("editable") is True,
                )

    def _flush(self, save: _Save) -> None:
        layer_id, sync = self._layer_id(), self._sync()
        rows, removed, refetch = save.rows, save.removed, save.refetch
        save.rows, save.removed, save.refetch = [], [], []
        if layer_id is None or sync is None:
            return
        if rows or removed:
            sync.apply_edit_results(layer_id, rows, removed)
        if refetch:
            sync.refetch(layer_id, refetch)

    def _finish(self, save: _Save) -> None:
        if save.finished:
            return
        save.finished = True
        self._flush(save)
        if save in self._saves:
            self._saves.remove(save)
        self._release(save.key)
        self._report(save)
        if save.unconfirmed:
            self._unconfirmed.extend(save.unconfirmed)
            self.check_unconfirmed()

    def _report(self, save: _Save) -> None:
        details = [
            f"{feature_label(r.doc_id)}: {text}. Not saved: "
            + describe_request(r, save.labels)
            for r, text in save.refused
        ]
        details += save.discard_lines
        lost = len(save.unconfirmed)
        if lost:
            details += [
                f"{feature_label(r.doc_id)} not confirmed: "
                + describe_request(r, save.labels)
                for r in save.unconfirmed
            ]
        if not save.requests:
            text = "Nothing was saved to CoMapeo."
        else:
            text = summary_text(
                save.saved, len(save.refused), lost, overlapped=save.overlapped
            )
        if save.overlapped:
            details.insert(0, CONCURRENT_DELETE_TEXT)
        if save.concurrent:
            details.insert(0, CONCURRENT_TEXT)
        level = Qgis.MessageLevel.Success
        if save.refused or lost or save.discard_lines or save.concurrent:
            level = Qgis.MessageLevel.Warning
        if save.overlapped:
            level = Qgis.MessageLevel.Warning
        self._notify(level, text, details)
        if lost:
            self._notify(Qgis.MessageLevel.Warning, lost_text(lost, closed=save.closed))

    def check_unconfirmed(self) -> None:
        """Once connected again, find out which unconfirmed edits landed."""
        if not self._unconfirmed or self._checking:
            return
        client = self.controller.client_for(self.project_id)
        layer_id = self._layer_id()
        if client is None or layer_id is None:
            return
        requests = list(self._unconfirmed)
        self._checking = True
        params = {
            "projectId": self.project_id,
            "layerId": layer_id,
            "ids": sorted({r.doc_id for r in requests}),
        }

        def got(result: Any, error: Any) -> None:
            self._checking = False
            if error is not None or not isinstance(result, dict):
                return
            features = {
                f.get("id"): f
                for f in result.get("features") or []
                if isinstance(f, dict)
            }
            missing = []
            for request in requests:
                if not landed(request, features.get(request.doc_id)):
                    missing.append(request)
                if request in self._unconfirmed:
                    self._unconfirmed.remove(request)
            labels = {}
            live = self.controller.live_definition(self.project_id, layer_id)
            if live is not None:
                labels = {c.name: c.label for c in live.columns}
            self._notify(
                Qgis.MessageLevel.Warning if missing else Qgis.MessageLevel.Info,
                checked_text(len(requests) - len(missing), len(missing)),
                [
                    f"{feature_label(r.doc_id)} not saved: "
                    + describe_request(r, labels)
                    for r in missing
                ],
            )

        client.request("layers.getFeatures", params, got)


def _base_of(row: QgsFeature, info: TableInfo) -> RowBase:
    def value(role: str) -> Any:
        name = info.private.get(role)
        return _plain(row[name]) if name is not None else None

    category = value("category_id")
    return RowBase(
        doc_id=str(value("doc_id")),
        version=str(value("version")),
        forks=stored_forks(value("forks")),
        category_id=category if isinstance(category, str) else None,
        editable=value("editable") is True,
    )


def _wkb(geometry: QgsGeometry) -> bytes:
    return bytes(geometry.asWkb()) if not geometry.isNull() else b""


def _plain(value: Any) -> Any:
    if value is None or QgsVariantUtils.isNull(value):
        return None
    return value


def _same(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, QDateTime) and isinstance(b, QDateTime):
        return a.toMSecsSinceEpoch() == b.toMSecsSinceEpoch()
    try:
        return bool(a == b)
    except TypeError:
        return False


def _json_value(value: Any, kind: str) -> Any:
    if value is None:
        return None
    if isinstance(value, QDateTime):
        return value.toUTC().toString(Qt.DateFormat.ISODateWithMs)
    if isinstance(value, float) and not math.isfinite(value):
        return UNSENDABLE
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    if isinstance(value, (str, bool, int, float)):
        return value
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return UNSENDABLE
    return value
