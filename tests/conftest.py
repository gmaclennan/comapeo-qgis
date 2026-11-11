try:
    import qgis.core  # noqa: F401
except ImportError:
    # tests/qgis imports qgis at module level; outside QGIS's Python skip it whole.
    collect_ignore = ["qgis"]
