"""The harness itself: an exception in a Qt slot must not pass unnoticed."""

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
