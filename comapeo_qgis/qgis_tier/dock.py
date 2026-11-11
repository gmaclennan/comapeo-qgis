"""The CoMapeo side panel: connection status, projects, last updated, Refresh."""

from __future__ import annotations

from qgis.gui import QgsDockWidget
from qgis.PyQt.QtCore import QEvent, QObject, QSize, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .connection import ServerState
from .controller import (
    Controller,
    extra_discovery_locations,
    set_extra_discovery_locations,
)

_ROLE_PROJECT = Qt.ItemDataRole.UserRole


class CoMapeoDock(QgsDockWidget):
    """`media_area` is the seam for the photo and audio viewer (milestone 4):
    a container below the project list, empty and hidden in this version."""

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
        self.locations_button = QPushButton("Other app locations…", body)
        self.locations_button.setObjectName("locationsButton")
        self.locations_button.setToolTip(
            "Folders or discovery files of CoMapeo Desktop builds that are not "
            "installed in the usual place, such as a development build"
        )
        self.locations_button.clicked.connect(self._edit_locations)
        locations_row = QHBoxLayout()
        locations_row.addStretch(1)
        locations_row.addWidget(self.locations_button)
        layout.addLayout(locations_row)
        self.projects = QTreeWidget(body)
        self.projects.setObjectName("projectList")
        self.projects.setHeaderLabels(["Project", "Status"])
        self.projects.setRootIsDecorated(False)
        self.projects.setWordWrap(True)
        self.projects.setUniformRowHeights(False)
        header = self.projects.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_rows)
        header.sectionResized.connect(lambda *_: self._fit_timer.start(0))
        self.projects.viewport().installEventFilter(self)
        self.projects.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.projects, 1)
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
        self.media_area = QWidget(body)
        self.media_area.setObjectName("mediaArea")
        self.media_area.hide()
        layout.addWidget(self.media_area)
        self.setWidget(body)
        controller.changed.connect(self.update_view)
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
        self._fit_rows()
        self._update_buttons()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # The stretched status column changes width without sectionResized.
        if event.type() == QEvent.Type.Resize and watched is self.projects.viewport():
            self._fit_timer.start(0)
        return super().eventFilter(watched, event)

    def _fit_rows(self) -> None:
        # QTreeView sizes rows as if unwrapped, so give each status its wrapped height.
        width = self.projects.header().sectionSize(1) - 8
        if width <= 0:
            return
        metrics = self.projects.fontMetrics()
        flags = int(Qt.TextFlag.TextWordWrap)
        for i in range(self.projects.topLevelItemCount()):
            item = self.projects.topLevelItem(i)
            text = metrics.boundingRect(0, 0, width, 100_000, flags, item.text(1))
            item.setSizeHint(1, QSize(width, text.height() + 4))
        self.projects.doItemsLayout()

    def status_text(self) -> str:
        statuses = self.controller.server_statuses
        if not statuses:
            from .connection import NOT_RUNNING_MESSAGE

            return NOT_RUNNING_MESSAGE
        lines = []
        for status in statuses:
            if status.state in (ServerState.CONNECTED, ServerState.NOT_RUNNING):
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

    def _edit_locations(self) -> None:
        text, ok = QInputDialog.getMultiLineText(
            self,
            "Other app locations",
            "Look for CoMapeo Desktop in these folders or discovery files too, one "
            "per line. For a development build, give its data folder.",
            "\n".join(extra_discovery_locations()),
        )
        if ok:
            set_extra_discovery_locations(text.splitlines())
            self.controller.refresh()

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
