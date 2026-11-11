
import sys
from qgis.core import *
from qgis.PyQt.QtCore import QUrl, QEventLoop, QTimer
from qgis.PyQt.QtNetwork import QNetworkRequest
app = QgsApplication([], False); app.initQgis()
sock, mode = sys.argv[1], sys.argv[2]
req = QNetworkRequest(QUrl('unix+http://localsock/hello'))
req.setAttribute(QNetworkRequest.Attribute.FullLocalServerNameAttribute, sock)
if mode == 'nam':
    rep = QgsNetworkAccessManager.instance().get(req)
    loop = QEventLoop(); rep.finished.connect(loop.quit); QTimer.singleShot(3000, loop.quit); loop.exec()
    print('QgsNAM unix+http:', rep.error(), bytes(rep.readAll()), flush=True)
else:
    r = QgsBlockingNetworkRequest(); err = r.get(req)
    print('Blocking unix+http:', err, r.errorMessage(), bytes(r.reply().content()), flush=True)
import os; os._exit(0)
