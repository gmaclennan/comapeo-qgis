# Initial research digest (10 Nov 2026)

Condensed from four research passes: comapeo-desktop `origin/main`, comapeo-core-react-native, @comapeo/core 7.7.0 + sibling repos, and QGIS plugin capabilities. "Unverified" marks claims taken from code reading or docs without a test.

## 1. CoMapeo Desktop today (origin/main: core 7.5.2, @comapeo/ipc 11.0.0, Electron 45, Node 24)

- `MapeoManager` runs in an Electron `utilityProcess` (`src/services/core.ts`), forked from `src/main/app.ts`. The renderer talks to it over Electron MessagePorts (structured clone) using `@comapeo/ipc` + `rpc-reflector` 4.4.0.
- No socket, named pipe, CLI or deep link exists for external processes. A second process cannot open the storage (hypercore oplog locks, root key, indexer state). The plugin must go through the running app.
- Core's Fastify (blobs, icons, maps) listens on `127.0.0.1`, random port per launch, no token. `@comapeo/map-server` likewise. Ports are never written to disk, so nothing is discoverable.
- Data dir is `<userData>/comapeo/`; `userData` differs per build type (CoMapeo Desktop, … RC, … Internal, … Dev). macOS `~/Library/Application Support/CoMapeo Desktop/comapeo` is long relative to the ~104-byte Unix socket path limit.
- On macOS closing the window leaves core running; on Windows/Linux closing all windows quits.
- Smallest change to expose core: in `core.ts`, a `node:net` server (Unix socket / Windows named pipe); per connection wrap the stream in a `MessagePortLike` and call `createComapeoCoreServer(manager, port)` exactly as done for MessagePorts. About 100–200 lines.
- Risks: the raw manager API gives any same-user process sync/invite/leave-project power (needs file mode 0600 / pipe ACL, a token, or a reduced facade); structured-clone types need a JSON convention.
- Existing zero-change fallback: export of observations/tracks as GeoJSON and observations+media as ZIP (`project.exportGeoJSONFile`, `exportZipFile`). Each feature carries the full raw doc in a `$comapeo` member.
- Desktop UI today: view observations and tracks; edit observation notes, field values, category; delete observation; remove attachment (by updating the doc); no real create UI.

## 2. Socket protocol in comapeo-core-react-native (backend on @comapeo/ipc 10.0.0)

- Two Unix sockets: `control.sock` (lifecycle, root-key init — not wanted on desktop) and `comapeo.sock` (RPC). No auth; mobile relies on the OS sandbox.
- Framing: 4-byte unsigned little-endian length + UTF-8 JSON (`framed-stream`). No reviver: Buffers arrive as `{"type":"Buffer","data":[…]}`, Dates as strings, `undefined` dropped.
- Envelope: `{"id": <channel>, "message": <inner>}`. Channels: `@@comapeo/manager`, `@@comapeo/project-routing`, `@@comapeo/services`, `@@comapeo/project/<publicId>`, and (v10) `@@comapeo/events`.
- rpc-reflector tuples: request `[0, msgId, [path…], args]`; response `[1, msgId, null, result]` or `[1, msgId, error]`; `[2|3, event, path]` on/off; `[4, event, path, err, args]` emit. Invalid frames are silently dropped.
- `getProject` is not a wire call: the client calls the routing channel (`assertProjectExists` in v9/10, `getProjectInstance` in v11) and treats the returned channel id as opaque.
- Events differ by ipc version (v9: rpc-reflector ON/EMIT; v10: unconditional push on the events channel; v11: same fixed pushed list as v10, checked).
- Reusable plain-Node pieces: `backend/lib/message-port.js`, `server-helper.js`, `comapeo-rpc.js`.
- No Windows handling in that repo. Node `net.listen('\\\\.\\pipe\\name')` should work (unverified).
- Python client estimate: 300–500 lines (framing, envelope, request/response, project routing, events, reconnect).

## 3. Core data model (@comapeo/core 7.7.0, @comapeo/schema 2.3.0)

- CRUD per data type: `create(value)`, `getByDocId`, `getByVersionId`, `getMany({includeDeleted, lang})`, `update(versionId | versionId[], fullValue)`, `delete(docId)`. Update is full-document, linked to the version(s) replaced; stale version probably forks (unverified); conflict winner is latest `updatedAt`; resolve by passing `[versionId, ...forks]`.
- Change notification: each DataType emits `updated-docs(docs[])` after indexing local or synced changes. Nothing consumes it today; whether it crosses `@comapeo/ipc` is unverified.
- Observation: optional `lat`/`lon` (can have no location), `tags` (free-form: bool/number/string/null or arrays), `attachments[]` (`driveDiscoveryId`, `name`, `type` photo|audio|video, `hash`), `metadata` (position, accuracy), optional `presetRef`. Points only.
- Track: `locations[]` (≥2; timestamp + coords), `observationRefs[]`, `tags`, optional `presetRef`.
- Preset (category): `name`, `geometry[]`, `tags`, `fieldRefs[]`, optional `iconRef`, optional `color`. Field: `tagKey`, `type` (text/number/selectOne/selectMultiple/date), `label`, `options`.
- Icon: variants SVG, or PNG at small/medium/large × 1–3 pixel density.
- Desktop matches category by tags first, then `presetRef`.
- URLs: `$blobs.getUrl` → `http://127.0.0.1:<port>/blobs/<projectPublicId>/<driveId>/<type>/<variant>/<name>` (photo: original/preview/thumbnail; audio/video: original only; audio typically `audio/mp4`; Range supported). `$icons.getIconUrl(iconDocId, {mimeType, size, pixelDensity})` → `…/icons/<projectPublicId>/<iconDocId>/<size>[@Nx].<png|svg>`.
- A 404 (`BLOB_NOT_AVAILABLE_ERROR`) on a known attachment usually means "not synced yet". Desktop is an archive device so eventually fetches originals.
- Roles: creator/coordinator may edit others' docs, members only their own — but core does not appear to enforce this; desktop's UI does (`canEdit`). Edits keep `createdBy`; `updatedBy` becomes the desktop device.
- Remote detection alerts are the only polygon-capable doc type.
- Other access paths: `comapeo-cloud` REST (bearer token; read-only for observations/tracks; needs a server joined to the project); `comapeo-core-cli` (a separate peer with its own identity); `comapeo-media-server` (unpublished prototype, adds SVG→PNG rendering).
- No RFC covers GIS interop or an external API.

## 4. QGIS plugin capabilities (LTR 3.44 on Qt5; latest 4.2 on Qt6)

Tested locally against the macOS builds of QGIS 4.2.3 / 4.0.2 unless marked.

- One codebase can cover 3.44 and 4.x via `qgis.PyQt`, with `qgisMinimumVersion=3.4x`, `qgisMaximumVersion=4.99`. `supportsQt6` is obsolete. Bundled Python 3.12 on macOS/Windows. Dependencies: stdlib + PyQt + qgis only; vendor pure-Python; no binaries; no pip.
- Ways to get data in:
  1. GeoPackage cache + socket sync: persistent, usable with the app closed, pattern used by Mergin Maps and QFieldSync; JSON columns surface as map fields (`map_get("tags",'k')`); plugin owns reconciliation.
  2. Memory layers + socket sync: least code; layers reopen valid but empty; custom properties survive so the plugin can refill.
  3. App serves OGC API Features (QGIS supports Part 4 editing since 3.32): works without a plugin, but needs localhost TCP HTTP, fixed schema, OPTIONS handling, polling only.
  4. Custom Python data provider: registration works, but GIL-bound rendering and little precedent. Not shortlisted.
- Commit signals (`committedFeaturesAdded`, `committedAttributeValuesChanges`, `committedGeometriesChanges`, `committedFeaturesRemoved`) fire with final ids on memory and GeoPackage layers. Provider-level writes coexist with a user's open edit session. `beforeCommitChanges` cannot veto.
- Icons: SVG/raster marker paths accept a local file, an http(s) URL, or `base64:` data (base64 is saved with the symbol, so projects stay self-contained). Categorized renderer gives a per-category legend.
- Attachments: the Attachment form widget supports image/audio/video but loads local paths, not http URLs (remote only via external-storage backends). No dependable web view on QGIS 4 (QtWebKit gone; QtWebEngine missing from the macOS builds). Options: "Open URL" layer action (cheapest); local attachment cache with file paths; custom dock widget with `QLabel` + `QgsMediaWidget`. No built-in thumbnail gallery. One-to-many via a child layer + relation.
- Networking: `QLocalSocket` (Unix socket on macOS/Linux, named pipe on Windows) connected to a Node socket server on Qt6/macOS; Qt5 and Windows from docs only. Python `AF_UNIX` is the wrong tool on Windows. Anything QGIS fetches itself (icon URLs, native providers) needs TCP HTTP. Long-lived subscriptions belong on the main thread, signal-driven.
- UX hooks: Browser panel tree (`QgsDataItemProvider`), Data Source Manager page, dock widgets, layer-tree indicators, temporal controller, `QgsAuthManager` for tokens.
- Tooling: `pyproject.toml` + ruff; `qgis-plugin-ci` for packaging/release; `pytest-qgis` 5.x; `qgis/qgis` Docker images (`ltr` = Qt5, `stable`/`latest` = Qt6) with `QT_QPA_PLATFORM=offscreen`; QGIS-free core package under plain pytest; fake socket server for integration tests.
- plugins.qgis.org: GPL-2.0-or-later compatible licence, public repo and tracker, no binaries, under 25 MB, Bandit and detect-secrets block on upload, manual approval.
- Licence: plugin should be GPL-2.0-or-later; the desktop app over a socket is a separate program.

## 5. qgis-smp-plugin (existing sibling) as a model

Reusable: release on GitHub Release with tag/metadata check and `git archive` zip. To improve on: tests run only against mocked QGIS (no QGIS in CI), no Qt6 declaration, Linux-only dev install script, flake8 + advisory pylint, missing icon file.

## Open technical questions to verify by spike

- `updated-docs` over `@comapeo/ipc` 11: checked — v11 (`src/lib/events.js`) relays a fixed list only (`local-peers`, `map-share`, `map-share-error`, `invite-received`, `invite-updated`, `own-role-change`, `sync-state`). `updated-docs` is not on it, so live refresh needs a small ipc change (or polling / refresh on `sync-state`).
- `QLocalSocket` ↔ Node named pipe on Windows; `QLocalSocket` on Qt5.
- ipc 11 routing and event wire details.
- Stale-version update behaviour (fork vs error).
- PyQt QtMultimedia availability on Windows/Linux QGIS builds.
