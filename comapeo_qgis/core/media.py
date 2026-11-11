"""On-disk cache of attachment files, keyed by attachment key, never by URL."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

from .model import MEDIA_VARIANTS, Attachment

# Names come from any member's device and the OS opens files by extension.
_EXTENSIONS = {
    "photo": (".jpg", (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif")),
    "audio": (".m4a", (".m4a", ".mp3", ".aac", ".wav", ".ogg", ".opus")),
    "video": (".mp4", (".mp4", ".mov", ".m4v", ".webm")),
}
# Real attachment names carry no extension, so the server's content type comes first.
_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/x-wav": ".wav",
    "audio/vnd.wave": ".wav",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-m4v": ".m4v",
    "video/webm": ".webm",
}
UNKNOWN_TYPE_EXTENSION = ".bin"
_PARTIAL_PREFIX = ".part-"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class MediaState(Enum):
    CACHED = "cached"
    NOT_FETCHED = "not_fetched"
    NOT_SYNCED = "not_synced"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class MediaEntry:
    state: MediaState
    path: Path
    url: str | None


class FetchOutcome(Enum):
    OK = "ok"
    NOT_SYNCED = "not_synced"
    ERROR = "error"


def classify_status(status: int) -> FetchOutcome:
    """A 404 from the media server means the file has not synced here yet.

    Only 200 is a complete file; a 206 partial body must not be cached as one.
    """
    if status == 200:
        return FetchOutcome.OK
    if status == 404:
        return FetchOutcome.NOT_SYNCED
    return FetchOutcome.ERROR


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def file_extension(attachment: Attachment, content_type: str | None = None) -> str:
    """From the content type, else the name, else the type's default; never
    an extension outside the attachment type's allow-list."""
    known = _EXTENSIONS.get(attachment.type)
    if known is None:
        return UNKNOWN_TYPE_EXTENSION
    default, allowed = known
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    from_type = _CONTENT_TYPES.get(media_type)
    if from_type in allowed:
        return from_type
    suffix = Path(attachment.name or "").suffix.lower()
    return suffix if suffix in allowed else default


def _allowed_extensions(attachment: Attachment) -> tuple[str, ...]:
    known = _EXTENSIONS.get(attachment.type)
    if known is None:
        return (UNKNOWN_TYPE_EXTENSION,)
    return known[1]


def is_media_url(url: str | None) -> bool:
    """Only the app's own media server on this computer is fetched."""
    if not url:
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    return parts.scheme == "http" and host in _LOOPBACK_HOSTS


class MediaCache:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self._not_synced: set[tuple[str, str, str]] = set()

    def project_dir(self, project_id: str) -> Path:
        return self.root / _digest(project_id)

    def path_for(
        self,
        project_id: str,
        attachment: Attachment,
        variant: str,
        content_type: str | None = None,
    ) -> Path:
        if variant not in MEDIA_VARIANTS:
            raise ValueError(f"unknown media variant {variant!r}")
        name = variant + file_extension(attachment, content_type)
        return self.attachment_dir(project_id, attachment) / name

    def attachment_dir(self, project_id: str, attachment: Attachment) -> Path:
        return self.project_dir(project_id) / _digest(attachment.key)

    def cached_file(
        self, project_id: str, attachment: Attachment, variant: str
    ) -> Path | None:
        """The stored file for this variant, whatever extension it was saved with."""
        preferred = self.path_for(project_id, attachment, variant)
        if preferred.is_file():
            return preferred
        for extension in _allowed_extensions(attachment):
            path = preferred.with_name(variant + extension)
            if path.is_file():
                return path
        return None

    def lookup(
        self, project_id: str, attachment: Attachment, variant: str
    ) -> MediaEntry:
        url = attachment.urls.get(variant)
        cached = self.cached_file(project_id, attachment, variant)
        if cached is not None:
            return MediaEntry(MediaState.CACHED, cached, url)
        path = self.path_for(project_id, attachment, variant)
        if (project_id, attachment.key, variant) in self._not_synced:
            return MediaEntry(MediaState.NOT_SYNCED, path, url)
        if url is None:
            return MediaEntry(MediaState.UNAVAILABLE, path, None)
        return MediaEntry(MediaState.NOT_FETCHED, path, url)

    def record_fetch(
        self,
        project_id: str,
        attachment: Attachment,
        variant: str,
        status: int,
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> MediaEntry:
        """Record an HTTP result; stores `data` on success.

        An empty 200 body is treated as a failed fetch, not cached.
        """
        outcome = classify_status(status)
        if outcome is FetchOutcome.OK and data:
            self.store(project_id, attachment, variant, data, content_type)
        elif outcome is FetchOutcome.NOT_SYNCED:
            self._not_synced.add((project_id, attachment.key, variant))
        return self.lookup(project_id, attachment, variant)

    def store(
        self,
        project_id: str,
        attachment: Attachment,
        variant: str,
        data: bytes,
        content_type: str | None = None,
    ) -> Path:
        partial = self.partial_file(project_id, attachment, variant)
        try:
            partial.write_bytes(data)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        return self.commit(partial, project_id, attachment, variant, content_type)

    def partial_file(
        self, project_id: str, attachment: Attachment, variant: str
    ) -> Path:
        """A new private file to stream a download into; pass it to `commit`."""
        folder = self.attachment_dir(project_id, attachment)
        # mkdir(parents=True) ignores `mode` for the parents it creates.
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for item in (folder.parent, folder):
            item.mkdir(mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=_PARTIAL_PREFIX)
        os.close(fd)
        return Path(tmp)

    def commit(
        self,
        partial: Path,
        project_id: str,
        attachment: Attachment,
        variant: str,
        content_type: str | None = None,
    ) -> Path:
        """Move a complete partial file into place as this variant."""
        path = self.path_for(project_id, attachment, variant, content_type)
        try:
            os.replace(partial, path)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        for extension in _allowed_extensions(attachment):
            other = path.with_name(variant + extension)
            if other != path:
                other.unlink(missing_ok=True)
        self._not_synced.discard((project_id, attachment.key, variant))
        return path

    def retry_not_synced(self) -> None:
        """Forget 404s so the next lookup fetches again (call on refresh)."""
        self._not_synced.clear()

    def remove_partial_files(self, older_than: float = 3600.0) -> int:
        """Delete files left by writes that never finished; call on startup.

        Recent ones are kept because another QGIS may be writing them now.
        """
        cutoff = time.time() - older_than
        removed = 0
        for path in self.root.glob(f"*/*/{_PARTIAL_PREFIX}*"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def remove_project(self, project_id: str) -> None:
        shutil.rmtree(self.project_dir(project_id), ignore_errors=True)
        self._not_synced = {k for k in self._not_synced if k[0] != project_id}
