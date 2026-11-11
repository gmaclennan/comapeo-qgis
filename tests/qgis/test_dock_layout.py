"""The CoMapeo panel at the widths a side panel is given."""

from __future__ import annotations

from comapeo_qgis.plugin import CoMapeoPlugin
from tests.fake_server import FakeProject, FakeServer, observation, track
from tests.qgis.conftest import pump, use_discovery, wait_until


def test_a_long_status_wraps_in_a_narrow_panel_and_unwraps_when_wide(iface):
    fake = FakeServer().start()
    project = fake.add_project(FakeProject("p1", "River survey (test data)"))
    project.features["observations"] = [observation("a")]
    project.features["tracks"] = [track("t1")]
    use_discovery(fake.write_discovery())
    plugin = CoMapeoPlugin(iface)
    try:
        plugin.initGui()
        controller = plugin.controller
        wait_until(lambda: controller.available_layers("p1"))
        controller.add_project("p1")
        wait_until(lambda: controller.project_status("p1").last_refresh)
        wait_until(lambda: not controller.project_status("p1").busy)
        fake.stop()
        wait_until(lambda: not controller.project_status("p1").connected)

        dock = plugin.dock
        dock.setFloating(True)
        dock.resize(340, 420)
        dock.show()
        pump(200)
        tree = dock.projects
        item = tree.topLevelItem(0)
        line = tree.fontMetrics().height()
        status_width = tree.header().sectionSize(1)
        assert tree.fontMetrics().horizontalAdvance(item.text(1)) > status_width
        assert tree.visualItemRect(item).height() >= 2 * line

        dock.resize(1400, 420)
        pump(200)
        assert tree.visualItemRect(item).height() < 2 * line
    finally:
        plugin.unload()
        fake.stop()
