"""Server faults and odd layer ids, served by tests/fake_server.py."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest
from qgis.core import QgsBrowserModel, QgsProject, QgsProviderRegistry, QgsVectorLayer
from qgis.PyQt.QtCore import QModelIndex

from comapeo_qgis.plugin import CoMapeoPlugin
from comapeo_qgis.qgis_tier.connection import ServerState
from tests.fake_server import (
    FakeProject,
    FakeServer,
    _Failure,
    default_layers,
    observation,
)
from tests.qgis.conftest import process_events, pump, use_discovery, wait_until
from tests.qgis.helpers import rows
from tests.qgis.test_browser_model import child_index

INCOMPLETE = (
    "Not connected. The first download did not finish, so this data is incomplete."
)


class DiesMidFetchServer(FakeServer):
    """Goes away, connection and listener, when asked for a second page."""

    def _get_features(self, params):
        if params.get("cursor") is not None:
            listener, self._listener = self._listener, None
            if listener is not None:
                listener.close()
            raise ConnectionError("killed during the first download")
        return super()._get_features(params)


class MissingLayerServer(FakeServer):
    """Lists `tracks` but answers LAYER_NOT_FOUND for its features."""

    def _get_features(self, params):
        if params.get("layerId") == "tracks":
            raise _Failure("LAYER_NOT_FOUND", "no such layer")
        return super()._get_features(params)


@pytest.fixture
def fake():
    server = FakeServer().start()
    yield server
    server.stop()


@pytest.fixture
def plugin(iface):
    instance = CoMapeoPlugin(iface)
    yield instance
    instance.unload()


def start(plugin, fake, project: FakeProject):
    fake.add_project(project)
    use_discovery(fake.write_discovery())
    plugin.initGui()
    wait_until(lambda: plugin.controller.available_layers(project.project_id))
    return plugin.controller


def requests(fake: FakeServer, method: str) -> int:
    return sum(1 for r in fake.requests if r.get("method") == method)


def layer_with_id(layer_id: str, title: str) -> dict:
    layer = copy.deepcopy(default_layers()[0])
    layer["layerId"] = layer_id
    layer["title"] = title
    return layer


def meta_keys(path) -> list[str]:
    meta = QgsVectorLayer(f"{path}|layername=comapeo_meta", "meta", "ogr")
    assert meta.isValid()
    return [f["key"] for f in meta.getFeatures()]


def file_tables(path) -> set[str]:
    metadata = QgsProviderRegistry.instance().providerMetadata("ogr")
    connection = metadata.createConnection(str(path), {})
    return {t.tableName() for t in connection.tables()}


def test_failed_subscribe_is_retried_by_refresh(plugin, fake):
    project = FakeProject("p1", "Fake project")
    project.features["observations"] = [observation("a")]
    fake.fail_next("subscribe", "INTERNAL_ERROR")
    controller = start(plugin, fake, project)
    layer = controller.add_layer("p1", "observations")
    wait_until(
        lambda: "INTERNAL_ERROR" in (controller.project_status("p1").error or "")
    )

    plugin.dock.refresh_button.click()
    wait_until(lambda: set(rows(layer)) == {"a"})
    wait_until(lambda: any("p1" in c.subscriptions for c in fake.connections))
    wait_until(lambda: not controller.project_status("p1").busy)
    assert controller.project_status("p1").describe().startswith("Up to date")

    project.features["observations"].append(observation("b"))
    fake.notify(
        "layer.changed", {"projectId": "p1", "layerId": "observations", "ids": ["b"]}
    )
    wait_until(lambda: set(rows(layer)) == {"a", "b"})


def test_layer_listed_but_never_found_stops_with_an_error(plugin):
    server = MissingLayerServer().start()
    try:
        project = FakeProject("p1", "Fake project")
        project.features["observations"] = [observation("a")]
        controller = start(plugin, server, project)
        layer = controller.add_layer("p1", "observations")
        wait_until(lambda: set(rows(layer)) == {"a"})
        wait_until(
            lambda: "LAYER_NOT_FOUND" in (controller.project_status("p1").error or "")
        )
        wait_until(lambda: not controller.project_status("p1").busy)
        seen = len(server.requests)
        pump(700)
        assert len(server.requests) == seen
        assert requests(server, "layers.list") <= 3
    finally:
        server.stop()


def test_layer_not_found_once_is_recovered_by_a_refresh(plugin, fake):
    project = FakeProject("p1", "Fake project")
    project.features["observations"] = [observation("a")]
    fake.fail_next("layers.getFeatures", "LAYER_NOT_FOUND", layerId="observations")
    controller = start(plugin, fake, project)
    layer = controller.add_layer("p1", "observations")
    wait_until(lambda: set(rows(layer)) == {"a"})
    wait_until(lambda: not controller.project_status("p1").busy)
    assert controller.project_status("p1").error is None


def test_cursor_refused_mid_fetch_starts_the_layer_again(plugin, fake):
    project = FakeProject("p1", "Fake project")
    ids = {f"o{i:04d}" for i in range(2500)}
    project.features["observations"] = [observation(i) for i in sorted(ids)]
    fake.fail_next("layers.getFeatures", "INVALID_CURSOR", cursor="0:1000")
    controller = start(plugin, fake, project)
    layer = controller.add_layer("p1", "observations")
    wait_until(lambda: set(rows(layer)) == ids)
    wait_until(lambda: not controller.project_status("p1").busy)
    assert not fake.failures
    assert controller.project_status("p1").error is None
    starts = [
        r
        for r in fake.requests
        if r.get("method") == "layers.getFeatures"
        and r["params"]["layerId"] == "observations"
        and "cursor" not in r["params"]
    ]
    assert len(starts) == 2


def test_layer_ids_differing_only_in_case_or_punctuation_keep_their_own_rows(
    plugin, fake
):
    project = FakeProject(
        "p1",
        "Fake project",
        layers=[
            layer_with_id("a-b", "Dash"),
            layer_with_id("a_b", "Underscore"),
            layer_with_id("Obs", "Upper"),
            layer_with_id("obs", "Lower"),
        ],
    )
    expected = {
        "a-b": {"x1", "x2"},
        "a_b": {"y1"},
        "Obs": {"u1"},
        "obs": {"l1", "l2"},
    }
    for layer_id, ids in expected.items():
        project.features[layer_id] = [observation(i) for i in sorted(ids)]
    controller = start(plugin, fake, project)
    layers = controller.add_project("p1")
    assert len(layers) == 4
    for layer in layers:
        want = expected[layer.customProperty("comapeo/layerId")]
        wait_until(lambda layer=layer, want=want: set(rows(layer)) == want)
    assert len({layer.source() for layer in layers}) == 4


def test_cache_keeps_one_metadata_row_per_key(plugin, fake):
    odd = "it's\\"
    project = FakeProject("p1", "Fake project", layers=[layer_with_id(odd, "Odd")])
    project.features[odd] = [observation("q1")]
    controller = start(plugin, fake, project)
    [layer] = controller.add_project("p1")
    wait_until(lambda: set(rows(layer)) == {"q1"})
    for _ in range(2):
        wait_until(lambda: not controller.project_status("p1").busy)
        fetched = requests(fake, "layers.getFeatures")
        plugin.dock.refresh_button.click()
        wait_until(lambda n=fetched: requests(fake, "layers.getFeatures") > n)
        wait_until(lambda: not controller.project_status("p1").busy)
    keys = meta_keys(controller.cache("p1").path)
    assert len(keys) == len(set(keys)), keys
    assert f"layer:{odd}" in keys


def test_tables_staged_for_a_failed_rebuild_go_when_the_schema_reverts(plugin, fake):
    project = FakeProject("p1", "Fake project")
    project.features["observations"] = [observation(i) for i in "abc"]
    controller = start(plugin, fake, project)
    layer = controller.add_layer("p1", "observations")
    wait_until(lambda: set(rows(layer)) == set("abc"))
    wait_until(lambda: not controller.project_status("p1").busy)
    path = controller.cache("p1").path
    before = file_tables(path)
    assert len(before) == 3

    wait_until(lambda: any(c.subscriptions for c in fake.connections))
    river = {"categoryId": "cat-river", "name": "River", "color": "#0000ff"}
    project.categories.append(river)
    for layer_id in ("observations", "tracks"):
        fake.fail_next("layers.getFeatures", "INTERNAL_ERROR", layerId=layer_id)
    fake.notify("layer.schemaChanged", {"projectId": "p1", "layerId": "observations"})
    wait_until(lambda: controller.project_status("p1").error)
    wait_until(lambda: not fake.failures)
    assert len(file_tables(path)) == 5

    project.categories.remove(river)
    listed = requests(fake, "layers.list")
    fake.notify("layer.schemaChanged", {"projectId": "p1", "layerId": "observations"})
    wait_until(lambda: requests(fake, "layers.list") > listed)
    wait_until(lambda: not controller.project_status("p1").busy)
    wait_until(lambda: controller.project_status("p1").error is None)
    assert file_tables(path) == before
    assert not [k for k in meta_keys(path) if k.startswith("staged:")]
    assert set(rows(layer)) == set("abc")


def test_damaged_cached_definition_is_left_out_offline(iface, fake):
    project = FakeProject("p1", "Fake project")
    project.features["observations"] = [observation("a")]
    first = CoMapeoPlugin(iface)
    try:
        controller = start(first, fake, project)
        controller.add_project("p1")
        wait_until(lambda: controller.project_status("p1").last_refresh is not None)
        wait_until(lambda: not controller.project_status("p1").busy)
        path = controller.cache("p1").path
    finally:
        first.unload()
    fake.stop()
    QgsProject.instance().clear()

    meta = QgsVectorLayer(f"{path}|layername=comapeo_meta", "meta", "ogr")
    index = meta.fields().indexOf("value")
    for feature in meta.getFeatures():
        if feature["key"] == "layer:observations":
            value = json.loads(feature["value"])
            value["definition"] = {"title": "no layer id"}
            assert meta.dataProvider().changeAttributeValues(
                {feature.id(): {index: json.dumps(value)}}
            )
    del meta

    plugin = CoMapeoPlugin(iface)
    model = QgsBrowserModel()
    model.initialize()
    try:
        plugin.initGui()
        titles = [d.title for d in plugin.controller.available_layers("p1")]
        assert titles == ["Tracks"]
        cached = child_index(
            model,
            child_index(model, QModelIndex(), "CoMapeo"),
            "Cached projects (offline)",
        )
        project_index = child_index(model, cached, "Fake project")
        child_index(model, project_index, "Tracks")
        assert model.rowCount(project_index) == 1
        plugin.dock.projects.topLevelItem(0).setSelected(True)
        assert plugin.dock.add_button.isEnabled()
    finally:
        plugin.unload()
        model.deleteLater()
        process_events()


def test_last_updated_is_unknown_until_every_layer_was_fetched(plugin, fake):
    project = FakeProject("p1", "Fake project")
    project.features["observations"] = [observation(f"o{i:04d}") for i in range(2500)]
    fake.fail_next("layers.getFeatures", "INTERNAL_ERROR", cursor="0:1000")
    controller = start(plugin, fake, project)
    layer = controller.add_layer("p1", "observations")
    wait_until(lambda: controller.project_status("p1").error)
    wait_until(lambda: not controller.project_status("p1").busy)
    assert 0 < len(rows(layer)) < 2500
    assert controller.project_status("p1").last_refresh is None

    fake.stop()
    wait_until(lambda: not controller.project_status("p1").connected)
    assert controller.project_status("p1").describe() == INCOMPLETE


def test_wrong_secret_reads_as_not_running_and_sends_only_hello(plugin, fake):
    fake.add_project(FakeProject("p1", "Fake project"))
    path = fake.write_discovery()
    with open(path) as f:
        data = json.load(f)
    data["secret"] = "A" * 43
    with open(path, "w") as f:
        json.dump(data, f)
    use_discovery(path)
    plugin.initGui()
    wait_until(lambda: fake.accepted >= 2, message="retried")
    pump(300)
    [status] = plugin.controller.server_statuses
    assert status.state is ServerState.NOT_RUNNING
    assert {r.get("method") for r in fake.requests} == {"hello"}
    assert fake.accepted <= 4


def test_last_updated_is_the_oldest_layer_refresh(plugin, fake):
    project = FakeProject("p1", "Fake project")
    project.features["observations"] = [observation("a")]
    controller = start(plugin, fake, project)
    controller.add_project("p1")
    wait_until(lambda: controller.project_status("p1").last_refresh is not None)
    wait_until(lambda: not controller.project_status("p1").busy)

    started = datetime.now(timezone.utc)
    fetched = requests(fake, "layers.getFeatures")
    fake.fail_next("layers.getFeatures", "INTERNAL_ERROR", layerId="observations")
    plugin.dock.refresh_button.click()
    wait_until(lambda: requests(fake, "layers.getFeatures") >= fetched + 2)
    wait_until(lambda: controller.project_status("p1").error)
    wait_until(lambda: not controller.project_status("p1").busy)
    assert controller.project_status("p1").last_refresh < started


def badge_tooltips(plugin, layer) -> list[str]:
    view = plugin.iface.layerTreeView()
    node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
    return [indicator.toolTip() for indicator in view.indicators(node)]


def test_interrupted_first_download_reads_as_incomplete(iface):
    server = DiesMidFetchServer().start()
    plugin = CoMapeoPlugin(iface)
    try:
        project = FakeProject("p1", "Fake project")
        project.features["observations"] = [
            observation(f"o{i:04d}") for i in range(2500)
        ]
        controller = start(plugin, server, project)
        layer = controller.add_layer("p1", "observations")
        wait_until(lambda: not controller.project_status("p1").connected)
        wait_until(lambda: len(rows(layer)) == 1000, message="first page kept")

        status = controller.project_status("p1")
        assert status.incomplete
        assert status.describe() == INCOMPLETE
        wait_until(lambda: plugin.indicators.stale_layers() == [layer.id()])
        assert badge_tooltips(plugin, layer) == [INCOMPLETE]
        wait_until(lambda: plugin.dock.projects.topLevelItem(0) is not None)
        assert plugin.dock.projects.topLevelItem(0).text(1) == INCOMPLETE

        plugin.unload()
        plugin = CoMapeoPlugin(iface)
        plugin.initGui()
        [reopened] = [
            map_layer
            for map_layer in QgsProject.instance().mapLayers().values()
            if map_layer.customProperty("comapeo/layerId") == "observations"
        ]
        assert plugin.controller.project_status("p1").describe() == INCOMPLETE
        wait_until(lambda: plugin.indicators.stale_layers() == [reopened.id()])
        assert badge_tooltips(plugin, reopened) == [INCOMPLETE]
    finally:
        plugin.unload()
        server.stop()
