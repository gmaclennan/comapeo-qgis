"""Names that differ between Qt5 (QGIS 3.40/3.44) and Qt6 (QGIS 4)."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from qgis.PyQt.QtCore import QDateTime, QMetaType


def meta_type(name: str) -> Any:
    scoped = getattr(QMetaType, "Type", None)
    if scoped is not None and hasattr(scoped, name):
        return getattr(scoped, name)
    return getattr(QMetaType, name)


def to_qdatetime(value: datetime) -> QDateTime:
    millis = math.floor(value.astimezone(timezone.utc).timestamp() * 1000)
    return QDateTime.fromMSecsSinceEpoch(millis).toUTC()
