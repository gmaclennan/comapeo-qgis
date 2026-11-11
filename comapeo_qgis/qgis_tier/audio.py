"""Audio playback in the panel over QtMultimedia, which differs between Qt5 and Qt6."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from qgis.PyQt.QtCore import QObject, QTimer, QUrl, pyqtSignal

STALL_MS = 6000

PLAYING = "playing"
PAUSED = "paused"
STOPPED = "stopped"


def _multimedia() -> Any:
    try:
        from qgis.PyQt import QtMultimedia
    except ImportError:
        return None
    return QtMultimedia


def playback_available() -> bool:
    return _multimedia() is not None


class AudioPlayer(QObject):
    """`stateChanged(state)` with PLAYING, PAUSED or STOPPED; `failed(message)`
    when the file cannot be played here, including when playback never starts."""

    stateChanged = pyqtSignal(str)
    positionChanged = pyqtSignal(int)
    durationChanged = pyqtSignal(int)
    failed = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.path: Path | None = None
        self.state = STOPPED
        self._module = _multimedia()
        self._player: Any = None
        self._output: Any = None
        self._states: Any = None
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._on_stall)

    @property
    def available(self) -> bool:
        return self._module is not None

    def _ensure(self) -> Any:
        if self._player is not None or self._module is None:
            return self._player
        m = self._module
        player = m.QMediaPlayer(self)
        # Qt5 also has a QAudioOutput, a raw PCM sink unrelated to QMediaPlayer.
        if hasattr(player, "setAudioOutput"):
            self._states = m.QMediaPlayer.PlaybackState
            self._output = m.QAudioOutput(self)
            player.setAudioOutput(self._output)
            player.playbackStateChanged.connect(self._on_state)
            player.errorOccurred.connect(lambda *_a: self._on_error())
        else:
            self._states = m.QMediaPlayer.State
            player.stateChanged.connect(self._on_state)
            # PyQt5 exposes the Qt5 error signal under the method's name.
            player.error.connect(lambda *_a: self._on_error())
        player.positionChanged.connect(self._on_position)
        player.durationChanged.connect(lambda d: self.durationChanged.emit(int(d)))
        self._player = player
        return player

    def play(self, path: Path) -> bool:
        player = self._ensure()
        if player is None:
            self.failed.emit("Audio playback is not available in this QGIS.")
            return False
        if self.path != path:
            self.stop()
            self.path = path
            url = QUrl.fromLocalFile(str(path))
            if hasattr(player, "setSource"):
                player.setSource(url)
            else:
                player.setMedia(self._module.QMediaContent(url))
        player.play()
        if player.position() == 0:
            self._watchdog.start(STALL_MS)
        return True

    def pause(self) -> None:
        if self._player is not None:
            self._player.pause()

    def stop(self) -> None:
        self._watchdog.stop()
        if self._player is not None:
            self._player.stop()

    def release(self) -> None:
        self.stop()
        if self._player is not None:
            with contextlib.suppress(AttributeError, TypeError):
                if hasattr(self._player, "setSource"):
                    self._player.setSource(QUrl())
                else:
                    self._player.setMedia(self._module.QMediaContent())
        self.path = None

    def position(self) -> int:
        return int(self._player.position()) if self._player is not None else 0

    def duration(self) -> int:
        return int(self._player.duration()) if self._player is not None else 0

    def media_status(self) -> str:
        if self._player is None:
            return "none"
        status = self._player.mediaStatus()
        return getattr(status, "name", str(status))

    def _on_state(self, state: Any) -> None:
        if state == self._states.PlayingState:
            value = PLAYING
        elif state == self._states.PausedState:
            value = PAUSED
        else:
            value = STOPPED
            self._watchdog.stop()
        if value != self.state:
            self.state = value
            self.stateChanged.emit(value)

    def _on_position(self, position: int) -> None:
        if position > 0:
            self._watchdog.stop()
        self.positionChanged.emit(int(position))

    def _on_error(self) -> None:
        self._watchdog.stop()
        message = ""
        if self._player is not None:
            with contextlib.suppress(AttributeError, TypeError):
                message = self._player.errorString()
        self.stop()
        self.failed.emit(message or "This audio file could not be played here.")

    def _on_stall(self) -> None:
        if self._player is not None and self._player.position() == 0:
            self.stop()
            self.failed.emit("Playback did not start.")
