"""Ties the connector, the per-project syncs, the cache and the map layers together."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsFeatureRequest,
    QgsMapLayer,
    QgsMessageLog,
    QgsProject,
    QgsSettings,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication, QEvent, QObject, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import QWidget

from ..core.editing import PAUSED_TEXT, read_only_message
from ..core.media import MediaCache
from ..core.model import (
    GEOMETRY_LINESTRING,
    Attachment,
    LayerDefinition,
    Project,
    parse_attachments,
    parse_categories,
    parse_layers,
    parse_projects,
)
from ..core.reconcile import categories_digest, layer_schema_key
from .cache import (
    CacheError,
    CacheLocked,
    ProjectCache,
    TableInfo,
    cached_files,
    default_cache_root,
    is_cache_source,
    project_in_file,
    table_from_source,
)
from .connection import POLL_INTERVAL_MS, Connector, ServerState, ServerStatus
from .editing import EditHook
from .layers import (
    GROUP_PROP_PROJECT,
    PROP_LAYER,
    comapeo_ids,
    comapeo_layers,
    configure,
    create_layer,
    point_at,
    project_group,
    refresh_not_editable,
)
from .media import MediaDownload, MediaFetcher
from .style import (
    append_new_categories,
    apply_default_style,
    follow_category_field,
)
from .sync import ProjectSync
from .transport import LocalApiClient

CONNECTING_TEXT = "Still connecting to CoMapeo Desktop."
DOWNLOADING_TEXT = (
    "This layer can be edited once it has finished downloading from CoMapeo Desktop."
)
TRANSACTION_TEXT = (
    "CoMapeo layers cannot be edited while the project uses transaction groups."
)

Notify = Callable[[Any, str, str], None]

SETTING_DISCOVERY = "comapeo/discoveryPath"
SETTING_EXTRA_DISCOVERY = "comapeo/extraDiscoveryPaths"
PROJECT_POLL_MS = POLL_INTERVAL_MS


@dataclass(frozen=True)
class ProjectStatus:
    project_id: str
    name: str
    connected: bool
    gone: bool
    busy: bool
    last_refresh: datetime | None
    error: str | None
    warning: str | None
    offered: bool = False
    # Titles of the cached layers that have never finished a full fetch.
    incomplete_layers: tuple[str, ...] = ()
    # The oldest refresh of the layers that have finished one.
    complete_refresh: datetime | None = None
    # The cache file exists but cannot be read.
    damaged: bool = False
    # Another program holds a lock on the cache file, so its details are unknown.
    locked: bool = False
    # Titles of the layers whose live updates wait for an edit session.
    paused_layers: tuple[str, ...] = ()
    # Why the app lets none of the project's editable layers be edited.
    read_only: str | None = None

    @property
    def incomplete(self) -> bool:
        return bool(self.incomplete_layers)

    @property
    def stale(self) -> bool:
        return not self.connected

    def describe(self) -> str:
        when = format_time(self.last_refresh)
        if self.locked and not (self.connected and self.error):
            return (
                "Another program is using the data cached for this project, so it "
                "cannot be read just now. The plugin tries again every few seconds."
            )
        if self.damaged:
            return (
                "The data cached for this project cannot be read. Use “Remove "
                "cached data” to delete it; it is downloaded again once CoMapeo "
                "Desktop is connected."
            )
        if self.gone:
            if self.incomplete and self.complete_refresh is None:
                return (
                    "No longer available in CoMapeo Desktop. The first download "
                    "did not finish, so the cached data is incomplete. You can "
                    "remove the cached data."
                )
            return (
                f"No longer available in CoMapeo Desktop. {self._cached()} You can "
                "remove the cached data."
            )
        if not self.connected:
            if self.offered:
                return "Not on the map."
            if self.incomplete and self.complete_refresh is None:
                return (
                    "Not connected. The first download did not finish, so this "
                    "data is incomplete."
                )
            return f"Not connected. {self._cached()}"
        if self.error:
            return self.error
        if self.paused_layers:
            if len(self.paused_layers) == 1:
                return PAUSED_TEXT.replace("this layer", self.paused_layers[0])
            return "Live updates are paused for layers with unsaved edits."
        text = "Updating…" if self.busy else f"Up to date (last updated {when})."
        return f"{text} {self.read_only}" if self.read_only else text

    def _cached(self) -> str:
        if not self.incomplete:
            return f"Showing data cached {format_time(self.last_refresh)}."
        *rest, last = self.incomplete_layers
        names = f"{', '.join(rest)} and {last}" if rest else last
        which = "they are" if rest else "that layer is"
        return (
            f"Showing data cached {format_time(self.complete_refresh)}. The first "
            f"download of {names} did not finish, so {which} incomplete."
        )


def unloaded_text(layer: QgsVectorLayer) -> str:
    buffer = layer.editBuffer()
    changed = {
        fid
        for fid in (
            *buffer.changedAttributeValues(),
            *buffer.changedGeometries(),
            *buffer.deletedFeatureIds(),
        )
        if fid >= 0
    }
    what = f"{len(changed)} observation{'s' if len(changed) != 1 else ''} in "
    return (
        "The CoMapeo plugin was unloaded, so unsaved changes to "
        f"{what if changed else ''}{layer.name()} were discarded."
    )


def format_time(value: datetime | None) -> str:
    if value is None:
        return "at an unknown time"
    return "on " + value.astimezone().strftime("%Y-%m-%d %H:%M")


def default_lang() -> str | None:
    locale = QgsApplication.locale()
    return locale or None


class Controller(QObject):
    """`changed` fires whenever anything the panel or Browser shows may differ.

    `selectionChanged(project_id, layer_id, doc_ids)` fires when the selection on
    a CoMapeo layer changes. `mediaRetry` fires when media that was not yet
    synced or failed should be fetched again (Refresh, or an app connecting).
    `mediaRemoving(project_id)` fires before a project's media files are deleted.
    `messageShown(level, text, details)` carries every message for the user.
    """

    changed = pyqtSignal()
    selectionChanged = pyqtSignal(str, str, list)
    mediaRetry = pyqtSignal()
    mediaRemoving = pyqtSignal(str)
    downloadsChanged = pyqtSignal()
    messageShown = pyqtSignal(object, str, str)

    def __init__(
        self,
        *,
        project: QgsProject | None = None,
        cache_root: Path | None = None,
        discovery_override: Callable[[], str | None] | None = None,
        discovery_extra: Callable[[], list[str]] | None = None,
        lang: Callable[[], str | None] = default_lang,
        client_version: str = "0.1.0",
        notify: Notify | None = None,
        dialog_parent: Callable[[], QWidget | None] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.project = project or QgsProject.instance()
        self._notify = notify
        self._dialog_parent = dialog_parent
        self._hooks: dict[str, EditHook] = {}
        self._edit_keys: dict[str, Any] = {}
        self._live: dict[str, tuple[Any, dict[str, LayerDefinition]]] = {}
        self.cache_root = Path(cache_root) if cache_root else default_cache_root()
        self._lang = lang
        self.connector = Connector(
            client_version,
            discovery_override or _setting_override,
            self,
            extra=discovery_extra or extra_discovery_locations,
        )
        self._caches: dict[str, ProjectCache] = {}
        self._file_projects: dict[Path, Project] = {}
        self._locked_files: set[Path] = set()
        self._syncs: dict[str, ProjectSync] = {}
        self._projects: dict[str, Project] = {}
        self._served: dict[str, str] = {}
        self._origins: dict[str, str] = {}
        self._listed: set[str] = set()
        self._catalog: dict[str, tuple[Any, Any]] = {}
        self._gone: set[str] = set()
        self._watched: dict[str, QgsVectorLayer] = {}
        self._reloading: set[str] = set()
        self._project_signals: list[tuple[Any, Callable[..., Any]]] = []
        self._started = False
        self.list_errors: dict[str, str] = {}
        self.last_download: tuple[str, Any] | None = None
        self.media = MediaCache(self.cache_root.parent / "media")
        self.fetcher = MediaFetcher(self.media, self)
        self._downloads: dict[str, MediaDownload] = {}
        # The protocol has no notification for projects joined or left.
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._poll_projects)

    # Lifecycle

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.connector.clientReady.connect(self._on_client_ready)
        self.connector.clientLost.connect(self._on_client_lost)
        self.connector.statusChanged.connect(self.changed)
        for signal, slot in (
            (self.project.layersAdded, self._on_layers_added),
            (self.project.layersRemoved, self._on_layers_removed),
            (self.project.readProject, self._on_read_project),
        ):
            signal.connect(slot)
            self._project_signals.append((signal, slot))
        with contextlib.suppress(OSError):
            self.media.remove_partial_files()
        self._on_layers_added(list(self.project.mapLayers().values()))
        self.connector.start()
        self._poll.start(PROJECT_POLL_MS)

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self._poll.stop()
        for download in list(self._downloads.values()):
            download.cancel()
        self.fetcher.abort_all()
        for signal, slot in self._project_signals:
            with contextlib.suppress(TypeError, RuntimeError):
                signal.disconnect(slot)
        self._project_signals.clear()
        for layer in list(self._watched.values()):
            with contextlib.suppress(TypeError, RuntimeError):
                layer.selectionChanged.disconnect(self._on_selection)
        for layer_id, hook in list(self._hooks.items()):
            hook.detach()
            layer = self._watched.get(layer_id)
            # Without the plugin QGIS would write the edits to the cache itself.
            with contextlib.suppress(RuntimeError):
                if layer is not None and layer.isEditable():
                    if layer.isModified():
                        self.notify(Qgis.MessageLevel.Warning, unloaded_text(layer))
                    layer.rollBack()
                if layer is not None:
                    layer.setReadOnly(True)
            hook.deleteLater()
        self._hooks.clear()
        self._edit_keys.clear()
        self._watched.clear()
        for sync in self._syncs.values():
            sync.close()
            sync.deleteLater()
        self._syncs.clear()
        for signal, slot in (
            (self.connector.clientReady, self._on_client_ready),
            (self.connector.clientLost, self._on_client_lost),
            (self.connector.statusChanged, self.changed),
        ):
            with contextlib.suppress(TypeError, RuntimeError):
                signal.disconnect(slot)
        self.connector.stop()
        for cache in self._caches.values():
            cache.close()
        self._caches.clear()
        self._listed.clear()

    # Queries for the panel and Browser

    @property
    def server_statuses(self) -> list[ServerStatus]:
        return self.connector.statuses

    @property
    def connected(self) -> bool:
        return any(s.state is ServerState.CONNECTED for s in self.server_statuses)

    def served_projects(self, build_type: str) -> list[Project]:
        return [
            self._projects[p]
            for p, b in self._served.items()
            if b == build_type and p in self._projects
        ]

    def cached_projects(self) -> list[Project]:
        """Projects with a cache file, including those on the map whose file is
        damaged, so that their cached data can still be removed."""
        projects = []
        for path in cached_files(self.cache_root):
            project = self._project_from_file(path)
            if project is not None:
                projects.append(project)
        listed = {p.project_id for p in projects}
        for project_id in self.active_projects():
            # Damaged, or locked by another program before it was first read.
            if project_id not in listed and self.cache(project_id).exists:
                projects.append(Project(project_id, self.project_name(project_id)))
        return projects

    def project_name(self, project_id: str) -> str:
        project = self._projects.get(project_id)
        if project is None and self.cache(project_id).exists:
            project = self.cache(project_id).project()
        if project is not None and project.name:
            return project.name
        for group in self.project.layerTreeRoot().findGroups(True):
            if group.customProperty(GROUP_PROP_PROJECT) == project_id and group.name():
                return group.name()
        return project_id[:8]

    def available_layers(self, project_id: str) -> list[LayerDefinition]:
        catalog = self._catalog.get(project_id)
        if catalog is not None:
            return [
                layer for layer in parse_layers(catalog[0]) if layer.geometry_supported
            ]
        cache = self.cache(project_id)
        if not cache.exists:
            return []
        return [info.definition for info in cache.tables().values()]

    def is_stale(self, project_id: str) -> bool:
        sync = self._syncs.get(project_id)
        return sync is None or not sync.connected

    def active_projects(self) -> list[str]:
        ids = {comapeo_ids(layer)[0] for layer in comapeo_layers(self.project)}
        return sorted(ids)

    def project_status(self, project_id: str) -> ProjectStatus:
        """The project as a whole; the oldest refresh of any of its layers."""
        cache = self.cache(project_id)
        tables = list(cache.tables().values()) if cache.exists else []
        return self._status(project_id, tables)

    def layer_status(self, project_id: str, layer_id: str) -> ProjectStatus:
        """As project_status, but only this layer's download counts."""
        cache = self.cache(project_id)
        readable = cache.exists and not cache.is_damaged()
        info = cache.table(layer_id) if readable else None
        if info is None:
            return self.project_status(project_id)
        return self._status(project_id, [info])

    def _status(self, project_id: str, tables: list[TableInfo]) -> ProjectStatus:
        sync = self._syncs.get(project_id)
        damaged = self.cache(project_id).is_damaged()
        stamps = []
        incomplete = []
        for info in tables:
            try:
                stamps.append(datetime.fromisoformat(info.last_refresh or ""))
            except (TypeError, ValueError):
                incomplete.append(info.definition.title or info.layer_id)
        return ProjectStatus(
            project_id=project_id,
            name=self.project_name(project_id),
            connected=sync is not None and sync.connected,
            gone=project_id in self._gone,
            busy=sync is not None and sync.busy,
            # The oldest: the data is only as fresh as its least recently fetched layer.
            last_refresh=min(stamps) if stamps and not incomplete else None,
            error=sync.error if sync else None,
            warning=sync.warning if sync else None,
            offered=project_id in self._served and sync is None,
            incomplete_layers=tuple(incomplete),
            complete_refresh=min(stamps) if stamps else None,
            damaged=damaged,
            locked=self.cache(project_id).locked,
            paused_layers=tuple(
                info.definition.title or info.layer_id
                for info in tables
                if sync is not None and sync.held(info.layer_id)
            ),
            read_only=self._read_only(project_id, tables) if sync else None,
        )

    def _read_only(self, project_id: str, tables: list[TableInfo]) -> str | None:
        for info in tables:
            live = self.live_definition(project_id, info.layer_id)
            if live is None or live.editable is True:
                continue
            if live.read_only_reason != "unsupported":
                return read_only_message(live)
        return None

    def cache(self, project_id: str) -> ProjectCache:
        cache = self._caches.get(project_id)
        if cache is None:
            cache = ProjectCache(self.cache_root, project_id)
            self._caches[project_id] = cache
        return cache

    # Actions

    def refresh(self) -> None:
        """Manual refresh: rescan for apps, re-list projects, refetch active ones."""
        self.connector.scan()
        for build_type, client in self.connector.ready_clients().items():
            self._list_projects(build_type, client)
        self._retry_media()

    def _retry_media(self) -> None:
        self.media.retry_not_synced()
        self.fetcher.clear_errors()
        self.mediaRetry.emit()

    # Media

    def media_client(self, project_id: str) -> LocalApiClient | None:
        """The connection to the app serving this project, if it is ready."""
        build_type = self._served.get(project_id)
        client = self.connector.client(build_type) if build_type else None
        return client if client is not None and client.is_ready else None

    def client_for(self, project_id: str) -> LocalApiClient | None:
        return self.media_client(project_id)

    def sync_for(self, project_id: str) -> ProjectSync | None:
        return self._syncs.get(project_id)

    def resolved_ids(self, layer: QgsMapLayer) -> tuple[str, str] | None:
        return self._resolved_ids(layer)

    def lang(self) -> str | None:
        return self._lang()

    def dialog_parent(self) -> QWidget | None:
        return self._dialog_parent() if self._dialog_parent else None

    def notify(self, level: Any, text: str, details: str = "") -> None:
        """A message for the user: logged, and shown by the plugin if it can."""
        QgsMessageLog.logMessage(
            f"{text}\n{details}" if details else text, "CoMapeo", level
        )
        if self._notify is not None:
            self._notify(level, text, details)
        self.messageShown.emit(level, text, details)

    # Editing

    def live_definition(self, project_id: str, layer_id: str) -> LayerDefinition | None:
        """The layer as the connected app last described it; None until it has."""
        catalog = self._catalog.get(project_id)
        if catalog is None:
            return None
        known = self._live.get(project_id)
        if known is None or known[0] is not catalog[0]:
            parsed = {d.layer_id: d for d in parse_layers(catalog[0])}
            known = (catalog[0], parsed)
            self._live[project_id] = known
        return known[1].get(layer_id)

    def edit_state(
        self, layer: QgsMapLayer
    ) -> tuple[bool, str | None, LayerDefinition | None]:
        """Whether the plugin lets this map layer be edited now, why not, and the
        layer as the connected app describes it."""
        ids = self._resolved_ids(layer)
        if ids is None:
            return False, None, None
        project_id, layer_id = ids
        sync = self._syncs.get(project_id)
        if sync is None or not sync.connected or self.client_for(project_id) is None:
            return False, read_only_message(None), None
        live = self.live_definition(project_id, layer_id)
        if live is None:
            return False, CONNECTING_TEXT, None
        if live.editable is not True:
            return False, read_only_message(live), live
        cache = self.cache(project_id)
        info = cache.table(layer_id) if cache.exists else None
        if info is None or not info.editable_format or not info.last_refresh:
            return False, DOWNLOADING_TEXT, live
        if self.project.transactionMode() != Qgis.TransactionMode.Disabled:
            return False, TRANSACTION_TEXT, live
        return True, None, live

    def read_only_reason(self, layer: QgsMapLayer) -> str | None:
        """Why a map layer cannot be edited while its app is connected."""
        ids = self._resolved_ids(layer)
        sync = self._syncs.get(ids[0]) if ids is not None else None
        if sync is None or not sync.connected:
            return None
        allowed, message, live = self.edit_state(layer)
        return None if allowed or live is None else message

    def update_editing(self, layer: QgsVectorLayer, force: bool = False) -> None:
        """Lock or unlock the layer and its columns for the current state."""
        ids = self._resolved_ids(layer)
        if ids is None:
            return
        project_id, layer_id = ids
        cache = self.cache(project_id)
        info = cache.table(layer_id) if cache.exists else None
        if info is None:
            return
        allowed, message, live = self.edit_state(layer)
        categories = cache.categories() if allowed else []
        key = (
            allowed,
            info.table,
            repr(live) if allowed else None,
            categories_digest(categories) if allowed else None,
        )
        if layer.isEditable():
            hook = self._hooks.get(layer.id())
            # Only what the app says: a dropped connection usually comes back.
            if hook is not None and live is not None and live.editable is not True:
                hook.editing_lost(message)
            # Locking columns mid-session would strand the user's edits; the save
            # is refused instead. A rebuild still needs its columns unlocked.
            if not allowed:
                return
        if not force and self._edit_keys.get(layer.id()) == key:
            return
        self._edit_keys[layer.id()] = key
        configure(layer, project_id, info, live if allowed else None, categories)

    def _refresh_editing(self, project_id: str) -> None:
        for layer in self._layers_of(project_id):
            self.update_editing(layer)
        for layer_id, hook in self._hooks.items():
            if hook.project_id == project_id and layer_id in self._watched:
                hook.check_unconfirmed()

    def _set_catalog(self, project_id: str, layers: Any, categories: Any) -> None:
        self._catalog[project_id] = (layers, categories)
        self._refresh_editing(project_id)
        self.changed.emit()

    def cached_attachments(
        self, project_id: str, layer_id: str, doc_id: str
    ) -> tuple[Attachment, ...] | None:
        cache = self.cache(project_id)
        info = cache.table(layer_id) if cache.exists else None
        return cache.attachments(info, doc_id) if info is not None else None

    def fetch_attachments(
        self,
        project_id: str,
        layer_id: str,
        doc_id: str,
        callback: Callable[[tuple[Attachment, ...] | None, bool], None],
    ) -> bool:
        """Ask the app for a document's attachments with current media URLs.

        `callback(attachments, removed)`; attachments is None on an error.
        Returns False, without calling back, when the app is not connected."""
        client = self.media_client(project_id)
        if client is None:
            return False
        params: dict[str, Any] = {
            "projectId": project_id,
            "layerId": layer_id,
            "ids": [doc_id],
        }

        def got(result: Any, error: Any) -> None:
            if error is not None or not isinstance(result, dict):
                callback(None, False)
                return
            for feature in result.get("features") or []:
                if isinstance(feature, dict) and feature.get("id") == doc_id:
                    callback(parse_attachments(feature.get("attachments")), False)
                    return
            callback((), doc_id in (result.get("removed") or []))

        client.request("layers.getFeatures", params, got)
        return True

    def download(self, project_id: str) -> MediaDownload | None:
        return self._downloads.get(project_id)

    def download_all_media(self, project_id: str) -> MediaDownload | None:
        """Start fetching every media file of a project; None if not connected."""
        running = self._downloads.get(project_id)
        if running is not None:
            return running
        client = self.media_client(project_id)
        layers = self.available_layers(project_id)
        if client is None or not layers:
            return None

        def request(method: str, params: Any, callback: Any) -> None:
            if client.is_ready:
                client.request(method, params, callback)
            else:
                callback(None, None)

        download = MediaDownload(
            project_id,
            [layer.layer_id for layer in layers],
            request,
            self.fetcher,
            self,
        )
        self._downloads[project_id] = download
        download.finished.connect(lambda p=project_id: self._on_download_done(p))
        download.start()
        self.downloadsChanged.emit()
        return download

    def _on_download_done(self, project_id: str) -> None:
        download = self._downloads.pop(project_id, None)
        if download is not None:
            self.last_download = (project_id, download.summary)
            download.deleteLater()
        self.downloadsChanged.emit()

    def add_layer(self, project_id: str, layer_id: str) -> QgsVectorLayer | None:
        info = self.ensure_table(project_id, layer_id)
        if info is None:
            return None
        cache = self.cache(project_id)
        layer = create_layer(cache, info)
        if not layer.isValid():
            return None
        apply_default_style(layer, info, cache.categories())
        group = project_group(self.project, project_id, self.project_name(project_id))
        self.project.addMapLayer(layer, False)
        group.addLayer(layer)
        return layer

    def add_project(self, project_id: str) -> list[QgsVectorLayer]:
        added = []
        for definition in self.available_layers(project_id):
            layer = self.add_layer(project_id, definition.layer_id)
            if layer is not None:
                added.append(layer)
        return added

    def reset_style(self, layer: QgsMapLayer) -> bool:
        ids = self._resolved_ids(layer)
        if ids is None or not isinstance(layer, QgsVectorLayer):
            return False
        cache = self.cache(ids[0])
        info = cache.table(ids[1]) if cache.exists else None
        if info is None:
            return False
        apply_default_style(layer, info, cache.categories())
        return True

    def remove_cached_data(self, project_id: str) -> str | None:
        """Returns a message for the user if the cache file could not be deleted."""
        layers = comapeo_layers(self.project, project_id)
        self.project.removeMapLayers([layer.id() for layer in layers])
        root = self.project.layerTreeRoot()
        for group in root.findGroups(True):
            if group.customProperty(GROUP_PROP_PROJECT) == project_id:
                parent = group.parent()
                if parent is not None and not group.children():
                    parent.removeChildNode(group)
        sync = self._syncs.pop(project_id, None)
        if sync is not None:
            sync.close()
            sync.deleteLater()
        self.mediaRemoving.emit(project_id)
        download = self._downloads.get(project_id)
        if download is not None:
            download.cancel()
        self.fetcher.abort_project(project_id)
        self.fetcher.clear_errors(project_id)
        self.media.remove_project(project_id)
        problem = None
        try:
            self.cache(project_id).remove()
        except PermissionError:
            # Windows refuses to delete an open file; the removed layers close it
            # only when their deferred deletion runs.
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            try:
                self.cache(project_id).remove()
            except OSError as err:
                problem = f"Could not delete the cached data: {err.strerror or err}"
        except OSError as err:
            problem = f"Could not delete the cached data: {err.strerror or err}"
        self._caches.pop(project_id, None)
        if problem is None:
            self._gone.discard(project_id)
        self.changed.emit()
        return problem

    def ensure_table(self, project_id: str, layer_id: str) -> TableInfo | None:
        """The cached table for a layer, created from the app's definition if needed.

        A stored table is used as it is, even if out of date: the project's sync
        rebuilds it without emptying the map in the meantime."""
        cache = self.cache(project_id)
        catalog = self._catalog.get(project_id)
        if catalog is not None:
            self._set_aside_if_damaged(project_id)
        current = cache.table(layer_id) if cache.exists else None
        if current is not None and cache.is_stored(current):
            return current
        if catalog is None:
            return current
        layers_result, categories_result = catalog
        raw = next(
            (
                item
                for item in (layers_result or {}).get("layers", [])
                if isinstance(item, dict) and item.get("layerId") == layer_id
            ),
            None,
        )
        parsed = parse_layers({"layers": [raw]}) if raw is not None else []
        if not parsed or not parsed[0].geometry_supported:
            return cache.table(layer_id) if cache.exists else None
        key = layer_schema_key(
            parsed[0], self._lang(), parse_categories(categories_result)
        )
        try:
            project = self._projects.get(project_id)
            if project is not None:
                cache.set_project(project, self._served.get(project_id))
            if cache.raw_categories() != categories_result:
                cache.set_categories(categories_result)
            info, created = cache.ensure_table(raw, key)
            if created:
                self._on_rebuilt(
                    project_id,
                    layer_id,
                    current.field("category_id") if current else "",
                    current.table if current else "",
                )
        except CacheError:
            return cache.table(layer_id)
        return info

    # Connection events

    def _on_client_ready(self, build_type: str, client: LocalApiClient) -> None:
        client.notification.connect(self._on_notification)
        self._list_projects(build_type, client)
        self._retry_media()

    def _list_projects(
        self, build_type: str, client: LocalApiClient, full: bool = True
    ) -> None:
        client.request(
            "projects.list",
            None,
            lambda result, error: self._on_projects(
                build_type, client, result, error, full
            ),
        )

    def _poll_projects(self) -> None:
        for build_type, client in self.connector.ready_clients().items():
            self._list_projects(build_type, client, full=False)
        # Nothing announces that a lock was released; look again.
        if self._locked_files or any(c.locked for c in self._caches.values()):
            self.changed.emit()

    def _on_projects(
        self,
        build_type: str,
        client: LocalApiClient,
        result: Any,
        error: Any,
        full: bool,
    ) -> None:
        """`full` (connect, manual Refresh) refetches everything; a poll only
        acts on projects that appeared, disappeared or were renamed."""
        if not client.is_ready:
            return
        if error is not None:
            self.list_errors[build_type] = (
                f"CoMapeo Desktop could not list its projects ({error.code})."
            )
            self.changed.emit()
            return
        changed = self.list_errors.pop(build_type, None) is not None or full
        self._listed.add(build_type)
        projects = parse_projects(result)
        before = {p for p, b in self._served.items() if b == build_type}
        for project_id in before:
            del self._served[project_id]
        for project in projects:
            project_id = project.project_id
            new = project_id not in before
            renamed = self._projects.get(project_id) != project
            changed = changed or new or renamed
            self._projects[project_id] = project
            serving = self._served.setdefault(project_id, build_type) == build_type
            if serving:
                self._origins[project_id] = build_type
            self._gone.discard(project_id)
            cache = self.cache(project_id)
            build = cache.build_type() if cache.exists else None
            # Under a lock the build is unknown, not different; writing would wait.
            moved = (
                serving and cache.exists and not cache.locked and build != build_type
            )
            # Before writing: metadata written into a damaged file would hide it.
            if full or new or renamed or moved:
                self._set_aside_if_damaged(project_id)
            if cache.exists and (full or new or renamed or moved):
                with contextlib.suppress(CacheError):
                    cache.set_project(project, build_type if serving else None)
            if full or new:
                self._load_catalog(project_id, client)
        offered = {p.project_id for p in projects}
        active = set(self.active_projects())
        for project_id in sorted(active & offered):
            if full or project_id not in before:
                self._activate(project_id, refresh=full)
        for project_id in sorted((active | before) - offered):
            if project_id in self._served or not self._origin_answered(project_id):
                continue
            changed = changed or project_id not in self._gone
            self._mark_gone(project_id)
        if changed:
            self.changed.emit()

    def _origin_answered(self, project_id: str) -> bool:
        """Whether the build that last served this project has listed its projects
        since connecting; only then can its absence from the list mean gone."""
        origin = self._origins.get(project_id)
        if origin is None:
            cache = self.cache(project_id)
            origin = cache.build_type() if cache.exists else None
        return origin is not None and origin in self._listed

    def _load_catalog(self, project_id: str, client: LocalApiClient) -> None:
        params: dict[str, Any] = {"projectId": project_id}
        lang = self._lang()
        if lang:
            params["lang"] = lang
        replies: dict[str, Any] = {}

        def got(name: str) -> Callable[[Any, Any], None]:
            def callback(result: Any, error: Any) -> None:
                if error is not None:
                    return
                replies[name] = result
                if len(replies) == 2:
                    self._set_catalog(project_id, replies["layers"], replies["cats"])

            return callback

        client.request("layers.list", params, got("layers"))
        client.request("categories.list", params, got("cats"))

    def _on_client_lost(self, build_type: str) -> None:
        self.list_errors.pop(build_type, None)
        self._listed.discard(build_type)
        for project_id, served_by in list(self._served.items()):
            if served_by != build_type:
                continue
            del self._served[project_id]
            self._catalog.pop(project_id, None)
            sync = self._syncs.get(project_id)
            if sync is not None:
                sync.detach()
            self._refresh_editing(project_id)
        self.changed.emit()

    def _on_notification(self, method: str, params: Any) -> None:
        project_id = params.get("projectId") if isinstance(params, dict) else None
        sync = self._syncs.get(project_id) if isinstance(project_id, str) else None
        if sync is not None:
            sync.handle_notification(method, params)

    # Project and layer events

    def _activate(self, project_id: str, refresh: bool = False) -> None:
        build_type = self._served.get(project_id)
        client = self.connector.client(build_type) if build_type else None
        sync = self._syncs.get(project_id)
        if sync is None:
            sync = ProjectSync(self.cache(project_id), self._lang, self)
            sync.layerDataChanged.connect(
                lambda layer_id, p=project_id: self._on_data_changed(p, layer_id)
            )
            sync.layerRebuilt.connect(
                lambda layer_id, old_field, old_table, p=project_id: self._on_rebuilt(
                    p, layer_id, old_field, old_table
                )
            )
            sync.categoriesChanged.connect(
                lambda p=project_id: self._on_categories_changed(p)
            )
            sync.schemaLoaded.connect(
                lambda layers, cats, p=project_id: self._set_catalog(p, layers, cats)
            )
            sync.statusChanged.connect(self.changed)
            sync.statusChanged.connect(lambda p=project_id: self._refresh_editing(p))
            sync.projectGone.connect(lambda p=project_id: self._on_gone(p))
            self._syncs[project_id] = sync
        if client is not None and not sync.connected:
            self._set_aside_if_damaged(project_id)
            sync.attach(client)
        elif client is not None and refresh:
            sync.refresh()
        self.changed.emit()

    def _set_aside_if_damaged(self, project_id: str) -> None:
        """With the app at hand a damaged cache is moved aside and refetched; the
        map layers are pointed at the new tables as they are created."""
        self.cache(project_id).set_aside_if_damaged()

    def _on_layers_added(self, layers: list[QgsMapLayer]) -> None:
        touched = set()
        for layer in layers:
            if not isinstance(layer, QgsVectorLayer):
                continue
            project_id = self._adopt(layer)
            if project_id is not None:
                touched.add(project_id)
                self._watch(layer, project_id)
                self.update_editing(layer, force=True)
        for project_id in touched:
            self._activate(project_id)

    def _adopt(self, layer: QgsVectorLayer) -> str | None:
        ids = self._resolved_ids(layer)
        if ids is not None:
            project_id, layer_id = ids
            cache = self.cache(project_id)
            info = cache.table(layer_id) if cache.exists else None
            if info is not None:
                point_at(layer, cache, info)
            return project_id
        source = layer.source()
        if not is_cache_source(self.cache_root, source):
            return None
        found = self._cache_for_source(source)
        if found is None:
            return None
        cache, info = found
        configure(layer, cache.project_id, info)
        layer.setName(info.definition.title)
        apply_default_style(layer, info, cache.categories())
        return cache.project_id

    def _cache_for_source(self, source: str) -> tuple[ProjectCache, TableInfo] | None:
        path = Path(source.split("|", 1)[0])
        table = table_from_source(source)
        project = self._project_from_file(path)
        if project is None or table is None:
            return None
        cache = self.cache(project.project_id)
        for info in cache.tables().values():
            if info.table == table:
                return cache, info
        return None

    def _project_from_file(self, path: Path) -> Project | None:
        """The last project read from the file while another program locks it."""
        for cache in self._caches.values():
            if cache.path == path and cache.project() is not None:
                self._locked_files.discard(path)
                return cache.project()
        try:
            project = project_in_file(path)
        except CacheLocked:
            self._locked_files.add(path)
            return self._file_projects.get(path)
        self._locked_files.discard(path)
        if project is None:
            self._file_projects.pop(path, None)
        else:
            self._file_projects[path] = project
        return project

    def _on_layers_removed(self, _layer_ids: list[str]) -> None:
        for layer_id in list(self._watched):
            if self.project.mapLayer(layer_id) is None:
                del self._watched[layer_id]
                hook = self._hooks.pop(layer_id, None)
                if hook is not None:
                    hook.detach()
                self._edit_keys.pop(layer_id, None)
        active = set(self.active_projects())
        for project_id in list(self._syncs):
            if project_id not in active:
                sync = self._syncs.pop(project_id)
                sync.close()
                sync.deleteLater()
        self.changed.emit()

    def _on_read_project(self, *_args: Any) -> None:
        self._on_layers_added(list(self.project.mapLayers().values()))
        self.changed.emit()

    def _watch(self, layer: QgsVectorLayer, project_id: str) -> None:
        if layer.id() in self._watched:
            return
        self._watched[layer.id()] = layer
        layer.selectionChanged.connect(self._on_selection)
        self._hooks[layer.id()] = EditHook(layer, self, project_id)

    def _on_selection(self, *_args: Any) -> None:
        layer = self.sender()
        if not isinstance(layer, QgsVectorLayer) or layer.id() in self._reloading:
            return
        ids = self._resolved_ids(layer)
        if ids is None:
            return
        cache = self.cache(ids[0])
        info = cache.table(ids[1]) if cache.exists else None
        if info is None:
            return
        field = info.field("doc_id")
        doc_ids = [f[field] for f in layer.selectedFeatures()]
        self.selectionChanged.emit(ids[0], ids[1], doc_ids)

    # Sync events

    def _on_data_changed(self, project_id: str, layer_id: str) -> None:
        cache = self.cache(project_id)
        info = cache.table(layer_id)
        # Tracks list categories as they come into use; points list them all already.
        kind = info.definition.geometry_type if info is not None else None
        lines = kind == GEOMETRY_LINESTRING
        for layer in self._layers_of(project_id, layer_id):
            if info is None:
                layer.reload()
            else:
                self._reload_keeping_selection(layer, info.field("doc_id"))
                refresh_not_editable(layer)
            if lines:
                append_new_categories(layer, info, cache.categories())
            layer.triggerRepaint()
        self.changed.emit()

    def _reload_keeping_selection(self, layer: QgsVectorLayer, id_field: str) -> None:
        """Reloading drops the user's selection; select the same documents again."""
        before = {f[id_field] for f in layer.selectedFeatures()}
        self._reloading.add(layer.id())
        try:
            layer.reload()
            if before:
                request = QgsFeatureRequest()
                request.setFlags(Qgis.FeatureRequestFlag.NoGeometry)
                request.setSubsetOfAttributes([id_field], layer.fields())
                layer.selectByIds(
                    [
                        f.id()
                        for f in layer.getFeatures(request)
                        if f[id_field] in before
                    ]
                )
        finally:
            self._reloading.discard(layer.id())
        after = {f[id_field] for f in layer.selectedFeatures()}
        if after != before:
            ids = self._resolved_ids(layer)
            if ids is not None:
                self.selectionChanged.emit(ids[0], ids[1], sorted(after))

    def _on_rebuilt(
        self,
        project_id: str,
        layer_id: str,
        old_category_field: str,
        old_table: str = "",
    ) -> None:
        cache = self.cache(project_id)
        info = cache.table(layer_id)
        if info is None:
            return
        replaced = {old_table: layer_id} if old_table else {}
        for layer in self._layers_of(project_id, layer_id, replaced):
            point_at(layer, cache, info)
            follow_category_field(layer, old_category_field, info)
            append_new_categories(layer, info, cache.categories())
            self.update_editing(layer, force=True)
            layer.triggerRepaint()

    def _on_categories_changed(self, project_id: str) -> None:
        cache = self.cache(project_id)
        categories = cache.categories()
        for layer in self._layers_of(project_id):
            ids = self._resolved_ids(layer)
            info = cache.table(ids[1]) if ids else None
            if info is not None:
                append_new_categories(layer, info, categories)
                self.update_editing(layer)

    def _resolved_ids(
        self, layer: QgsMapLayer, replaced: dict[str, str] | None = None
    ) -> tuple[str, str] | None:
        """`comapeo_ids`, trusting the table over a layer id a pasted style brought.
        `replaced` maps tables a rebuild just superseded to their layer."""
        ids = comapeo_ids(layer)
        if ids is None:
            return None
        project_id, layer_id = ids
        table = table_from_source(layer.source())
        owner = (replaced or {}).get(table or "")
        cache = self.cache(project_id)
        if owner is None and table and cache.exists:
            owner = cache.layer_for_table(table)
        if owner is None or owner == layer_id:
            return ids
        layer.setCustomProperty(PROP_LAYER, owner)
        return project_id, owner

    def _layers_of(
        self,
        project_id: str,
        layer_id: str | None = None,
        replaced: dict[str, str] | None = None,
    ) -> list[QgsVectorLayer]:
        found = []
        for layer in comapeo_layers(self.project, project_id):
            ids = self._resolved_ids(layer, replaced)
            if ids is not None and layer_id in (None, ids[1]):
                found.append(layer)
        return found

    def _on_gone(self, project_id: str) -> None:
        self._mark_gone(project_id)
        self.changed.emit()

    def _mark_gone(self, project_id: str) -> None:
        """No connected app offers this project; its layers show cached data."""
        self._served.pop(project_id, None)
        self._catalog.pop(project_id, None)
        if project_id in self._syncs or self.cache(project_id).exists:
            self._gone.add(project_id)
        sync = self._syncs.get(project_id)
        if sync is not None:
            sync.detach()


def extra_discovery_locations() -> list[str]:
    """Folders or files from the panel's "Other locations", one per line."""
    value = QgsSettings().value(SETTING_EXTRA_DISCOVERY, "")
    if not isinstance(value, str):
        return []
    return [line.strip() for line in value.splitlines() if line.strip()]


def set_extra_discovery_locations(locations: list[str]) -> None:
    text = "\n".join(line.strip() for line in locations if line.strip())
    QgsSettings().setValue(SETTING_EXTRA_DISCOVERY, text)


def _setting_override() -> str | None:
    value = QgsSettings().value(SETTING_DISCOVERY, "")
    return value if isinstance(value, str) and value else None
