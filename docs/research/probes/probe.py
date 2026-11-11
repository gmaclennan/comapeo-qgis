import sys, base64, traceback
from qgis.core import *
from qgis.gui import *
from qgis.PyQt.QtCore import QUrl, QEventLoop, QTimer, QT_VERSION_STR
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkAccessManager, QLocalSocket

sock = sys.argv[1]
app = QgsApplication([], True)
app.initQgis()
print('QGIS', Qgis.QGIS_VERSION, 'Qt', QT_VERSION_STR)
print('providers:', sorted(QgsProviderRegistry.instance().providerList()))


def section(name, fn):
    print('\n==', name)
    try:
        fn()
    except Exception:
        traceback.print_exc()


def t_memory_signals():
    vl = QgsVectorLayer('Point?crs=epsg:4326&field=id:string&field=tags:map', 'm', 'memory')
    print('valid', vl.isValid(), 'fields', [(f.name(), f.typeName()) for f in vl.fields()])
    log = []
    vl.beforeCommitChanges.connect(lambda *a: log.append(('beforeCommitChanges', a)))
    vl.afterCommitChanges.connect(lambda *a: log.append(('afterCommitChanges',)))
    vl.committedFeaturesAdded.connect(lambda lid, feats: log.append(('committedFeaturesAdded', [f.id() for f in feats])))
    vl.committedFeaturesRemoved.connect(lambda lid, fids: log.append(('committedFeaturesRemoved', list(fids))))
    vl.committedAttributeValuesChanges.connect(lambda lid, m: log.append(('committedAttributeValuesChanges', dict(m))))
    vl.committedGeometriesChanges.connect(lambda lid, m: log.append(('committedGeometriesChanges', list(m.keys()))))
    vl.featureAdded.connect(lambda fid: log.append(('featureAdded', fid)))
    vl.startEditing()
    f = QgsFeature(vl.fields())
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(1, 2)))
    f.setAttributes(['a', {'k': 'v', 'n': 1}])
    vl.addFeature(f)
    print('commit', vl.commitChanges(False))
    fid = next(vl.getFeatures()).id()
    vl.changeAttributeValue(fid, 0, 'b')
    vl.changeGeometry(fid, QgsGeometry.fromPointXY(QgsPointXY(3, 4)))
    print('commit', vl.commitChanges(False))
    vl.deleteFeature(fid)
    print('commit', vl.commitChanges())
    for l in log:
        print('  ', l)
    # provider-direct write + dataChanged
    n = []
    vl.dataChanged.connect(lambda: n.append(1))
    f2 = QgsFeature(vl.fields()); f2.setAttributes(['x', {'a': 'b'}])
    vl.dataProvider().addFeatures([f2])
    print('provider-direct add -> layer dataChanged emitted:', len(n), 'count', vl.featureCount())
    print('map attr roundtrip', next(vl.getFeatures())['tags'])


def t_svg_base64():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><circle cx="5" cy="5" r="4" fill="red"/></svg>'
    p = 'base64:' + base64.b64encode(svg).decode()
    c = QgsApplication.svgCache().svgContent(p, 10, QColor_('red'), QColor_('black'), 1, 1)
    print('base64 svg content ok:', bytes(c)[:20])
    sl = QgsSvgMarkerSymbolLayer(p)
    print('svg layer path startswith base64:', sl.path()[:12])
    rl = QgsRasterMarkerSymbolLayer('http://127.0.0.1:47911/x.png')
    print('raster marker path:', rl.path())
    # remote http svg
    got = []
    QgsApplication.svgCache().remoteSvgFetched.connect(lambda u: got.append(u))
    c = QgsApplication.svgCache().svgContent('http://127.0.0.1:47911/icon.svg', 10, QColor_('red'), QColor_('black'), 1, 1)
    spin(1500)
    c2 = QgsApplication.svgCache().svgContent('http://127.0.0.1:47911/icon.svg', 10, QColor_('red'), QColor_('black'), 1, 1)
    print('remote svg fetched signal:', got, 'content after fetch has circle:', b'circle' in bytes(c2))


def QColor_(n):
    from qgis.PyQt.QtGui import QColor
    return QColor(n)


def spin(ms):
    loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()


def t_network():
    r = QgsBlockingNetworkRequest()
    err = r.get(QNetworkRequest(QUrl('http://127.0.0.1:47911/tcp')))
    print('blocking tcp:', err, bytes(r.reply().content()))
    s = QLocalSocket()
    s.connectToServer(sock)
    print('QLocalSocket connected:', s.waitForConnected(2000))
    s.write(b'GET /raw HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n'); s.waitForBytesWritten(1000); s.waitForReadyRead(2000)
    print('QLocalSocket raw response head:', bytes(s.readAll())[:40])


def t_gui_classes():
    import qgis.gui as g, qgis.core as c
    for n in ['QgsMediaWidget', 'QgsExternalResourceWidget', 'QgsWebView', 'QgsExternalStorageFileWidget', 'QgsDataItemGuiProvider', 'QgsSourceSelectProvider', 'QgsLayerTreeViewIndicator', 'QgsDockWidget', 'QgsHtmlWidgetWrapper']:
        print(n, hasattr(g, n))
    print('DocumentViewerContent:', [x.name for x in QgsExternalResourceWidget.DocumentViewerContent])
    print('external storages:', [s.type() for s in QgsApplication.externalStorageRegistry().externalStorages()])
    for mod in ['QtMultimedia', 'QtWebSockets', 'QtWebEngineWidgets', 'QtWebKitWidgets', 'QtSvg', 'QtTest']:
        try:
            __import__('qgis.PyQt.' + mod); print('qgis.PyQt.' + mod, 'OK')
        except Exception as e:
            print('qgis.PyQt.' + mod, 'NO', e)


def t_py_provider():
    class P(QgsVectorDataProvider):
        @classmethod
        def providerKey(cls): return 'probepy'
        @classmethod
        def description(cls): return 'probe'
        @classmethod
        def createProvider(cls, uri, opts, flags=Qgis.DataProviderReadFlags()): return P(uri, opts, flags)
        def __init__(self, uri='', opts=QgsDataProvider.ProviderOptions(), flags=Qgis.DataProviderReadFlags()):
            super().__init__(uri)
            self._f = QgsFields(); self._f.append(QgsField('id', __import__('qgis.PyQt.QtCore', fromlist=['QMetaType']).QMetaType.Type.QString))
        def name(self): return 'probepy'
        def isValid(self): return True
        def wkbType(self): return Qgis.WkbType.Point
        def fields(self): return self._f
        def crs(self): return QgsCoordinateReferenceSystem('EPSG:4326')
        def extent(self): return QgsRectangle(0, 0, 1, 1)
        def featureCount(self): return 0
        def capabilities(self): return Qgis.VectorProviderCapability.AddFeatures | Qgis.VectorProviderCapability.DeleteFeatures | Qgis.VectorProviderCapability.ChangeAttributeValues | Qgis.VectorProviderCapability.ChangeGeometries
        def featureSource(self): return None
    md = QgsProviderMetadata(P.providerKey(), P.description(), P.createProvider)
    ok = QgsProviderRegistry.instance().registerProvider(md)
    print('registerProvider ->', ok, 'in list:', 'probepy' in QgsProviderRegistry.instance().providerList())
    vl = QgsVectorLayer('whatever', 'p', 'probepy')
    print('python provider layer valid:', vl.isValid(), 'provider type:', vl.providerType(), 'caps:', vl.dataProvider().capabilitiesString() if vl.dataProvider() else None)
    # layer with unknown provider (simulates plugin not loaded)
    bad = QgsVectorLayer('whatever', 'p', 'notregistered')
    print('unregistered provider layer valid:', bad.isValid())


def t_geojson_and_fields():
    # json field in gpkg
    import tempfile, os
    d = tempfile.mkdtemp()
    path = os.path.join(d, 'c.gpkg')
    src = QgsVectorLayer('Point?crs=epsg:4326&field=id:string&field=tags:map', 'm', 'memory')
    f = QgsFeature(src.fields()); f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(1, 2))); f.setAttributes(['a', {'k': 'v'}])
    src.dataProvider().addFeatures([f])
    o = QgsVectorFileWriter.SaveVectorOptions(); o.driverName = 'GPKG'; o.layerName = 'obs'
    res = QgsVectorFileWriter.writeAsVectorFormatV3(src, path, QgsCoordinateTransformContext(), o)
    print('write gpkg', res[0], res[1])
    vl = QgsVectorLayer(path + '|layername=obs', 'obs', 'ogr')
    print('gpkg fields', [(x.name(), x.typeName(), x.subType()) for x in vl.fields()], 'val', repr(next(vl.getFeatures())['tags']))
    print('expr map_get:', QgsExpression("map_get(\"tags\", 'k')").evaluate(_ctx(vl)))
    # external edit detection: write via second handle then reload
    vl2 = QgsVectorLayer(path + '|layername=obs', 'obs2', 'ogr')
    f2 = QgsFeature(vl2.fields()); f2.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(5, 6))); f2.setAttribute('id', 'b')
    print('add via 2nd handle', vl2.dataProvider().addFeatures([f2]))
    print('count before reload', vl.featureCount()); vl.dataProvider().reloadData(); print('after reloadData', vl.featureCount())


def _ctx(vl):
    c = QgsExpressionContext(); c.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(vl)); c.setFeature(next(vl.getFeatures())); return c


section('memory signals', t_memory_signals)
section('network', t_network)
section('svg', t_svg_base64)
section('gui classes', t_gui_classes)
section('python provider', t_py_provider)

def t_project():
    import tempfile, os
    d = tempfile.mkdtemp(); pp = os.path.join(d, 'p.qgz')
    prj = QgsProject.instance()
    m = QgsVectorLayer('Point?crs=epsg:4326&field=id:string', 'mem', 'memory')
    f = QgsFeature(m.fields()); f.setAttributes(['a']); m.dataProvider().addFeatures([f])
    m.setCustomProperty('myplugin/source', 'project-123')
    prj.addMapLayer(m)
    pyl = QgsVectorLayer('whatever', 'py', 'probepy'); prj.addMapLayer(pyl)
    print('write', prj.write(pp)); prj.clear()
    print('read', prj.read(pp))
    for l in prj.mapLayers().values():
        print('  reopened', l.name(), l.providerType(), 'valid', l.isValid(), 'count', l.featureCount() if l.isValid() else None, 'customprop', l.customProperty('myplugin/source'))
section('project', t_project)

sys.stdout.flush()
import os
os._exit(0)
