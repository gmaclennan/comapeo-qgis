"""The harness itself: an exception in a Qt slot must not pass unnoticed."""

import os

import pytest
from qgis.PyQt.QtCore import QTimer

from tests.qgis.conftest import wait_until


def test_exception_in_a_slot_is_caught_by_the_fixture(no_uncaught_exceptions):
    def boom():
        raise RuntimeError("raised in a slot")

    QTimer.singleShot(0, boom)
    wait_until(lambda: no_uncaught_exceptions)
    [(kind, error, _tb)] = no_uncaught_exceptions
    assert kind is RuntimeError and str(error) == "raised in a slot"
    no_uncaught_exceptions.clear()


@pytest.mark.skipif(
    not os.environ.get("COMAPEO_EXPECT_QGIS"), reason="COMAPEO_EXPECT_QGIS not set"
)
def test_runs_under_the_expected_qgis_and_qt():
    from qgis.core import Qgis
    from qgis.PyQt.QtCore import QT_VERSION_STR

    qgis_major, qt_major = os.environ["COMAPEO_EXPECT_QGIS"].split("/")
    assert str(Qgis.QGIS_VERSION_INT // 10000) == qgis_major, Qgis.QGIS_VERSION
    assert QT_VERSION_STR.split(".")[0] == qt_major, QT_VERSION_STR
