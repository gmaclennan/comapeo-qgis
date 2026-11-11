"""Projects joined or left in the app while QGIS stays connected."""

from __future__ import annotations

import pytest

from comapeo_qgis.plugin import CoMapeoPlugin
from comapeo_qgis.qgis_tier import controller as controller_module
from tests.fake_server import FakeProject, FakeServer, observation
from tests.qgis.conftest import pump, use_discovery, wait_until
from tests.qgis.helpers import rows


@pytest.fixture
def fake():
    server = FakeServer().start()
    project = server.add_project(FakeProject("p1", "Fake project"))
    project.features["observations"] = [observation("a")]
    yield server
    server.stop()


@pytest.fixture
def plugin(iface, fake, monkeypatch):
    monkeypatch.setattr(controller_module, "PROJECT_POLL_MS", 200)
    use_discovery(fake.write_discovery())
    instance = CoMapeoPlugin(iface)
    instance.initGui()
    yield instance
    instance.unload()


def panel_rows(plugin) -> dict[str, str]:
    projects = plugin.dock.projects
    return {
        projects.topLevelItem(i).text(0): projects.topLevelItem(i).text(1)
        for i in range(projects.topLevelItemCount())
    }


def on_map(plugin):
    controller = plugin.controller
    wait_until(lambda: controller.available_layers("p1"))
    layers = controller.add_project("p1")
    [observations] = [
        layer
        for layer in layers
        if layer.customProperty("comapeo/layerId") == "observations"
    ]
    wait_until(lambda: set(rows(observations)) == {"a"})
    wait_until(lambda: not controller.project_status("p1").busy)
    return layers, observations


def leave(fake: FakeServer, project_id: str) -> FakeProject:
    """As PROTOCOL.md says: the subscription ends without a notification."""
    project = fake.projects.pop(project_id)
    for conn in fake.connections:
        conn.subscriptions.discard(project_id)
    return project


def test_project_left_while_connected_is_marked_and_comes_back_live(plugin, fake):
    layers, observations = on_map(plugin)
    assert plugin.indicators.stale_layers() == []
    project = leave(fake, "p1")
    plugin.dock.refresh_button.click()

    controller = plugin.controller
    wait_until(lambda: controller.project_status("p1").gone)
    status = controller.project_status("p1")
    assert status.stale and not status.connected
    wait_until(
        lambda: (
            sorted(plugin.indicators.stale_layers()) == sorted(x.id() for x in layers)
        )
    )
    assert "No longer available" in panel_rows(plugin)["Fake project"]
    assert set(rows(observations)) == {"a"}

    fake.add_project(project)
    project.features["observations"].append(observation("b"))
    plugin.dock.refresh_button.click()
    wait_until(lambda: plugin.indicators.stale_layers() == [])
    wait_until(lambda: set(rows(observations)) == {"a", "b"})

    # Live updates again, so the plugin subscribed afresh.
    wait_until(lambda: any("p1" in c.subscriptions for c in fake.connections))
    project.features["observations"].append(observation("c"))
    fake.notify(
        "layer.changed", {"projectId": "p1", "layerId": "observations", "ids": ["c"]}
    )
    wait_until(lambda: set(rows(observations)) == {"a", "b", "c"})


def test_project_left_is_noticed_without_refresh(plugin, fake):
    layers, _ = on_map(plugin)
    leave(fake, "p1")
    wait_until(lambda: plugin.controller.project_status("p1").gone)
    wait_until(
        lambda: (
            sorted(plugin.indicators.stale_layers()) == sorted(x.id() for x in layers)
        )
    )


def test_project_joined_in_the_app_appears_without_refresh(plugin, fake):
    wait_until(lambda: "Fake project" in panel_rows(plugin))
    fake.add_project(FakeProject("p2", "Joined later"))
    wait_until(lambda: "Joined later" in panel_rows(plugin))
    wait_until(lambda: plugin.controller.available_layers("p2"))


def test_polling_does_not_refetch_projects_on_the_map(plugin, fake):
    on_map(plugin)
    seen = len(fake.requests)
    pump(1000)
    methods = {r.get("method") for r in fake.requests[seen:]}
    assert methods == {"projects.list"}


def test_failed_project_list_is_shown_in_the_panel(iface, fake):
    fake.fail_next("projects.list", "INTERNAL_ERROR")
    use_discovery(fake.write_discovery())
    plugin = CoMapeoPlugin(iface)
    try:
        plugin.initGui()
        wait_until(lambda: "could not list" in plugin.dock.status_label.text())
        assert "INTERNAL_ERROR" in plugin.dock.status_label.text()
        plugin.dock.refresh_button.click()
        wait_until(lambda: "Fake project" in panel_rows(plugin))
        assert "could not list" not in plugin.dock.status_label.text()
    finally:
        plugin.unload()


def test_project_left_while_qgis_was_closed_is_reported_gone(iface, fake):
    use_discovery(fake.write_discovery())
    first = CoMapeoPlugin(iface)
    try:
        first.initGui()
        on_map(first)
    finally:
        first.unload()
    leave(fake, "p1")

    plugin = CoMapeoPlugin(iface)
    try:
        plugin.initGui()
        wait_until(lambda: plugin.controller.project_status("p1").gone)
        assert "No longer available" in panel_rows(plugin)["Fake project"]
    finally:
        plugin.unload()


def test_project_offered_but_not_on_the_map_does_not_read_as_disconnected(plugin, fake):
    fake.add_project(FakeProject("p2", "Second project"))
    on_map(plugin)
    plugin.dock.refresh_button.click()
    wait_until(lambda: "Second project" in panel_rows(plugin))
    assert panel_rows(plugin)["Second project"] == "Not on the map."
    assert panel_rows(plugin)["Fake project"].startswith("Up to date")

    fake.stop()
    wait_until(lambda: not plugin.controller.connected)
    wait_until(lambda: panel_rows(plugin).get("Fake project", "").startswith("Not"))
    assert "Second project" not in panel_rows(plugin)
