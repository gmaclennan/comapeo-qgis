"""The CoMapeo side panel: connection status, projects, last updated, Refresh,
and the selected observation's photos and audio."""

from __future__ import annotations

import contextlib

from qgis.gui import QgsDockWidget
from qgis.PyQt.QtCore import QRect, QSize, Qt
from qgis.PyQt.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .connection import ServerState
from .controller import Controller
from .media_panel import MediaPanel

_ROLE_PROJECT = Qt.ItemDataRole.UserRole


class _WrappingDelegate(QStyledItemDelegate):
    """QTreeView sizes rows without the column width, so wrapped text is cut off."""

    def __init__(self, view: QTreeWidget) -> None:
        super().__init__(view)
        self.view = view

    def sizeHint(self, option: QStyleOptionViewItem, index: object) -> QSize:
        base = super().sizeHint(option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        width = max(40, self.view.header().sectionSize(index.column()) - 12)
        flags = int(Qt.TextFlag.TextWordWrap)
        text = opt.fontMetrics.boundingRect(QRect(0, 0, width, 10000), flags, opt.text)
        return QSize(base.width(), max(base.height(), text.height() + 8))


class CoMapeoDock(QgsDockWidget):
    def __init__(self, controller: Controller, parent: QWidget | None = None) -> None:
        super().__init__("CoMapeo", parent)
        self.setObjectName("CoMapeoDock")
        self.controller = controller
        body = QWidget(self)
        layout = QVBoxLayout(body)
        self.status_label = QLabel(body)
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.status_label)
        self.projects = QTreeWidget(body)
        self.projects.setObjectName("projectList")
        self.projects.setHeaderLabels(["Project", "Status"])
        self.projects.setRootIsDecorated(False)
        self.projects.setWordWrap(True)
        self.projects.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.projects.header().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.projects.setItemDelegate(_WrappingDelegate(self.projects))
        self.projects.header().sectionResized.connect(
            lambda *_a: self.projects.scheduleDelayedItemsLayout()
        )
        self.projects.setMaximumHeight(160)
        self.projects.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.projects)
        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh", body)
        self.refresh_button.setObjectName("refreshButton")
        self.refresh_button.clicked.connect(controller.refresh)
        self.add_button = QPushButton("Add to map", body)
        self.add_button.setObjectName("addButton")
        self.add_button.clicked.connect(self._add_selected)
        self.remove_button = QPushButton("Remove cached data", body)
        self.remove_button.setObjectName("removeButton")
        self.remove_button.clicked.connect(self._remove_selected)
        for button in (self.refresh_button, self.add_button, self.remove_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        downloads = QHBoxLayout()
        self.download_button = QPushButton("Download all media", body)
        self.download_button.setObjectName("downloadButton")
        self.download_button.setToolTip(
            "Save every photo, audio recording and video of the selected project "
            "on this computer, so they can be seen while CoMapeo Desktop is closed."
        )
        self.download_button.clicked.connect(self._download_selected)
        self.download_progress = QProgressBar(body)
        self.download_progress.setObjectName("downloadProgress")
        self.cancel_download_button = QPushButton("Cancel", body)
        self.cancel_download_button.setObjectName("cancelDownloadButton")
        self.cancel_download_button.clicked.connect(self._cancel_download)
        downloads.addWidget(self.download_button)
        downloads.addWidget(self.download_progress, 1)
        downloads.addWidget(self.cancel_download_button)
        layout.addLayout(downloads)
        self.download_message = QLabel(body)
        self.download_message.setObjectName("downloadMessage")
        self.download_message.setWordWrap(True)
        layout.addWidget(self.download_message)
        line = QFrame(body)
        line.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(line)
        self.media_area = MediaPanel(controller, body)
        layout.addWidget(self.media_area, 1)
        self.setWidget(body)
        controller.changed.connect(self.update_view)
        controller.downloadsChanged.connect(self._update_buttons)
        self._watched_download = None
        self.update_view()

    def update_view(self) -> None:
        self.status_label.setText(self.status_text())
        selected = self.selected_project()
        self.projects.clear()
        for project_id, name in self._project_rows():
            status = self.controller.project_status(project_id)
            item = QTreeWidgetItem([name, status.describe()])
            item.setData(0, _ROLE_PROJECT, project_id)
            item.setToolTip(1, status.describe())
            if status.warning:
                item.setToolTip(1, f"{status.describe()}\n{status.warning}")
            self.projects.addTopLevelItem(item)
            if project_id == selected:
                item.setSelected(True)
        self._update_buttons()

    def status_text(self) -> str:
        statuses = self.controller.server_statuses
        if not statuses:
            from .connection import NOT_RUNNING_MESSAGE

            return NOT_RUNNING_MESSAGE
        lines = []
        for status in statuses:
            if status.state is ServerState.CONNECTED:
                lines.append(status.message)
            else:
                lines.append(f"{status.app_name}: {status.message}")
            problem = self.controller.list_errors.get(status.build_type)
            if problem:
                lines.append(problem)
        return "\n".join(lines)

    def selected_project(self) -> str | None:
        items = self.projects.selectedItems()
        return items[0].data(0, _ROLE_PROJECT) if items else None

    def _project_rows(self) -> list[tuple[str, str]]:
        rows: dict[str, str] = {}
        for status in self.controller.server_statuses:
            for project in self.controller.served_projects(status.build_type):
                rows[project.project_id] = project.name or project.project_id[:8]
        for project in self.controller.cached_projects():
            rows.setdefault(project.project_id, project.name or project.project_id[:8])
        return sorted(rows.items(), key=lambda row: row[1].lower())

    def _update_buttons(self) -> None:
        project_id = self.selected_project()
        self.add_button.setEnabled(
            project_id is not None
            and bool(self.controller.available_layers(project_id))
        )
        self.remove_button.setEnabled(
            project_id is not None and self.controller.cache(project_id).exists
        )
        download = self.controller.download(project_id) if project_id else None
        if download is not self._watched_download:
            if self._watched_download is not None:
                with contextlib.suppress(TypeError, RuntimeError):
                    self._watched_download.progress.disconnect(self._on_progress)
            if download is not None:
                download.progress.connect(self._on_progress)
            self._watched_download = download
        running = download is not None
        self.download_button.setVisible(not running)
        self.download_button.setEnabled(
            project_id is not None
            and self.controller.media_client(project_id) is not None
        )
        self.download_progress.setVisible(running)
        self.cancel_download_button.setVisible(running)
        last = self.controller.last_download
        if running:
            self._on_progress(download.done, download.total)
        elif last is not None and last[0] == project_id:
            self.download_message.setText(last[1].describe())
        else:
            self.download_message.clear()
        self.download_message.setVisible(bool(self.download_message.text()))

    def _on_progress(self, done: int, total: int) -> None:
        download = self._watched_download
        if download is None:
            return
        if download.listing:
            self.download_progress.setRange(0, 0)
            self.download_message.setText("Listing the project's media…")
        else:
            self.download_progress.setRange(0, max(total, 1))
            self.download_progress.setValue(done)
            self.download_message.setText(f"Downloading {done} of {total} files…")
        self.download_message.setVisible(True)

    def _download_selected(self) -> None:
        project_id = self.selected_project()
        if project_id is not None:
            self.controller.download_all_media(project_id)
            self._update_buttons()

    def _cancel_download(self) -> None:
        download = self._watched_download
        if download is not None:
            download.cancel()

    def _add_selected(self) -> None:
        project_id = self.selected_project()
        if project_id is not None:
            self.controller.add_project(project_id)

    def _remove_selected(self) -> None:
        project_id = self.selected_project()
        if project_id is None:
            return
        name = self.controller.project_name(project_id)
        answer = QMessageBox.question(
            self,
            "Remove cached data",
            f"Remove the CoMapeo data for “{name}” cached on this computer, and its "
            "layers from this QGIS project? CoMapeo Desktop keeps its own copy.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            problem = self.controller.remove_cached_data(project_id)
            if problem:
                QMessageBox.warning(self, "Remove cached data", problem)
