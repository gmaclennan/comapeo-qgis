"""Fetching attachment files from the app's media server into the media cache."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qgis.PyQt.QtCore import QObject, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import (
    QNetworkAccessManager,
    QNetworkProxy,
    QNetworkReply,
    QNetworkRequest,
)

from ..core.media import (
    FetchOutcome,
    MediaCache,
    MediaState,
    classify_status,
    is_media_url,
)
from ..core.model import Attachment, parse_attachments

TRANSFER_TIMEOUT_MS = 60_000
MAX_PARALLEL = 4

Key = tuple[str, str, str]


class _Job:
    def __init__(
        self,
        reply: QNetworkReply,
        project_id: str,
        attachment: Attachment,
        variant: str,
    ) -> None:
        self.reply = reply
        self.project_id = project_id
        self.attachment = attachment
        self.variant = variant
        self.partial: Path | None = None
        self.file: Any = None
        self.aborted = False
        self.write_error: str | None = None
        self.written = 0


class MediaFetcher(QObject):
    """One download per attachment variant at a time, streamed to disk.

    `fetched(project_id, key, variant, outcome)` fires when a fetch ends, with
    "ok", "not_synced", "error" or "aborted"; the file, if any, is then in the cache.
    """

    fetched = pyqtSignal(str, str, str, str)

    def __init__(self, cache: MediaCache, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.cache = cache
        self._nam = QNetworkAccessManager(self)
        # The media server is on this computer; a QGIS proxy setting must not catch it.
        self._nam.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        self._jobs: dict[Key, _Job] = {}
        self._errors: dict[Key, str] = {}

    def is_fetching(self, project_id: str, key: str, variant: str) -> bool:
        return (project_id, key, variant) in self._jobs

    def error(self, project_id: str, key: str, variant: str) -> str | None:
        return self._errors.get((project_id, key, variant))

    def clear_errors(
        self, project_id: str | None = None, keys: Sequence[str] | None = None
    ) -> None:
        """Forget failures so the next request fetches again; all of them by default."""
        self._errors = {
            k: v
            for k, v in self._errors.items()
            if (project_id is not None and k[0] != project_id)
            or (keys is not None and k[1] not in keys)
        }

    def fetch(self, project_id: str, attachment: Attachment, variant: str) -> bool:
        """Start a download unless one is running; False if there is no URL to use."""
        key = (project_id, attachment.key, variant)
        if key in self._jobs:
            return True
        url = attachment.urls.get(variant)
        if not is_media_url(url):
            return False
        request = QNetworkRequest(QUrl(url))
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.ManualRedirectPolicy,
        )
        if hasattr(request, "setTransferTimeout"):
            request.setTransferTimeout(TRANSFER_TIMEOUT_MS)
        self._errors.pop(key, None)
        reply = self._nam.get(request)
        job = _Job(reply, project_id, attachment, variant)
        self._jobs[key] = job
        reply.readyRead.connect(lambda job=job: self._on_ready_read(job))
        reply.finished.connect(lambda job=job: self._on_finished(job))
        return True

    def abort_project(self, project_id: str) -> None:
        for key, job in list(self._jobs.items()):
            if key[0] == project_id:
                self._abort(job)

    def abort(self, project_id: str, key: str, variant: str) -> None:
        job = self._jobs.get((project_id, key, variant))
        if job is not None:
            self._abort(job)

    def abort_all(self) -> None:
        for job in list(self._jobs.values()):
            self._abort(job)

    def _abort(self, job: _Job) -> None:
        job.aborted = True
        job.reply.abort()

    def _status(self, job: _Job) -> int | None:
        value = job.reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _on_ready_read(self, job: _Job) -> None:
        if job.aborted or job.write_error is not None:
            return
        if self._status(job) != 200:
            job.reply.readAll()
            return
        data = bytes(job.reply.readAll())
        try:
            if job.file is None:
                job.partial = self.cache.partial_file(
                    job.project_id, job.attachment, job.variant
                )
                job.file = open(job.partial, "wb")  # noqa: SIM115 - closed in _close
            job.file.write(data)
            job.written += len(data)
        except OSError as err:
            job.write_error = err.strerror or str(err)
            job.reply.abort()

    def _close(self, job: _Job, keep: bool) -> Path | None:
        if job.file is not None:
            with contextlib.suppress(OSError):
                job.file.close()
            job.file = None
        if job.partial is not None and not keep:
            job.partial.unlink(missing_ok=True)
            return None
        return job.partial

    def _complete(self, job: _Job) -> bool:
        """Whether a 200 body arrived whole; a reply can fail after its headers."""
        self._on_ready_read(job)
        if (
            job.write_error is not None
            or job.reply.error() != QNetworkReply.NetworkError.NoError
        ):
            return False
        length = job.reply.header(QNetworkRequest.KnownHeaders.ContentLengthHeader)
        try:
            return length is None or int(length) == job.written
        except (TypeError, ValueError):
            return True

    def _on_finished(self, job: _Job) -> None:
        key = (job.project_id, job.attachment.key, job.variant)
        if self._jobs.get(key) is job:
            del self._jobs[key]
        job.reply.deleteLater()
        status = self._status(job)
        outcome = "error"
        if job.aborted and job.write_error is None:
            self._close(job, keep=False)
            outcome = "aborted"
        elif job.write_error is not None:
            self._close(job, keep=False)
            self._errors[key] = f"Could not save the file: {job.write_error}"
        elif status is None or (
            classify_status(status) is FetchOutcome.OK and not self._complete(job)
        ):
            self._close(job, keep=False)
            if job.write_error is not None:
                self._errors[key] = f"Could not save the file: {job.write_error}"
            else:
                self._errors[key] = job.reply.errorString() or "The download stopped."
        else:
            result = classify_status(status)
            partial = self._close(job, keep=result is FetchOutcome.OK)
            if result is FetchOutcome.OK and partial is not None:
                content_type = job.reply.header(
                    QNetworkRequest.KnownHeaders.ContentTypeHeader
                )
                try:
                    self.cache.commit(
                        partial,
                        job.project_id,
                        job.attachment,
                        job.variant,
                        content_type if isinstance(content_type, str) else None,
                    )
                    outcome = "ok"
                except OSError as err:
                    self._errors[key] = f"Could not save the file: {err.strerror}"
            elif result is FetchOutcome.OK:
                self._errors[key] = "The app sent an empty file."
            elif result is FetchOutcome.NOT_SYNCED:
                self.cache.record_fetch(
                    job.project_id, job.attachment, job.variant, status or 404
                )
                outcome = "not_synced"
            else:
                self._errors[key] = f"The app answered with HTTP status {status}."
        self.fetched.emit(job.project_id, job.attachment.key, job.variant, outcome)


@dataclass
class DownloadSummary:
    downloaded: int = 0
    already: int = 0
    not_synced: int = 0
    failed: int = 0
    cancelled: bool = False
    error: str | None = None

    def describe(self) -> str:
        if self.error:
            return self.error
        parts = [
            f"{self.downloaded} downloaded",
            f"{self.already} already on this computer",
        ]
        if self.not_synced:
            parts.append(f"{self.not_synced} not yet synced")
        if self.failed:
            parts.append(f"{self.failed} could not be downloaded")
        text = "Media files: " + ", ".join(parts) + "."
        return ("Cancelled. " + text) if self.cancelled else text


class MediaDownload(QObject):
    """Every media file of a project: lists features fresh, then downloads each
    variant not yet cached, a few at a time.

    `request(method, params, callback)` sends a local API request.
    """

    progress = pyqtSignal(int, int)
    finished = pyqtSignal()

    def __init__(
        self,
        project_id: str,
        layer_ids: Sequence[str],
        request: Callable[[str, Any, Callable[[Any, Any], None]], None],
        fetcher: MediaFetcher,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.project_id = project_id
        self._layer_ids = list(layer_ids)
        self._request = request
        self._fetcher = fetcher
        self._queue: list[tuple[Attachment, str]] = []
        self._seen: set[tuple[str, str]] = set()
        self._running: set[tuple[str, str]] = set()
        self.total = 0
        self.done = 0
        self.summary = DownloadSummary()
        self.listing = True
        self.active = False

    def start(self) -> None:
        self.active = True
        self._fetcher.fetched.connect(self._on_fetched)
        self._list_next(None)

    def cancel(self) -> None:
        if not self.active:
            return
        self.summary.cancelled = True
        self._queue.clear()
        for key, variant in list(self._running):
            self._fetcher.abort(self.project_id, key, variant)
        self._finish()

    def _list_next(self, cursor: Any) -> None:
        if not self.active:
            return
        if not self._layer_ids:
            self.listing = False
            self.progress.emit(self.done, self.total)
            self._pump()
            return
        params: dict[str, Any] = {
            "projectId": self.project_id,
            "layerId": self._layer_ids[0],
        }
        if cursor is not None:
            params["cursor"] = cursor
        self._request("layers.getFeatures", params, self._on_page)

    def _on_page(self, result: Any, error: Any) -> None:
        if not self.active:
            return
        if error is not None or not isinstance(result, dict):
            code = getattr(error, "code", "no answer")
            self.summary.error = f"Could not list the project's media ({code})."
            self._finish()
            return
        for feature in result.get("features") or []:
            raw = feature.get("attachments") if isinstance(feature, dict) else None
            for attachment in parse_attachments(raw):
                for variant in attachment.urls:
                    if (attachment.key, variant) in self._seen:
                        continue
                    self._seen.add((attachment.key, variant))
                    state = self._fetcher.cache.lookup(
                        self.project_id, attachment, variant
                    ).state
                    if state is MediaState.CACHED:
                        self.summary.already += 1
                    else:
                        self._queue.append((attachment, variant))
                        self.total += 1
        cursor = result.get("nextCursor")
        if cursor is None:
            self._layer_ids.pop(0)
        self.progress.emit(self.done, self.total)
        self._list_next(cursor)

    def _pump(self) -> None:
        while self.active and self._queue and len(self._running) < MAX_PARALLEL:
            attachment, variant = self._queue.pop(0)
            if self._fetcher.fetch(self.project_id, attachment, variant):
                self._running.add((attachment.key, variant))
            else:
                self.summary.failed += 1
                self.done += 1
        if self.active and not self.listing and not self._queue and not self._running:
            self._finish()

    def _on_fetched(
        self, project_id: str, key: str, variant: str, outcome: str
    ) -> None:
        if project_id != self.project_id or (key, variant) not in self._running:
            return
        self._running.discard((key, variant))
        self.done += 1
        if outcome == "ok":
            self.summary.downloaded += 1
        elif outcome == "not_synced":
            self.summary.not_synced += 1
        elif outcome != "aborted":
            self.summary.failed += 1
        self.progress.emit(self.done, self.total)
        self._pump()

    def _finish(self) -> None:
        if not self.active:
            return
        self.active = False
        with contextlib.suppress(TypeError, RuntimeError):
            self._fetcher.fetched.disconnect(self._on_fetched)
        self.finished.emit()
