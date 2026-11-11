"""The CoMapeo side panel: connection status, projects, last updated, Refresh."""

from __future__ import annotations

from qgis.gui import QgsDockWidget
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .connection import ServerState
from .controller import Controller

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
        self.projects = QTreeWidget(body)
        self.projects.setObjectName("projectList")
        self.projects.setHeaderLabels(["Project", "Status"])
        self.projects.setRootIsDecorated(False)
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
