"""Runs only under QGIS's Python: scripts/qgis-python.sh -m pytest tests."""

import pytest

pytest.importorskip("qgis.core")

import qgis.utils
from qgis.testing import start_app
from qgis.testing.mocked import get_iface


@pytest.fixture(scope="module")
def iface():
    start_app()
    qgis.utils.iface = get_iface()
    return qgis.utils.iface


def test_qgis_loads_starts_and_unloads_the_plugin(iface):
    assert qgis.utils.loadPlugin("comapeo_qgis")
    assert qgis.utils.startPlugin("comapeo_qgis")
    assert "comapeo_qgis" in qgis.utils.active_plugins
    assert qgis.utils.unloadPlugin("comapeo_qgis")
    assert "comapeo_qgis" not in qgis.utils.active_plugins
