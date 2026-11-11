"""The tracks legend lists the categories tracks use, not every category."""

from __future__ import annotations

import pytest
from qgis.PyQt.QtGui import QColor

from comapeo_qgis.plugin import CoMapeoPlugin
from tests.fake_server import FakeProject, FakeServer, observation, track
from tests.qgis.conftest import use_discovery, wait_until
from tests.qgis.helpers import rows


def walked(doc_id: str, category_id: str) -> dict:
    feature = track(doc_id)
    feature["categoryId"] = category_id
    return feature


@pytest.fixture
def fake():
    server = FakeServer().start()
    project = server.add_project(FakeProject("p1", "Fake project"))
    project.categories += [
        {"categoryId": "cat-walk", "name": "Walk", "color": "#00aa00"},
        {"categoryId": "cat-boat", "name": "Boat trip", "color": "#0000aa"},
    ]
    project.features["observations"] = [observation("a")]
    project.features["tracks"] = [walked("t1", "cat-walk"), track("t2")]
    yield server
    server.stop()


@pytest.fixture
def plugin(iface, fake):
    use_discovery(fake.write_discovery())
    instance = CoMapeoPlugin(iface)
    instance.initGui()
    yield instance
    instance.unload()


def legend(layer) -> set:
    return {c.value() for c in layer.renderer().categories()}


def synced(plugin):
    controller = plugin.controller
    wait_until(lambda: controller.available_layers("p1"))
    observations, tracks = controller.add_project("p1")
    wait_until(lambda: set(rows(tracks)) == {"t1", "t2"})
    wait_until(lambda: not controller.project_status("p1").busy)
    return observations, tracks


def test_tracks_list_only_categories_in_use(plugin):
    observations, tracks = synced(plugin)
    assert legend(tracks) == {"cat-walk", None}
    assert legend(observations) == {"cat-school", "cat-walk", "cat-boat", None}


def test_a_category_first_used_later_joins_the_tracks_legend(plugin, fake):
    _observations, tracks = synced(plugin)
    renderer = tracks.renderer()
    [walk] = [c for c in renderer.categories() if c.value() == "cat-walk"]
    symbol = walk.symbol().clone()
    symbol.setColor(QColor("#123456"))
    renderer.updateCategorySymbol(renderer.categoryIndexForValue("cat-walk"), symbol)

    fake.projects["p1"].features["tracks"].append(walked("t3", "cat-boat"))
    changed = {"projectId": "p1", "layerId": "tracks", "ids": ["t3"]}
    fake.notify("layer.changed", changed)
    wait_until(lambda: "cat-boat" in legend(tracks), message="boat trip listed")

    assert legend(tracks) == {"cat-walk", "cat-boat", None}
    [walk] = [c for c in tracks.renderer().categories() if c.value() == "cat-walk"]
    assert walk.symbol().color().name() == "#123456"


def test_a_categories_change_does_not_add_unused_ones_to_tracks(plugin, fake):
    _observations, tracks = synced(plugin)
    source = tracks.source()
    fake.projects["p1"].categories.append(
        {"categoryId": "cat-river", "name": "River", "color": "#0000ff"}
    )
    fake.notify("layer.schemaChanged", {"projectId": "p1", "layerId": "tracks"})
    wait_until(lambda: tracks.source() != source, message="tracks table rebuilt")
    wait_until(lambda: not plugin.controller.project_status("p1").busy)
    assert legend(tracks) == {"cat-walk", None}
