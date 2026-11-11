"""Edits to CoMapeo features: which changes to send, which to drop, and the
words for every outcome. No QGIS here; the save hook feeds it plain values.

The server's answer is the one that counts. What is dropped here is only what
the server is certain to refuse whatever the data: a kind of edit or a column
the layer does not offer, a column outside the feature's category, and
everything the plugin itself does not offer yet (moving points, dates and
multiple choice).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .model import (
    KIND_DATETIME,
    KIND_MAP,
    KIND_STRING_LIST,
    ColumnSpec,
    LayerDefinition,
    revision,
)

OP_PROPERTIES = "properties"
OP_CATEGORY = "category"
OP_DELETE = "delete"

UPDATE = "features.update"
DELETE = "features.delete"

EDITING_DISABLED = "EDITING_DISABLED"
NOT_EDITABLE = "NOT_EDITABLE"
FEATURE_NOT_FOUND = "FEATURE_NOT_FOUND"
NOT_PERMITTED = "NOT_PERMITTED"
CONFLICT = "CONFLICT"
INVALID_VALUE = "INVALID_VALUE"

# Codes after which the plugin cannot tell whether the edit was applied.
UNCONFIRMED_CODES = frozenset(
    {"TIMEOUT", "CONNECTION_CLOSED", "CLIENT_ERROR", "INTERNAL_ERROR"}
)

# The server accepts these, but the plugin's first cut leaves them read-only.
PLUGIN_READ_ONLY_KINDS = (KIND_DATETIME, KIND_STRING_LIST, KIND_MAP)

# Discard kinds, in the order their messages are shown.
CREATED = "created"
SCHEMA = "schema"
GEOMETRY = "geometry"
LOCKED = "locked"
NOT_IN_CATEGORY = "not-in-category"
UNSENDABLE_VALUE = "value"
NO_CATEGORY = "no-category"
CATEGORY_LOCKED = "category"
DELETE_LOCKED = "delete"

UNSENDABLE = object()
UNCHANGED = object()


@dataclass(frozen=True)
class RowBase:
    """A row as it was when first changed in the edit session."""

    doc_id: str
    version: str
    forks: tuple[str, ...] = ()
    category_id: str | None = None
    editable: bool = False

    @property
    def revision(self) -> str:
        return revision(self.version, self.forks)


@dataclass
class RowChange:
    """One feature's edits. `values` maps column name to the new value, JSON
    ready, or UNSENDABLE; `category_id` is UNCHANGED unless the category moved."""

    base: RowBase
    values: dict[str, Any] = field(default_factory=dict)
    category_id: Any = UNCHANGED
    geometry: bool = False
    deleted: bool = False


@dataclass(frozen=True)
class EditRequest:
    method: str
    params: Mapping[str, Any]
    base: RowBase

    @property
    def doc_id(self) -> str:
        return self.base.doc_id


@dataclass(frozen=True)
class Discard:
    kind: str
    column: str | None = None
    count: int = 1


@dataclass(frozen=True)
class SavePlan:
    requests: tuple[EditRequest, ...]
    discards: tuple[Discard, ...]


def column_offered(definition: LayerDefinition, column: ColumnSpec) -> bool:
    """Whether the plugin lets this column be changed on this layer at all."""
    return (
        OP_PROPERTIES in definition.edit_operations
        and column.editable
        and column.kind not in PLUGIN_READ_ONLY_KINDS
    )


def column_fits(column: ColumnSpec, category_id: str | None) -> bool:
    """Whether a feature of this category can be given a value in the column."""
    if column.category_ids is None:
        return True
    return category_id is not None and category_id in column.category_ids


def plan_save(
    definition: LayerDefinition,
    project_id: str,
    changes: Iterable[RowChange],
    *,
    created: int = 0,
    schema_changed: bool = False,
    lang: str | None = None,
) -> SavePlan:
    """One request per feature that has something to send. `definition` is the
    layer as the connected server describes it now."""
    ops = definition.edit_operations
    counts: dict[tuple[str, str | None], int] = {}

    def drop(kind: str, column: str | None = None) -> None:
        counts[(kind, column)] = counts.get((kind, column), 0) + 1

    if created:
        counts[(CREATED, None)] = created
    if schema_changed:
        drop(SCHEMA)
    requests = []
    for change in changes:
        base = change.base
        params: dict[str, Any] = {
            "projectId": project_id,
            "layerId": definition.layer_id,
            "id": base.doc_id,
            "version": base.version,
            "forks": list(base.forks),
        }
        if lang:
            params["lang"] = lang
        if change.deleted:
            if OP_DELETE in ops:
                requests.append(EditRequest(DELETE, params, base))
            else:
                drop(DELETE_LOCKED)
            continue
        if change.geometry:
            drop(GEOMETRY)
        category = base.category_id
        new_category = change.category_id
        if new_category is not UNCHANGED and new_category != base.category_id:
            if OP_CATEGORY not in ops:
                drop(CATEGORY_LOCKED)
            elif new_category is None:
                drop(NO_CATEGORY)
            else:
                params["categoryId"] = new_category
                category = new_category
        properties = {}
        for name, value in change.values.items():
            column = definition.column(name)
            if column is None or not column_offered(definition, column):
                drop(LOCKED, name)
            elif not column_fits(column, category):
                drop(NOT_IN_CATEGORY, name)
            elif value is UNSENDABLE:
                drop(UNSENDABLE_VALUE, name)
            else:
                properties[name] = value
        if properties:
            params["properties"] = properties
        if "categoryId" in params or properties:
            requests.append(EditRequest(UPDATE, params, base))
    order = [
        CREATED,
        SCHEMA,
        GEOMETRY,
        DELETE_LOCKED,
        CATEGORY_LOCKED,
        NO_CATEGORY,
        LOCKED,
        NOT_IN_CATEGORY,
        UNSENDABLE_VALUE,
    ]
    discards = tuple(
        Discard(kind, column, count)
        for (kind, column), count in sorted(
            counts.items(), key=lambda kv: (order.index(kv[0][0]), kv[0][1] or "")
        )
    )
    return SavePlan(tuple(requests), discards)


def landed(request: EditRequest, feature: Any) -> bool:
    """Whether a feature fetched after a lost reply shows the request applied."""
    if request.method == DELETE:
        return feature is None
    if not isinstance(feature, dict) or feature.get("version") == request.base.version:
        return False
    if "categoryId" in request.params and (
        feature.get("categoryId") != request.params["categoryId"]
    ):
        return False
    shown = feature.get("properties")
    shown = shown if isinstance(shown, dict) else {}
    for name, sent in (request.params.get("properties") or {}).items():
        if _comparable(shown.get(name)) != _comparable(sent):
            return False
    return True


def _comparable(value: Any) -> Any:
    # The server trims strings, as CoMapeo Desktop does.
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return value


# Words


def read_only_message(definition: LayerDefinition | None) -> str:
    """Why a layer cannot be edited, from the connected server's description."""
    if definition is None:
        return "Editing needs CoMapeo Desktop to be connected."
    if definition.editable is None:
        return (
            "This version of CoMapeo Desktop cannot be edited from QGIS. "
            "Update CoMapeo Desktop."
        )
    reason = definition.read_only_reason
    if reason == "disabled":
        return "Editing from QGIS is switched off in CoMapeo Desktop."
    if reason == "unsupported":
        return f"{definition.title} cannot be edited from QGIS."
    if reason == "role":
        return "Your role in this project does not allow editing."
    return f"{definition.title} cannot be edited from QGIS."


def _label(labels: Mapping[str, str], column: str | None) -> str:
    if column is None:
        return "A column"
    return labels.get(column) or column


def discard_messages(
    discards: Sequence[Discard], labels: Mapping[str, str]
) -> list[str]:
    lines = []
    for d in discards:
        name = _label(labels, d.column)
        many = "s" if d.count != 1 else ""
        if d.kind == CREATED:
            lines.append(
                f"{d.count} new feature{many} discarded: observations cannot be "
                "created from QGIS."
            )
        elif d.kind == SCHEMA:
            lines.append(
                "Column changes are not supported on CoMapeo layers and were discarded."
            )
        elif d.kind == GEOMETRY:
            lines.append(
                f"Change of location discarded for {d.count} observation{many}: "
                "points cannot be moved from QGIS yet."
            )
        elif d.kind == DELETE_LOCKED:
            lines.append(
                f"{d.count} deletion{many} discarded: this layer does not allow "
                "deleting."
            )
        elif d.kind == CATEGORY_LOCKED:
            lines.append(
                f"Change of category discarded for {d.count} observation{many}: "
                "this layer does not allow changing the category."
            )
        elif d.kind == NO_CATEGORY:
            lines.append(
                f"Change of category discarded for {d.count} observation{many}: "
                "a category cannot be removed, only changed."
            )
        elif d.kind == LOCKED:
            lines.append(f"Change to {name} discarded: that column cannot be edited.")
        elif d.kind == NOT_IN_CATEGORY:
            lines.append(
                f"Change to {name} discarded for {d.count} observation{many}: it is "
                "not a field of their category."
            )
        elif d.kind == UNSENDABLE_VALUE:
            lines.append(f"Value for {name} could not be sent.")
    return lines


_VALUE_REASONS = {
    "type": "not a value of this column's type",
    "not-an-option": "not one of the field's options",
    "format": "not a date in a form CoMapeo accepts",
    "out-of-range": "out of range",
}


def refusal_text(code: str, data: Any, labels: Mapping[str, str]) -> str:
    """Why one edit was not saved, from the server's error code and data."""
    data = data if isinstance(data, dict) else {}
    column = data.get("column") if isinstance(data.get("column"), str) else None
    reason = data.get("reason")
    target = data.get("target")
    if code == CONFLICT:
        return "changed in CoMapeo since QGIS last updated it"
    if code == FEATURE_NOT_FOUND:
        return "it was deleted in CoMapeo"
    if code == NOT_PERMITTED:
        if reason == "not-own":
            return (
                "created on another device; your role lets you change only "
                "observations created on this device"
            )
        if reason == "role":
            return "your role in this project does not allow editing"
        return "not permitted"
    if code == EDITING_DISABLED:
        return "editing from QGIS is switched off in CoMapeo Desktop"
    if code == NOT_EDITABLE:
        if reason == "not-in-category":
            name = _label(labels, column)
            return f"{name} is not a field of this observation's category"
        if reason == "no-location":
            return "this observation has no location"
        if target == "column":
            return f"{_label(labels, column)} cannot be edited"
        if target == "category":
            return "the category cannot be changed"
        if target == "geometry":
            return "points cannot be moved"
        if target == "delete":
            return "observations cannot be deleted"
        return "this cannot be edited"
    if code == INVALID_VALUE:
        if reason == "would-change-category":
            what = _label(labels, column) + ": this value" if column else "These values"
            return (
                f"{what} would change the observation's category; change the "
                "category instead"
            )
        if reason == "category-would-not-show":
            return (
                "the category could not be changed because the observation's "
                "other tags still match another category"
            )
        if reason == "unknown-category":
            return "that category is no longer in the project"
        if reason == "category-not-for-layer":
            return "that category cannot be used here"
        if reason == "schema":
            detail = data.get("detail")
            extra = f" ({detail})" if isinstance(detail, str) and detail else ""
            return f"CoMapeo did not accept the observation{extra}"
        words = _VALUE_REASONS.get(reason, "value not accepted")
        if target == "column" or column:
            return f"{_label(labels, column)}: value not accepted, {words}"
        return f"value not accepted, {words}"
    if code == "METHOD_NOT_FOUND":
        return "this version of CoMapeo Desktop cannot save edits from QGIS"
    return f"refused ({code})"


def summary_text(saved: int, refused: int, unconfirmed: int = 0) -> str:
    if not refused and not unconfirmed:
        if saved == 1:
            return "1 change saved to CoMapeo."
        return f"{saved} changes saved to CoMapeo."
    parts = [f"{saved} saved", f"{refused} not saved"]
    if unconfirmed:
        parts.append(f"{unconfirmed} not confirmed")
    return ", ".join(parts) + ". The layer shows what CoMapeo holds."


CONCURRENT_TEXT = (
    "Saved, but another change was made at the same moment. The layer shows "
    "the version CoMapeo kept."
)
KEPT = "Your changes are still in the edit session."
NOT_CONNECTED_TEXT = (
    "Not saved: CoMapeo Desktop is not connected. Your changes are still in the "
    "edit session; open CoMapeo Desktop and save again."
)
CONNECTING_TEXT = (
    "Not saved: still connecting to CoMapeo Desktop. Your changes are still in "
    "the edit session; save again in a moment."
)
INTERNAL_TEXT = (
    "Not saved because of a problem in the CoMapeo plugin (see the log). Your "
    "changes are still in the edit session."
)
PAUSED_TEXT = "Live updates to this layer are paused while it has unsaved edits."


def waiting_text(count: int) -> str:
    many = "s" if count != 1 else ""
    return f"Still waiting for CoMapeo Desktop to confirm {count} change{many}."


def busy_text(count: int) -> str:
    many = "s" if count != 1 else ""
    return (
        f"Still waiting for CoMapeo Desktop to confirm {count} earlier change{many}. "
        "Your new changes are still in the edit session; save again in a moment."
    )


def lost_text(count: int) -> str:
    many = "s" if count != 1 else ""
    return (
        f"CoMapeo Desktop closed before confirming {count} change{many}. The plugin "
        "checks them once it is connected again."
    )


def checked_text(saved: int, missing: int) -> str:
    return (
        f"Of the changes CoMapeo Desktop had not confirmed, {saved} were saved and "
        f"{missing} were not."
    )


def large_save_question(count: int) -> str:
    return (
        f"Save {count} changes to CoMapeo? QGIS will wait while CoMapeo Desktop "
        "applies them."
    )


def describe_request(request: EditRequest, labels: Mapping[str, str]) -> str:
    """What a request asked for, for the list of changes that did not land."""
    if request.method == DELETE:
        return "delete"
    parts = []
    if "categoryId" in request.params:
        parts.append("category")
    for name, value in (request.params.get("properties") or {}).items():
        shown = "no value" if value is None else repr(value)
        parts.append(f"{_label(labels, name)} = {shown}")
    return ", ".join(parts)
