"""The media part of the CoMapeo panel: the selected observation's photos and audio."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from qgis.PyQt.QtCore import QSize, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPixmap
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.media import MediaState
from ..core.model import Attachment
from .audio import PAUSED, PLAYING, AudioPlayer
from .controller import Controller

THUMBNAIL_SIZE = 96
PREVIEW_MAX = 480
ROLE_KEY = Qt.ItemDataRole.UserRole

HINT_NONE = "Select an observation on a CoMapeo layer to see its photos and audio."
HINT_MANY = "{n} observations selected. Select just one to see its photos and audio."

# What a variant looks like in the panel, beyond the cached file itself.
CACHED = "cached"
LOADING = "loading"
NOT_SYNCED = "not_synced"
OFFLINE = "offline"
FAILED = "failed"
MISSING = "missing"

CAPTIONS = {
    LOADING: "Loading…",
    NOT_SYNCED: "Not yet synced",
    OFFLINE: "Not on this computer",
    FAILED: "Could not load",
    MISSING: "No image",
}


def placeholder(text: str, size: int = THUMBNAIL_SIZE) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor("#e4e4e4"))
    painter = QPainter(pixmap)
    painter.setPen(QColor("#555555"))
    painter.drawText(
        pixmap.rect().adjusted(6, 6, -6, -6),
        int(Qt.AlignmentFlag.AlignCenter) | int(Qt.TextFlag.TextWordWrap),
        text,
    )
    painter.end()
    return pixmap


def format_ms(value: int) -> str:
    seconds = max(0, int(value)) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


class _AudioRow(QWidget):
    def __init__(self, panel: MediaPanel, attachment: Attachment, title: str) -> None:
        super().__init__(panel)
        self.setObjectName("audioRow")
        self.attachment = attachment
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel(title, self)
        self.play_button = QPushButton("Play", self)
        self.play_button.setObjectName("playButton")
        self.time_label = QLabel("", self)
        self.time_label.setObjectName("audioTime")
        self.open_button = QPushButton("Open in player", self)
        self.open_button.setObjectName("openAudioButton")
        layout.addWidget(self.title)
        layout.addWidget(self.play_button)
        layout.addWidget(self.time_label, 1)
        layout.addWidget(self.open_button)
        self.play_button.clicked.connect(lambda: panel.toggle_play(attachment))
        self.open_button.clicked.connect(
            lambda: panel.open_external(attachment, "original")
        )
        if not panel.player.available:
            self.play_button.hide()


class _VideoRow(QWidget):
    def __init__(self, panel: MediaPanel, attachment: Attachment, title: str) -> None:
        super().__init__(panel)
        self.setObjectName("videoRow")
        self.attachment = attachment
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(title, self), 1)
        self.open_button = QPushButton("Open in player", self)
        self.open_button.setObjectName("openVideoButton")
        layout.addWidget(self.open_button)
        self.open_button.clicked.connect(
            lambda: panel.open_external(attachment, "original")
        )


class MediaPanel(QWidget):
    """Thumbnails fetched on demand, a larger view on click, originals only when
    asked for. Every asynchronous result is checked against the selection it was
    started for, so a late reply never shows under a newer selection."""

    def __init__(self, controller: Controller, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("mediaArea")
        self.controller = controller
        self.media = controller.media
        self.fetcher = controller.fetcher
        self.player = AudioPlayer(self)
        self._generation = 0
        self._current: tuple[str, str, str] | None = None
        self._attachments: tuple[Attachment, ...] = ()
        self._online = False
        self._waiting_for_list = False
        self._preview_key: str | None = None
        self._preview_image: tuple[Path, QPixmap] | None = None
        self._pending_open: tuple[int, str, str] | None = None
        self._pending_play: tuple[int, str] | None = None
        self._playing_key: str | None = None
        self._audio_rows: list[_AudioRow] = []
        self._video_rows: list[_VideoRow] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.message = QLabel(HINT_NONE, self)
        self.message.setObjectName("mediaMessage")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.photos = QListWidget(self)
        self.photos.setObjectName("photoList")
        self.photos.setViewMode(QListWidget.ViewMode.IconMode)
        self.photos.setIconSize(QSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        self.photos.setGridSize(QSize(THUMBNAIL_SIZE + 12, THUMBNAIL_SIZE + 30))
        self.photos.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.photos.setMovement(QListWidget.Movement.Static)
        self.photos.setWrapping(True)
        self.photos.setWordWrap(True)
        self.photos.itemClicked.connect(self._on_photo_clicked)
        self.photos.itemActivated.connect(self._on_photo_clicked)
        layout.addWidget(self.photos)
        self.preview = QLabel(self)
        self.preview.setObjectName("photoPreview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setWordWrap(True)
        layout.addWidget(self.preview)
        self.open_original_button = QPushButton("Open original", self)
        self.open_original_button.setObjectName("openOriginalButton")
        self.open_original_button.clicked.connect(self._open_original)
        layout.addWidget(self.open_original_button)
        self.audio_box = QVBoxLayout()
        layout.addLayout(self.audio_box)
        self.action_message = QLabel(self)
        self.action_message.setObjectName("mediaActionMessage")
        self.action_message.setWordWrap(True)
        layout.addWidget(self.action_message)
        layout.addStretch(1)
        self._ticker = QTimer(self)
        self._ticker.setInterval(250)
        self._ticker.timeout.connect(self._update_audio_rows)

        controller.selectionChanged.connect(self.on_selection)
        controller.mediaRetry.connect(self.reload)
        controller.mediaRemoving.connect(self._on_media_removing)
        controller.changed.connect(self._on_controller_changed)
        self.fetcher.fetched.connect(self._on_fetched)
        self.player.stateChanged.connect(self._on_player_state)
        self.player.positionChanged.connect(lambda _p: self._update_audio_rows())
        self.player.durationChanged.connect(lambda _d: self._update_audio_rows())
        self.player.failed.connect(self._on_play_failed)
        self._clear(HINT_NONE)

    # Selection

    def on_selection(self, project_id: str, layer_id: str, doc_ids: list) -> None:
        if len(doc_ids) == 1:
            self.show_document(project_id, layer_id, str(doc_ids[0]))
        elif doc_ids:
            self._clear(HINT_MANY.format(n=len(doc_ids)))
        else:
            self._clear(HINT_NONE)

    def show_document(self, project_id: str, layer_id: str, doc_id: str) -> None:
        self._reset()
        self._current = (project_id, layer_id, doc_id)
        self._attachments = (
            self.controller.cached_attachments(project_id, layer_id, doc_id) or ()
        )
        self._request_list()
        self._render()

    def reload(self) -> None:
        if self._current is not None:
            preview = self._preview_key
            self.show_document(*self._current)
            if preview is not None and self._find(preview) is not None:
                self._select_photo(preview)

    def _request_list(self) -> None:
        if self._current is None:
            return
        project_id, layer_id, doc_id = self._current
        generation = self._generation

        def got(attachments: tuple[Attachment, ...] | None, removed: bool) -> None:
            if generation != self._generation:
                return
            self._waiting_for_list = False
            if removed:
                self._attachments = ()
                self._online = True
                self._render("This observation has been deleted in CoMapeo.")
                return
            if attachments is None:
                self._render("CoMapeo Desktop could not list this observation's media.")
                return
            self._attachments = attachments
            self._online = True
            self.fetcher.clear_errors(project_id, [a.key for a in attachments])
            self._render()
            self._resume_pending()

        self._waiting_for_list = self.controller.fetch_attachments(
            project_id, layer_id, doc_id, got
        )

    def _resume_pending(self) -> None:
        """Downloads asked for before the app had sent current media URLs."""
        if self._pending_open is not None:
            _generation, key, variant = self._pending_open
            attachment = self._find(key)
            if attachment is not None:
                self.open_external(attachment, variant)
        if self._pending_play is not None:
            attachment = self._find(self._pending_play[1])
            if attachment is not None:
                self.toggle_play(attachment)

    def _reset(self) -> None:
        self._generation += 1
        self.player.release()
        self._playing_key = None
        self._current = None
        self._attachments = ()
        self._online = False
        self._waiting_for_list = False
        self._preview_key = None
        self._preview_image = None
        self._pending_open = None
        self._pending_play = None
        self.action_message.clear()

    def _clear(self, hint: str) -> None:
        self._reset()
        self.message.setText(hint)
        self.photos.clear()
        self.photos.hide()
        self._clear_preview()
        self._rebuild_rows([], [])

    def _on_controller_changed(self) -> None:
        if self._current is None:
            return
        project_id = self._current[0]
        connected = self.controller.media_client(project_id) is not None
        if not self.controller.cache(project_id).exists:
            self._clear(HINT_NONE)
        elif (self._online or self._waiting_for_list) and not connected:
            self._online = False
            self._waiting_for_list = False
            self._render()
        elif not self._online and not self._waiting_for_list and connected:
            self.reload()

    def _on_media_removing(self, project_id: str) -> None:
        if self._current is not None and self._current[0] == project_id:
            self._clear(HINT_NONE)

    # State of one variant

    def _state(self, attachment: Attachment, variant: str) -> tuple[str, Path | None]:
        if self._current is None:
            return OFFLINE, None
        project_id = self._current[0]
        entry = self.media.lookup(project_id, attachment, variant)
        if entry.state is MediaState.CACHED:
            return CACHED, entry.path
        if self.fetcher.is_fetching(project_id, attachment.key, variant):
            return LOADING, None
        if entry.state is MediaState.NOT_SYNCED:
            return NOT_SYNCED, None
        if self.fetcher.error(project_id, attachment.key, variant):
            return FAILED, None
        if not self._online:
            return (LOADING if self._waiting_for_list else OFFLINE), None
        if variant not in attachment.urls:
            return MISSING, None
        return LOADING, None

    def _ensure(self, attachment: Attachment, variant: str) -> str:
        """The variant's state, starting a download if it can be had."""
        state, _path = self._state(attachment, variant)
        if state == LOADING and self._online and self._current is not None:
            project_id = self._current[0]
            busy = self.fetcher.is_fetching(project_id, attachment.key, variant)
            if not busy and not self.fetcher.fetch(project_id, attachment, variant):
                return FAILED
        return state

    # Rendering

    def _render(self, note: str | None = None) -> None:
        photos = [a for a in self._attachments if a.type == "photo"]
        audio = [a for a in self._attachments if a.type == "audio"]
        video = [a for a in self._attachments if a.type == "video"]
        lines = []
        if note:
            lines.append(note)
        elif not self._attachments:
            lines.append(
                "Loading…" if self._waiting_for_list else "No photos or audio."
            )
        else:
            counts = []
            if photos:
                counts.append(f"{len(photos)} photo{'s' if len(photos) != 1 else ''}")
            if audio:
                counts.append(f"{len(audio)} audio recording{'s' * (len(audio) != 1)}")
            if video:
                counts.append(f"{len(video)} video{'s' if len(video) != 1 else ''}")
            other = len(self._attachments) - len(photos) - len(audio) - len(video)
            if other:
                counts.append(f"{other} other file{'s' * (other != 1)} not shown")
            lines.append(", ".join(counts) + ".")
        if self._attachments and not self._online and not self._waiting_for_list:
            lines.append(
                "CoMapeo Desktop is not connected: showing what is saved on this "
                "computer."
            )
        self.message.setText("\n".join(lines))

        self.photos.clear()
        for attachment in photos:
            item = QListWidgetItem()
            item.setData(ROLE_KEY, attachment.key)
            self._paint_item(item, attachment)
            self.photos.addItem(item)
        self.photos.setVisible(bool(photos))
        self._fit_photo_list()
        QTimer.singleShot(0, self._fit_photo_list)
        if self._preview_key is not None and self._find(self._preview_key) is None:
            self._preview_key = None
        self._show_preview()
        self._rebuild_rows(audio, video)

    def _fit_photo_list(self) -> None:
        cell = self.photos.gridSize()
        per_row = max(1, (self.photos.viewport().width() - 4) // cell.width())
        rows = max(1, min(2, -(-self.photos.count() // per_row)))
        self.photos.setFixedHeight(
            rows * cell.height() + 2 * self.photos.frameWidth() + 4
        )

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._fit_photo_list()
        if self._preview_key is not None:
            self._show_preview()

    def _paint_item(self, item: QListWidgetItem, attachment: Attachment) -> None:
        state = self._ensure(attachment, "thumbnail")
        _state, path = self._state(attachment, "thumbnail")
        pixmap = QPixmap(str(path)) if path is not None else QPixmap()
        if state == CACHED and not pixmap.isNull():
            item.setIcon(
                QIcon(
                    pixmap.scaled(
                        THUMBNAIL_SIZE,
                        THUMBNAIL_SIZE,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            )
            item.setText("")
            item.setToolTip("Click for a larger view")
        else:
            caption = CAPTIONS.get(state, CAPTIONS[FAILED])
            item.setIcon(QIcon(placeholder(caption)))
            item.setText(caption)
            item.setToolTip(caption)

    def _rebuild_rows(self, audio: list[Attachment], video: list[Attachment]) -> None:
        for row in self._audio_rows + self._video_rows:
            self.audio_box.removeWidget(row)
            row.hide()
            row.deleteLater()
        self._audio_rows = []
        self._video_rows = []
        for n, attachment in enumerate(audio, 1):
            row = _AudioRow(self, attachment, f"Audio {n}")
            self.audio_box.addWidget(row)
            self._audio_rows.append(row)
        for n, attachment in enumerate(video, 1):
            row = _VideoRow(self, attachment, f"Video {n}")
            self.audio_box.addWidget(row)
            self._video_rows.append(row)
        self._update_audio_rows()

    def _update_audio_rows(self, *_args: Any) -> None:
        for row in self._audio_rows:
            key = row.attachment.key
            playing = key == self._playing_key and self.player.state == PLAYING
            row.play_button.setText("Pause" if playing else "Play")
            if self._current is None:
                continue
            state, _path = self._state(row.attachment, "original")
            if key == self._playing_key and self.player.state in (PLAYING, PAUSED):
                text = (
                    f"{format_ms(self.player.position())} / "
                    f"{format_ms(self.player.duration())}"
                )
            elif state == CACHED:
                text = ""
            else:
                text = CAPTIONS.get(state, "")
            row.time_label.setText(text)

    # Larger view

    def _find(self, key: str) -> Attachment | None:
        return next((a for a in self._attachments if a.key == key), None)

    def _select_photo(self, key: str) -> None:
        for row in range(self.photos.count()):
            item = self.photos.item(row)
            if item.data(ROLE_KEY) == key:
                self.photos.setCurrentItem(item)
                self._on_photo_clicked(item)
                return

    def _on_photo_clicked(self, item: QListWidgetItem) -> None:
        key = item.data(ROLE_KEY)
        if not isinstance(key, str) or self._find(key) is None:
            return
        self._preview_key = key
        self.action_message.clear()
        self._show_preview()

    def _clear_preview(self) -> None:
        self.preview.clear()
        self.preview.hide()
        self.open_original_button.hide()

    def _load_preview(self, path: Path | None) -> QPixmap:
        if path is None:
            return QPixmap()
        if self._preview_image is None or self._preview_image[0] != path:
            self._preview_image = (path, QPixmap(str(path)))
        return self._preview_image[1]

    def _show_preview(self) -> None:
        attachment = self._find(self._preview_key) if self._preview_key else None
        if attachment is None or self._current is None:
            self._clear_preview()
            return
        variant = "preview" if "preview" in attachment.urls else "thumbnail"
        state = self._ensure(attachment, variant)
        _state, path = self._state(attachment, variant)
        self.preview.show()
        self.open_original_button.show()
        pixmap = self._load_preview(path)
        if state == CACHED and not pixmap.isNull():
            width = max(120, min(PREVIEW_MAX, self.preview.width() - 8))
            height = min(PREVIEW_MAX, 360)
            self.preview.setPixmap(
                pixmap.scaled(
                    width,
                    height,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            return
        texts = {
            LOADING: "Loading the larger view…",
            NOT_SYNCED: "This photo has not synced to this computer yet. It will be "
            "fetched again when you press Refresh.",
            OFFLINE: "The larger view is not saved on this computer. Open CoMapeo "
            "Desktop to see it.",
            MISSING: "There is no larger view of this photo.",
        }
        self.preview.setText(texts.get(state, "The larger view could not be loaded."))

    # Opening and playing

    def _open_original(self) -> None:
        attachment = self._find(self._preview_key) if self._preview_key else None
        if attachment is not None:
            self.open_external(attachment, "original")

    def open_external(self, attachment: Attachment, variant: str) -> None:
        """Open a file with the system's viewer or player, downloading it first."""
        state = self._ensure(attachment, variant)
        if state == CACHED:
            _state, path = self._state(attachment, variant)
            self._open_path(path)
        elif state == LOADING:
            self._pending_open = (self._generation, attachment.key, variant)
            self.action_message.setText("Downloading the original…")
        else:
            self.action_message.setText(self._unavailable_text(state))

    def toggle_play(self, attachment: Attachment) -> None:
        if self._playing_key == attachment.key and self.player.state == PLAYING:
            self.player.pause()
            return
        state = self._ensure(attachment, "original")
        if state == CACHED:
            _state, path = self._state(attachment, "original")
            self._play(attachment.key, path)
        elif state == LOADING:
            self._pending_play = (self._generation, attachment.key)
            self.action_message.setText("Downloading the recording…")
        else:
            self.action_message.setText(self._unavailable_text(state))
        self._update_audio_rows()

    def _play(self, key: str, path: Path | None) -> None:
        if path is None:
            return
        self._playing_key = key
        self.action_message.clear()
        self.player.play(path)

    def _on_player_state(self, state: str) -> None:
        # Some backends report the position rarely, so poll it while playing.
        if state == PLAYING:
            self._ticker.start()
        else:
            self._ticker.stop()
        self._update_audio_rows()

    def _on_play_failed(self, message: str) -> None:
        self.action_message.setText(
            f"{message} Use “Open in player” to listen in another application."
        )
        self._update_audio_rows()

    def _open_path(self, path: Path | None) -> None:
        if path is None:
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.action_message.setText("No application on this computer opened it.")
        else:
            self.action_message.clear()

    def _unavailable_text(self, state: str) -> str:
        if state == NOT_SYNCED:
            return "This file has not synced to this computer yet."
        if state == OFFLINE:
            return "This file is not saved on this computer. Open CoMapeo Desktop."
        return "This file could not be downloaded."

    def _on_fetched(
        self, project_id: str, key: str, variant: str, outcome: str
    ) -> None:
        if self._current is None or project_id != self._current[0]:
            return
        attachment = self._find(key)
        if attachment is None:
            return
        if variant == "thumbnail":
            for row in range(self.photos.count()):
                item = self.photos.item(row)
                if item.data(ROLE_KEY) == key:
                    self._paint_item(item, attachment)
        if key == self._preview_key:
            self._show_preview()
        pending = (self._generation, key, variant)
        if self._pending_open == pending:
            self._pending_open = None
            if outcome == "ok":
                self.open_external(attachment, variant)
            else:
                self.action_message.setText(
                    self._unavailable_text(self._state(attachment, variant)[0])
                )
        if variant == "original" and self._pending_play == (self._generation, key):
            self._pending_play = None
            state, path = self._state(attachment, "original")
            if state == CACHED:
                self._play(key, path)
            else:
                self.action_message.setText(self._unavailable_text(state))
        self._update_audio_rows()

    def shutdown(self) -> None:
        self._reset()
        self.player.release()
