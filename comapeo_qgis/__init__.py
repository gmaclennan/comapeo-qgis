"""CoMapeo QGIS plugin."""


def classFactory(iface):
    from .plugin import CoMapeoPlugin

    return CoMapeoPlugin(iface)
