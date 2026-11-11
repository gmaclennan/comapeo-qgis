# Local socket spike results (11 Nov 2026)

Repo: https://github.com/gmaclennan/comapeo-qgis-spike (throwaway, public). Evidence run: https://github.com/gmaclennan/comapeo-qgis-spike/actions/runs/42928958624.

## Result

Node `net` server ↔ PyQt `QLocalSocket` passed in every environment tested, using 4-byte little-endian length + JSON frames: small echo, 1 MB round trip, server pushes after 500 ms and 3 s idle, server-side close, and prompt failure when no server exists.

| Environment | Qt | Python | Result |
|---|---|---|---|
| Windows / macOS / Linux, pip PyQt5 | 5.15 | 3.13 | pass |
| Windows / macOS / Linux, pip PyQt6 | 6.12 | 3.13 | pass |
| Docker `qgis/qgis:ltr` (QGIS 3.44.15) | 5.15.18 | 3.14 | pass |
| Docker `qgis/qgis:stable` (QGIS 4.2.3) | 6.10.2 | 3.14 | pass |
| Windows OSGeo4W `qgis-ltr` (3.44.15) | 5.15.13 | 3.12 | pass |
| Windows OSGeo4W `qgis` (4.2.3) | 6.11.1 | 3.12 | pass |

Windows: both `\\.\pipe\<name>` and the bare name connect. On Unix use full paths.

## Module availability in real QGIS

| Import | Docker ltr | Docker stable | Windows ltr | Windows 4.2 |
|---|---|---|---|---|
| `QLocalSocket` | OK | OK | OK | OK |
| `QtMultimedia.QMediaPlayer` | OK | OK | OK | OK |
| `qgis.gui.QgsMediaWidget` | OK | OK | OK | OK |
| `QtWebSockets` (direct PyQt) | no | no | yes | yes |
| `QtWebEngineWidgets` | no | no | no | yes |

Import success does not prove audio playback works.

## Access control

- Unix: socket at mode 0600 denies other users (verified). There is a short window between `listen` and `chmod`; a 0700 parent directory would close it (not tested).
- Windows: Node/libuv creates the pipe with the default security descriptor. Verified in CI: another non-admin local user could open the pipe read-only but not write to it. Whether a read-only client receives pushed data was not tested. Node offers no option to narrow pipe access, and remote (SMB) access is not rejected at pipe level per libuv source (not tested).
- Consequence for design: on Windows the pipe alone does not guarantee same-user-only reads. Options: require a client write (hello/handshake) before sending anything; include a per-user unguessable element in the pipe name; or add a token.

## Gotchas for the client

- Server close: macOS/Linux emit `errorOccurred(PeerClosedError)` then `disconnected`; Windows emits only `disconnected`.
- Use scoped enums (`QLocalSocket.LocalSocketState.ConnectedState`), `errorOccurred`, and `exec()` — these work on both Qt5 (5.15) and Qt6.
- No `flush()` needed. Frames arrive split into chunks on macOS/Linux.
- Socket path limit: 103 bytes on macOS, 107 on Linux; over-length gives `EINVAL` on listen.
- `download.osgeo.org` timed out from Windows runners; the `ftp.osuosl.org` mirror worked. Install takes 3–6.5 minutes.

## Not verified

Electron's Node as server; client inside a running QGIS GUI process; QGIS on macOS via this harness; Windows standalone MSI; idle longer than 3 s; media playback.
