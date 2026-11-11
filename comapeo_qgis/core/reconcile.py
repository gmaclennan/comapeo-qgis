"""Work out what to write to the local cache from fetched results."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .model import Category, FeaturePage, FeatureRecord, LayerDefinition, Project


@dataclass(frozen=True)
class SyncPlan:
    """`deletes_withheld` is True when some page was malformed or had features
    without an id or version, so ids missing from the fetch were not deleted."""

    upserts: tuple[FeatureRecord, ...] = ()
    deletes: tuple[str, ...] = ()
    unchanged: int = 0
    deletes_withheld: bool = False

    @property
    def empty(self) -> bool:
        return not self.upserts and not self.deletes


class ReconcileError(Exception):
    pass


class Reconciler:
    """Diff one fetch (all its pages) against the cache.

    `cached` maps feature id to the version stored. Without `ids` this is a
    full fetch: ids absent from every page are deleted, but only by `finish()`,
    once the last page has arrived. With `ids` (a change notification) only
    those ids are touched. Apply each returned plan in order. If the server
    answers INVALID_CURSOR, or the connection drops mid-fetch, drop this
    instance and start a new one from the cache as it now stands.

    A page that is not `valid`, or that skipped features, makes `finish()`
    withhold the deletes it would otherwise infer; ids the server listed in
    `removed` are still deleted.

    `refresh_all` (the default) upserts every returned feature even when its
    version is unchanged, because server-derived values (category name, device
    names, translated labels) change without a new document version. Pass
    False only where stale derived values are acceptable.
    """

    def __init__(
        self,
        cached: Mapping[str, str],
        ids: Iterable[str] | None = None,
        *,
        refresh_all: bool = True,
    ) -> None:
        self._cached = dict(cached)
        self._requested = None if ids is None else set(ids)
        self._refresh_all = refresh_all
        self._seen: set[str] = set()
        self._finished = False
        self._complete = False
        self._doubtful = False

    @property
    def full(self) -> bool:
        return self._requested is None

    def add_page(self, page: FeaturePage) -> SyncPlan:
        if self._finished:
            raise ReconcileError("fetch already finished")
        if not page.valid or page.skipped:
            self._doubtful = True
        upserts: dict[str, FeatureRecord] = {}
        unchanged = 0
        for feature in page.features:
            if self._requested is not None and feature.id not in self._requested:
                continue
            self._seen.add(feature.id)
            if (
                not self._refresh_all
                and self._cached.get(feature.id) == feature.version
            ):
                if feature.id not in upserts:
                    unchanged += 1
                continue
            upserts[feature.id] = feature
            self._cached[feature.id] = feature.version
        deletes = []
        if self._requested is not None:
            for removed in page.removed:
                if removed in self._requested and removed not in self._seen:
                    self._seen.add(removed)
                    if self._cached.pop(removed, None) is not None:
                        deletes.append(removed)
        self._complete = page.is_last
        return SyncPlan(tuple(upserts.values()), tuple(deletes), unchanged)

    def finish(self) -> SyncPlan:
        """Call after the page whose `next_cursor` is None."""
        if self._finished:
            raise ReconcileError("fetch already finished")
        if not self._complete:
            raise ReconcileError("last page not received; cannot compute deletes")
        self._finished = True
        if self._doubtful:
            return SyncPlan(deletes_withheld=True)
        scope = self._cached.keys() if self._requested is None else self._requested
        deletes = tuple(
            sorted(i for i in scope if i not in self._seen and i in self._cached)
        )
        for feature_id in deletes:
            del self._cached[feature_id]
        return SyncPlan(deletes=deletes)


def reconcile(
    cached: Mapping[str, str],
    pages: Sequence[FeaturePage],
    ids: Iterable[str] | None = None,
    *,
    refresh_all: bool = True,
) -> SyncPlan:
    """One plan for a complete set of pages."""
    reconciler = Reconciler(cached, ids, refresh_all=refresh_all)
    upserts: dict[str, FeatureRecord] = {}
    deletes: list[str] = []
    unchanged = 0
    for page in pages:
        plan = reconciler.add_page(page)
        for feature in plan.upserts:
            upserts[feature.id] = feature
        deletes.extend(plan.deletes)
        unchanged += plan.unchanged
    last = reconciler.finish()
    deletes.extend(last.deletes)
    for feature_id in deletes:
        upserts.pop(feature_id, None)
    return SyncPlan(
        tuple(upserts.values()),
        tuple(deletes),
        unchanged,
        deletes_withheld=last.deletes_withheld,
    )


def layer_schema_key(
    layer: LayerDefinition,
    lang: str | None,
    categories: Sequence[Category] | None = None,
) -> str:
    """Store this with a cached layer; a different key means rebuild it.

    Labels and display values depend on the language without changing feature
    versions, so the language is part of the key. The server's schemaVersion
    covers columns only, so pass `categories` to also rebuild when categories
    were renamed, recoloured or given new icons while QGIS was disconnected.
    """
    if layer.schema_version is not None:
        schema = f"v:{layer.schema_version}"
    else:
        columns = [[c.name, c.type, c.role] for c in layer.columns]
        schema = f"c:{_digest(columns)}"
    key: list[str] = [schema, layer.geometry_type, lang or ""]
    if categories is not None:
        key.append(f"k:{categories_digest(categories)}")
    return json.dumps(key)


def categories_digest(categories: Sequence[Category]) -> str:
    entries = sorted(
        [
            c.category_id,
            c.name,
            c.color or "",
            "" if c.icon is None else c.icon.mime_type,
            "" if c.icon is None else hashlib.sha256(c.icon.data).hexdigest(),
        ]
        for c in categories
    )
    return _digest(entries)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class LayerPlan:
    added: tuple[LayerDefinition, ...] = ()
    rebuilt: tuple[LayerDefinition, ...] = ()
    unchanged: tuple[LayerDefinition, ...] = ()
    removed: tuple[str, ...] = ()


def plan_layers(
    cached: Mapping[str, str],
    layers: Sequence[LayerDefinition],
    lang: str | None,
    categories: Sequence[Category] | None = None,
) -> LayerPlan:
    """`cached` maps layer id to the `layer_schema_key` stored with it.

    Pass the same `categories` argument as when the keys were stored.
    """
    added, rebuilt, unchanged = [], [], []
    for layer in layers:
        if layer.layer_id not in cached:
            added.append(layer)
        elif cached[layer.layer_id] != layer_schema_key(layer, lang, categories):
            rebuilt.append(layer)
        else:
            unchanged.append(layer)
    offered = {layer.layer_id for layer in layers}
    removed = tuple(sorted(i for i in cached if i not in offered))
    return LayerPlan(tuple(added), tuple(rebuilt), tuple(unchanged), removed)


@dataclass(frozen=True)
class ProjectPlan:
    """`gone`: cached projects no longer offered by the app (left, or removed)."""

    added: tuple[Project, ...] = ()
    kept: tuple[Project, ...] = ()
    gone: tuple[str, ...] = ()


def plan_projects(cached: Iterable[str], projects: Sequence[Project]) -> ProjectPlan:
    """Compare cached project ids with a `projects.list` result."""
    known = set(cached)
    added = tuple(p for p in projects if p.project_id not in known)
    kept = tuple(p for p in projects if p.project_id in known)
    offered = {p.project_id for p in projects}
    return ProjectPlan(added, kept, tuple(sorted(known - offered)))
