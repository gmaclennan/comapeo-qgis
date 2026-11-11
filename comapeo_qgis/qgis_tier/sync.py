"""Keep one project's cache in step with the app: full refreshes and live changes."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from qgis.PyQt.QtCore import QObject, pyqtSignal

from ..core.model import (
    LayerDefinition,
    parse_categories,
    parse_feature_page,
    parse_layer_changed,
    parse_layers,
    parse_schema_changed,
)
from ..core.protocol import (
    CONNECTION_CLOSED,
    INVALID_CURSOR,
    LAYER_NOT_FOUND,
    PROJECT_NOT_FOUND,
    RequestError,
)
from ..core.reconcile import Reconciler, layer_schema_key
from .cache import CacheError, ProjectCache, TableInfo
from .transport import LocalApiClient

PAGE_LIMIT = 1000
IDS_PER_REQUEST = 1000
MAX_CURSOR_RESTARTS = 3


class _Fetch:
    """`replaces` is set when `info` is a staged table to commit once full."""

    def __init__(
        self,
        info: TableInfo,
        ids: list[str] | None,
        generation: int,
        cached: dict,
        replaces: TableInfo | None = None,
    ) -> None:
        self.info = info
        self.definition: LayerDefinition = info.definition
        self.ids = ids
        self.generation = generation
        self.replaces = replaces
        self.reconciler = Reconciler(cached, ids)
        self.cursor: Any = None
        self.cursors: set[str] = set()
        self.restarts = 0


class ProjectSync(QObject):
    """Owns the cache of one project while an app that has it is connected.

    Work is one fetch per layer at a time. Ids from `layer.changed` that arrive
    during a fetch are kept and fetched after it; a schema change or manual
    refresh starts a new generation and abandons fetches of the old one. A
    rebuilt table is filled while map layers stay on the old one, and the
    switch (`layerRebuilt`) happens only once the new table is complete.
    """

    # `layerRebuilt(layer_id, old_category_field, old_table)`: a table was
    # (re)created; `old_table` is the one map layers were on ("" if none).
    layerDataChanged = pyqtSignal(str)
    schemaLoaded = pyqtSignal(object, object)
    layerRebuilt = pyqtSignal(str, str, str)
    categoriesChanged = pyqtSignal()
    statusChanged = pyqtSignal()
    projectGone = pyqtSignal()

    def __init__(
        self,
        cache: ProjectCache,
        lang: Callable[[], str | None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.cache = cache
        self.project_id = cache.project_id
        self._lang = lang
        self._client: LocalApiClient | None = None
        self._generation = 0
        self._schema_pending = 0
        self._fetches: dict[str, _Fetch] = {}
        self._pending_ids: dict[str, set[str]] = {}
        self._pending_full: set[str] = set()
        self._staged: dict[str, tuple[TableInfo, TableInfo]] = {}
        self._subscribed = False
        self._subscribing = False
        # Layers already refetched once after LAYER_NOT_FOUND; a second one fails.
        self._missing: set[str] = set()
        self.error: str | None = None
        self.warning: str | None = None
        self.refreshing = False

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_ready

    @property
    def busy(self) -> bool:
        return self.refreshing or bool(self._fetches)

    def attach(self, client: LocalApiClient) -> None:
        self._client = client
        self._subscribed = False
        self._subscribing = False
        self._missing.clear()
        self._subscribe()

    def detach(self) -> None:
        self._client = None
        self._subscribed = False
        self._subscribing = False
        self._generation += 1
        self._fetches.clear()
        self._pending_full.clear()
        self._staged.clear()
        self.refreshing = False
        self.statusChanged.emit()

    def close(self) -> None:
        client = self._client
        self.detach()
        if client is not None and client.is_ready:
            client.request("unsubscribe", {"projectId": self.project_id})
        self.cache.close()

    def refresh(self) -> None:
        """Re-read layer definitions and categories, then refetch every layer.

        Subscribes first if an earlier subscribe failed."""
        if not self.connected:
            return
        self._missing.clear()
        if not self._subscribed:
            self._subscribe()
            return
        self._restart()

    def _subscribe(self) -> None:
        if self._subscribing or not self.connected:
            return
        self._subscribing = True
        self.refreshing = True
        self.error = None
        self.statusChanged.emit()
        self._request(
            "subscribe", {"projectId": self.project_id}, self._on_subscribed, 0
        )

    def _restart(self) -> None:
        if not self.connected:
            return
        self._generation += 1
        generation = self._generation
        self._fetches.clear()
        self._pending_full.clear()
        self._staged.clear()
        self.refreshing = True
        self.error = None
        self.warning = None
        self.statusChanged.emit()
        params = {"projectId": self.project_id}
        lang = self._lang()
        if lang:
            params["lang"] = lang
        replies: dict[str, Any] = {}

        def got(name: str) -> Callable[[Any, RequestError | None], None]:
            def callback(result: Any, error: RequestError | None) -> None:
                if generation != self._generation:
                    return
                if error is not None:
                    self._fail(error)
                    return
                replies[name] = result
                if len(replies) == 2:
                    self._on_schema(replies["layers"], replies["categories"])

            return callback

        self._request("layers.list", params, got("layers"), generation)
        self._request("categories.list", params, got("categories"), generation)

    def handle_notification(self, method: str, params: Any) -> None:
        if method == "layer.changed":
            changed = parse_layer_changed(params)
            if changed is None or changed.project_id != self.project_id:
                return
            if not changed.ids:
                return
            if self.cache.table(changed.layer_id) is None:
                return
            self._pending_ids.setdefault(changed.layer_id, set()).update(changed.ids)
            self._next(changed.layer_id)
        elif method == "layer.schemaChanged":
            changed = parse_schema_changed(params)
            if changed is not None and changed.project_id == self.project_id:
                self.refresh()

    def _on_subscribed(self, _result: Any, error: RequestError | None) -> None:
        self._subscribing = False
        if error is not None:
            self._fail(error)
            return
        self._subscribed = True
        self._restart()

    def _on_schema(self, layers_result: Any, categories_result: Any) -> None:
        self.schemaLoaded.emit(layers_result, categories_result)
        lang = self._lang()
        categories = parse_categories(categories_result)
        raw_by_id = {
            item.get("layerId"): item
            for item in (layers_result or {}).get("layers", [])
            if isinstance(item, dict)
        }
        try:
            if categories_result != self.cache.raw_categories():
                self.cache.set_categories(categories_result)
                categories_changed = True
            else:
                categories_changed = False
            layer_ids = []
            for layer in parse_layers(layers_result):
                if not layer.geometry_supported:
                    continue
                old = self.cache.table(layer.layer_id)
                key = layer_schema_key(layer, lang, categories)
                info, created = self.cache.ensure_table(
                    raw_by_id[layer.layer_id], key, stage=True
                )
                committed = self.cache.table(layer.layer_id)
                if created and committed is not None and committed.table != info.table:
                    self._staged[layer.layer_id] = (info, committed)
                elif created:
                    old_field = old.field("category_id") if old else ""
                    old_table = old.table if old else ""
                    self.layerRebuilt.emit(layer.layer_id, old_field, old_table)
                layer_ids.append(layer.layer_id)
            for layer_id in list(self._pending_ids):
                if layer_id not in layer_ids:
                    del self._pending_ids[layer_id]
        except CacheError as err:
            self.refreshing = False
            self.error = str(err)
            self.statusChanged.emit()
            return
        if categories_changed:
            self.categoriesChanged.emit()
        self.refreshing = False
        for layer_id in layer_ids:
            self._pending_full.add(layer_id)
            self._next(layer_id)
        self.statusChanged.emit()

    def _next(self, layer_id: str) -> None:
        if not self.connected or self.refreshing or layer_id in self._fetches:
            return
        info = self.cache.table(layer_id)
        replaces = None
        if layer_id in self._pending_full and layer_id in self._staged:
            info, replaces = self._staged[layer_id]
        if info is None:
            return
        if layer_id in self._pending_full:
            self._pending_full.discard(layer_id)
            ids = None
        elif self._pending_ids.get(layer_id):
            pending = sorted(self._pending_ids.pop(layer_id))
            ids, rest = pending[:IDS_PER_REQUEST], pending[IDS_PER_REQUEST:]
            if rest:
                self._pending_ids[layer_id] = set(rest)
        else:
            return
        fetch = _Fetch(info, ids, self._generation, self.cache.versions(info), replaces)
        self._fetches[layer_id] = fetch
        self.statusChanged.emit()
        self._page(fetch)

    def _page(self, fetch: _Fetch) -> None:
        params: dict[str, Any] = {
            "projectId": self.project_id,
            "layerId": fetch.info.layer_id,
            "limit": PAGE_LIMIT,
        }
        lang = self._lang()
        if lang:
            params["lang"] = lang
        if fetch.cursor is not None:
            params["cursor"] = fetch.cursor
        if fetch.ids is not None:
            params["ids"] = fetch.ids
        self._request(
            "layers.getFeatures",
            params,
            lambda result, error: self._on_page(fetch, result, error),
            fetch.generation,
        )

    def _on_page(self, fetch: _Fetch, result: Any, error: RequestError | None) -> None:
        layer_id = fetch.info.layer_id
        if self._fetches.get(layer_id) is not fetch:
            return
        if error is not None:
            del self._fetches[layer_id]
            if error.code == INVALID_CURSOR and fetch.restarts < MAX_CURSOR_RESTARTS:
                again = _Fetch(
                    fetch.info,
                    fetch.ids,
                    fetch.generation,
                    self.cache.versions(fetch.info),
                    fetch.replaces,
                )
                again.restarts = fetch.restarts + 1
                self._fetches[layer_id] = again
                self._page(again)
                return
            if fetch.ids is not None:
                self._pending_ids.setdefault(layer_id, set()).update(fetch.ids)
            if error.code == LAYER_NOT_FOUND and layer_id not in self._missing:
                self._missing.add(layer_id)
                self._restart()
                return
            self._fail(error)
            return
        page = parse_feature_page(result, fetch.definition)
        try:
            touched = self.cache.apply(fetch.info, fetch.reconciler.add_page(page))
            if page.is_last:
                last = fetch.reconciler.finish()
                touched += self.cache.apply(fetch.info, last)
                if last.deletes_withheld:
                    self.warning = (
                        "CoMapeo sent some features this plugin could not read, so "
                        "features missing from the reply were kept."
                    )
                if fetch.ids is None and fetch.replaces is None:
                    self.cache.mark_refreshed(layer_id)
                elif fetch.ids is None:
                    self.cache.commit_table(fetch.info)
        except CacheError as err:
            del self._fetches[layer_id]
            self.error = str(err)
            self.statusChanged.emit()
            return
        if page.is_last and fetch.replaces is not None:
            self._switch(fetch.info, fetch.replaces)
        elif touched and fetch.replaces is None:
            self.layerDataChanged.emit(layer_id)
        if not page.is_last:
            cursor = json.dumps(page.next_cursor, sort_keys=True)
            if cursor in fetch.cursors:
                del self._fetches[layer_id]
                self.error = (
                    "CoMapeo Desktop sent the same page twice, so the plugin "
                    "stopped reading. Try Refresh later."
                )
                self.statusChanged.emit()
                return
            fetch.cursors.add(cursor)
            fetch.cursor = page.next_cursor
            self._page(fetch)
            return
        del self._fetches[layer_id]
        self._missing.discard(layer_id)
        self.statusChanged.emit()
        self._next(layer_id)

    def _switch(self, info: TableInfo, old: TableInfo) -> None:
        self._staged.pop(info.layer_id, None)
        self.layerRebuilt.emit(info.layer_id, old.field("category_id"), old.table)
        try:
            self.cache.drop_table(old.table)
        except CacheError as err:
            self.warning = str(err)

    def _request(
        self,
        method: str,
        params: dict[str, Any],
        callback: Callable[[Any, RequestError | None], None],
        generation: int,
    ) -> None:
        client = self._client
        if client is None or not client.is_ready:
            return

        def guarded(result: Any, error: RequestError | None) -> None:
            if self._client is not client:
                return
            if generation and generation != self._generation:
                return
            callback(result, error)

        client.request(method, params, guarded)

    def _fail(self, error: RequestError) -> None:
        self.refreshing = False
        if error.code == CONNECTION_CLOSED:
            return
        if error.code == PROJECT_NOT_FOUND:
            self.error = "This project is no longer available in CoMapeo Desktop."
            self.statusChanged.emit()
            self.projectGone.emit()
            return
        self.error = f"CoMapeo Desktop could not send the data ({error.code}). "
        self.error += "Try Refresh later."
        self.statusChanged.emit()
