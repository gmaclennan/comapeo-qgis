"""The plugin is read-only even when the app says observations may be edited."""

from __future__ import annotations

import pytest

from comapeo_qgis.plugin import CoMapeoPlugin
from tests.qgis.conftest import pump, server_or_skip, use_discovery, wait_until
from tests.qgis.helpers import Oracle, rows

pytestmark = pytest.mark.real_server


def revisions(oracle: Oracle, layer_id: str) -> dict[str, tuple]:
    return {
        doc_id: (f["version"], tuple(f.get("forks") or ()))
        for doc_id, f in oracle.features(layer_id).items()
    }


def assert_not_editable(layer) -> None:
    assert layer.readOnly()
    assert not layer.startEditing()
    assert not layer.isEditable()
    feature = next(layer.getFeatures())
    index = layer.fields().indexOf("notes")
    assert not layer.changeAttributeValue(feature.id(), index, "changed in QGIS")
    assert not layer.deleteFeature(feature.id())
    assert not layer.commitChanges()


def test_layers_stay_read_only_and_nothing_is_written(iface):
    server = server_or_skip(observations=12, tracks=2, photos=0, allow_edits=True)
    oracle = Oracle(server.discovery_dir)
    plugin = CoMapeoPlugin(iface)
    try:
        definition = oracle.layers()["observations"]
        assert definition["editable"] is True
        assert {"properties", "delete"} <= set(definition["editOperations"])
        assert all(f["editable"] for f in oracle.features("observations").values())
        before = {layer_id: revisions(oracle, layer_id) for layer_id in oracle.layers()}

        use_discovery(server.discovery_dir)
        plugin.initGui()
        controller = plugin.controller
        wait_until(lambda: controller.available_layers(oracle.project_id))
        layers = controller.add_project(oracle.project_id)
        for layer in layers:
            expected = set(before[layer.customProperty("comapeo/layerId")])
            wait_until(lambda layer=layer, e=expected: set(rows(layer)) == e)
        wait_until(lambda: not controller.project_status(oracle.project_id).busy)
        for layer in layers:
            assert_not_editable(layer)

        observations = next(
            x for x in layers if x.customProperty("comapeo/layerId") == "observations"
        )
        doc_id = sorted(before["observations"])[0]
        changed = server.run(f"update {doc_id}")
        info = controller.cache(oracle.project_id).table("observations")
        version_field = info.field("version")
        wait_until(
            lambda: rows(observations)[doc_id][version_field] == changed["versionId"],
            message="the live update",
        )
        pump(500)
        for layer in layers:
            assert_not_editable(layer)

        after = {layer_id: revisions(oracle, layer_id) for layer_id in oracle.layers()}
        expected = {k: dict(v) for k, v in before.items()}
        expected["observations"][doc_id] = (changed["versionId"], ())
        assert after == expected
    finally:
        plugin.unload()
        oracle.close()
        server.stop()
