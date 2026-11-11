import sys, os, tempfile
from qgis.core import *
app = QgsApplication([], False); app.initQgis()
d = tempfile.mkdtemp(); path = os.path.join(d, 'c.gpkg')
src = QgsVectorLayer('Point?crs=epsg:4326&field=id:string&field=tags:map', 'm', 'memory')
o = QgsVectorFileWriter.SaveVectorOptions(); o.driverName = 'GPKG'; o.layerName = 'obs'
print('create', QgsVectorFileWriter.writeAsVectorFormatV3(src, path, QgsCoordinateTransformContext(), o)[0])
vl = QgsVectorLayer(path + '|layername=obs', 'obs', 'ogr')
log = []
vl.beforeCommitChanges.connect(lambda s: log.append(('beforeCommitChanges', s)))
vl.afterCommitChanges.connect(lambda: log.append(('afterCommitChanges',)))
vl.committedFeaturesAdded.connect(lambda lid, feats: log.append(('committedFeaturesAdded', [(f.id(), f['id']) for f in feats])))
vl.committedFeaturesRemoved.connect(lambda lid, fids: log.append(('committedFeaturesRemoved', list(fids))))
vl.committedAttributeValuesChanges.connect(lambda lid, m: log.append(('committedAttributeValuesChanges', dict(m))))
vl.committedGeometriesChanges.connect(lambda lid, m: log.append(('committedGeometriesChanges', list(m.keys()))))
vl.featureAdded.connect(lambda fid: log.append(('featureAdded', fid)))
def mk(i, x):
    f = QgsFeature(vl.fields()); f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, 2))); f.setAttribute('id', i); f.setAttribute('tags', {'k': i}); return f
vl.startEditing(); vl.addFeature(mk('user-a', 1))
# live update arrives while the user has an uncommitted edit: write straight to provider
ok, added = vl.dataProvider().addFeatures([mk('remote-b', 5)])
print('provider add during edit session:', ok, 'fid', added[0].id(), '| layer sees', sorted(f['id'] for f in vl.getFeatures()))
print('commit', vl.commitChanges(False), vl.commitErrors())
fid = [f.id() for f in vl.getFeatures() if f['id'] == 'user-a'][0]
vl.changeAttributeValue(fid, vl.fields().indexOf('tags'), {'k': 'changed'}); vl.changeGeometry(fid, QgsGeometry.fromPointXY(QgsPointXY(9, 9)))
# remote change to another feature while edits pending
rb = [f.id() for f in vl.getFeatures() if f['id'] == 'remote-b'][0]
print('provider change during edit:', vl.dataProvider().changeAttributeValues({rb: {vl.fields().indexOf('tags'): {'k': 'remote2'}}}))
print('commit', vl.commitChanges(False), vl.commitErrors())
vl.deleteFeature(fid); print('commit', vl.commitChanges(), vl.commitErrors())
for l in log: print('  ', l)
print('final', [(f.id(), f['id'], f['tags']) for f in vl.getFeatures()])
os._exit(0)
