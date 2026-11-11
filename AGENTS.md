# AGENTS.md: comapeo-qgis

QGIS plugin ("CoMapeo") that shows CoMapeo project data as QGIS layers by talking to CoMapeo Desktop over the local API. Design: `docs/PLAN.md`. Wire contract: `../comapeo-local-api/docs/PROTOCOL.md`, which is the source of truth; never code against behaviour it does not state.

## Commands

| Task | Command |
|---|---|
| Set up | `uv sync` (creates `.venv`) |
| Tests | `.venv/bin/pytest` (add `-m "not real_server"` to skip the dev-server tests) |
| Tests against the real server only | `.venv/bin/pytest tests/test_real_server.py` |
| Lint and format | `.venv/bin/ruff check . && .venv/bin/ruff format --check .` (fix: `ruff check --fix . && ruff format .`) |
| Tests under Python 3.9 | `uv run --isolated --no-project --python 3.9 --with 'pytest>=8' python -m pytest` |
| Tests under QGIS's Python (macOS) | `scripts/qgis-python.sh --install-test-deps` once, then `scripts/qgis-python.sh -m pytest`; `QGIS_VERSION=4.0.2` switches app. This is the only run that includes `tests/qgis/` |
| Any script under QGIS's Python | `scripts/qgis-python.sh path/to/script.py` |
| Smoke check in a real QGIS window (macOS) | `scripts/smoke-e2e.sh [--qgis-version 4.0.2]`: dev server, throwaway profile, Browser double-clicks, live update, then the saved project offline; screenshots and reports go to `docs/evidence/` |
| Media smoke check in a real QGIS window (macOS) | `scripts/smoke-e2e.sh --media --photos 20 --observations 120`: thumbnails, larger view, Open original, not-synced placeholder, audio playback samples, Download all media; files prefixed `media-` |

Everything must pass in `.venv` and under `scripts/qgis-python.sh` before you finish.

CI (`.github/workflows/test.yml`) adds QGIS 3.44 and 4.x in the `qgis/qgis` Docker images and in OSGeo4W on Windows, against a pinned `comapeo-local-api` commit (`LOCAL_API_SHA`). There `COMAPEO_REQUIRE_SERVER=1` turns a skipped `real_server` test into a failure and `COMAPEO_REQUIRE_QGIS=1` stops `tests/qgis/` being left out; `COMAPEO_EXPECT_QGIS=3/5` checks the QGIS and Qt majors. To reproduce the 3.44 job on a Mac, run the `qgis/qgis:ltr` image (amd64) with Node 24 and an `npm ci`'d copy of the server mounted in. OSGeo4W's `python-qgis*.bat` resets `PATH` and `PYTHONPATH`, so `COMAPEO_NODE` names Node there.

## Layout

```
comapeo_qgis/          the plugin package (what gets zipped and installed)
  __init__.py          classFactory only; must not import qgis at module level
  plugin.py            plugin entry point
  metadata.txt         plugin metadata
  core/                no qgis or PyQt imports; sans-I/O where possible
    framing.py         length-prefixed JSON frames
    protocol.py        client state machine (bytes in, bytes and events out)
    discovery.py       finding discovery files per build type and OS, plus extra locations
    model.py           API results to neutral records (fields, features, categories)
    reconcile.py       cache diffs, layer rebuild decisions
    media.py           attachment file cache keyed by attachment key and variant; extension
                       from the HTTP content type through an allow-list
  qgis_tier/           everything that needs QGIS
    transport.py       QLocalSocket driver for core/protocol.py (main thread, QTimer deadlines)
    connection.py      Connector: discovery scans, one client per build type, reconnect backoff
    cache.py           ProjectCache: one GeoPackage per project, a table per layer schema
    sync.py            ProjectSync: subscribe, full refresh, live ids, schema rebuilds
    controller.py      Controller: ties the above to QgsProject (adopt, re-attach, status)
    layers.py style.py map-layer properties and the default categorized style
    browser.py dock.py indicators.py   Browser entry, CoMapeo panel, stale badges
    media.py           MediaFetcher (QNetworkAccessManager, streamed to disk), MediaDownload
    media_panel.py     the panel's photos and audio for the selected observation
    audio.py           AudioPlayer over QtMultimedia (Qt5 setMedia, Qt6 setSource/QAudioOutput)
tests/                 plain pytest; fake_server.py implements PROTOCOL.md over a real socket,
                       with test hooks: fail_next() injects an error reply, stuck_cursor repeats a cursor
  winpipe.py           Windows named pipes for the fake server and driver
  dev_server.py        starts `node src/dev/cli.ts` in ../comapeo-local-api for tests
  test_real_server.py  runs the core against that dev server; skips when the checkout,
                       its node_modules or Node 22.18+ is missing
  media_server.py      a real HTTP server on 127.0.0.1 for media cases the dev server
                       cannot produce (held replies, a 404 that later succeeds)
tests/qgis/            tests that need QGIS; tests/conftest.py leaves them out elsewhere.
                       conftest.py has a real-widget iface and `wait_until` (runs the event loop)
scripts/qgis-python.sh headless runner for the bundled Python of a local QGIS app
scripts/smoke_e2e.py   drives the real-window smoke check; smoke_in_qgis.py runs inside QGIS (--code)
```

## Rules

- Runtime dependencies: Python standard library, `qgis` and `qgis.PyQt` only. No pip packages, no binaries. Import Qt only through `qgis.PyQt`, never `PyQt5`/`PyQt6` directly.
- One codebase for QGIS 3.40+ (Qt5) and 4.x (Qt6): use scoped enums (`QLocalSocket.LocalSocketState.ConnectedState`), `exec()` not `exec_()`, `errorOccurred`.
- Python 3.9 syntax and standard library at most (QGIS 3.40 allows 3.9): no `match`, no `X | Y` outside annotations, and catch `socket.timeout`, which is not `TimeoutError` before 3.10.
- `core/` must never import `qgis` or PyQt; `tests/test_packaging.py` enforces this.
- Tests check behaviour through public surfaces (bytes on the socket, the core API, resulting QGIS layers), not private functions or mock call counts. Prefer real sockets and real QGIS objects over mocks.
- Unix socket paths must stay under about 100 bytes: tests use `tests.fake_server.short_tempdir()`, not pytest's `tmp_path`.
- Cache downloaded media by attachment key and variant, never by URL; the media server port changes every launch. The attachment lists stored in the cache rows keep the URLs from when the row was written: never fetch from them. When the app is connected, ask it again (`layers.getFeatures` with `ids`) for current URLs.
- Every asynchronous media result in the panel is checked against the selection it was started for (a generation counter); a late reply must never show under a newer selection.
- Only `http` URLs on a loopback host are fetched (`core.media.is_media_url`), with no proxy and no redirects.
- Comments only for a non-obvious why, one line.
- QGIS tests wait with `wait_until` (runs the Qt event loop), never `time.sleep`. They compare QGIS layers with what the server serves over a second connection (`tests/qgis/helpers.Oracle`), not with the plugin's own state. Tests never touch the real QGIS profile (conftest asserts it) and point discovery at an empty folder unless they start a server.

## Gotchas

- Cache tables carry private columns (document id, version, category id, attachments) whose names are chosen per table against the server's column names, case-insensitively, because SQLite compares names that way and any tag key may arrive (`fid`, `geom` included). The renderer keys on the category id column, never the translated category name.
- A map layer is CoMapeo's only when it is an `ogr` layer on a file named after its project id (`cache.cache_file_name`) and carries the `comapeo/projectId` and `comapeo/layerId` custom properties (`layers.comapeo_ids`). The properties alone are not enough: Copy/Paste Style or a QML file copies them onto any layer. In plugin code, decide ownership through `comapeo_ids`/`comapeo_layers`, never by reading the properties directly. Within a project the table wins over `comapeo/layerId` (a style pasted between two CoMapeo layers brings the other's id): the controller's `_resolved_ids` corrects the property from the table the layer is on.
- A schema change creates a new table (`<layer>_<hash of layer id and schema key>`), staged (`staged:<id>` in `comapeo_meta`) while map layers stay on the old one. Only after the full fetch succeeds is it committed, map layers switched with `setDataSource` (carrying the user's `subsetString` across) and the old table dropped; a failed fetch leaves the old table and its refresh time. Saved QGIS projects that still point at a dropped table are re-pointed on load from the layers' `comapeo/projectId` and `comapeo/layerId` custom properties. `Controller.ensure_table` returns any stored table as it is and leaves rebuilds to the sync.
- GeoPackage has no list type: `string_list` columns are stored as JSON; QGIS 4.0 and 4.2 read them back as a `QStringList` field of subtype JSON, and 3.44 gives the same values.
- After writing through the cache's own provider, map layers on the same table need `reload()`; `featureCount()` is stale until then.
- The OGR provider does not convert geometry dimensions: write Point 2D and LineStringM (NaN where a time is missing) to match the table.
- Browser items: QGIS owns what `createChildren` returns, so each child is passed to `sip.transferto(child, None)`. Collections keep the `Fertile` capability: without it `QgsDataItem::refresh` recurses into a kept child with the children of a temporary copy, adopts them, and the copy's destructor then deletes them, so the next refresh crashes QGIS (seen only in a real window, where the tree is expanded while projects and layers arrive). With `Fertile`, QGIS refreshes one level, so the provider refreshes every expanded collection itself. Layer items are created `Populated` so they show no expand arrow. Nothing on the C++ side keeps the Python half of a grandchild alive, so the provider holds a reference to every item it makes (pruned once QGIS deletes it); without that, items reached through `QgsBrowserModel` silently become plain `QgsLayerItem`s. Never call `QgsDataItem.children()` from Python, in plugin code or tests: under PyQt5 (QGIS 3.x) it returns new Python-owned wrappers, and when the list is dropped they delete the C++ items still in the tree, which crashes QGIS later. Collections record the children QGIS keeps (`_Collection.held`, mirroring `QgsDataItem::refresh`, which keeps an existing child equal to a new one). Test the Browser through a real `QgsBrowserModel` (`tests/qgis/test_browser_model.py`), not by calling `createChildren` or `children()` from Python.
- A damaged cache (`ProjectCache.is_damaged`: SQLite says it is not a database, malformed, or lacks `comapeo_meta`; a lock is not damage) is renamed aside before a sync attaches or a table is created, so the rebuild never writes into it. `cached_projects` also lists damaged caches of projects on the map, since their name cannot be read from the file.
- Windows will not delete a file that is open. Map layers removed from the project close their GeoPackage only when deferred deletes run, so `remove_cached_data` flushes them and retries once.
- Windows: Python has no `AF_UNIX` there, so `tests/fake_server.py` and `tests/driver.py` use `tests/winpipe.py`, byte-mode named pipes with overlapped I/O behind a socket-like interface; the oracle in `tests/qgis/helpers.py` uses `QLocalSocket`. Only tests of POSIX file permissions skip on Windows. A dialog in a headless run blocks forever, so `tests/qgis/conftest.py` makes any unexpected `QMessageBox` fail the test.
- `FrameDecoder` refuses a frame whose JSON nests deeper than `MAX_JSON_DEPTH` (64) before parsing it, because Python 3.14 parses nesting that older versions reject with `RecursionError`.
- The protocol has no notification for projects joined or left, so the controller re-lists projects every 10 s (`PROJECT_POLL_MS`); a poll only acts on projects that appeared, disappeared or were renamed, never refetching the ones on the map.
- Discovery: the installed builds' data folders are listed in `core/discovery.APP_FOLDERS`. A development build of desktop is not among them (its data is in its checkout), so it is found only through an extra location: the panel's "Other app locations…" (setting `comapeo/extraDiscoveryPaths`) or `COMAPEO_LOCAL_API_DISCOVERY_EXTRA`. Each extra location is a build keyed `extra:<location>`. The override (`comapeo/discoveryPath` or `COMAPEO_LOCAL_API_DISCOVERY`) replaces every location, extras included; tests rely on that never to read a real app's folder, so keep it that way.
- `layer.reload()` drops the user's selection; the controller re-selects the same documents (`_reload_keeping_selection`), otherwise every refresh would clear the media panel.
- Audio: under `QT_QPA_PLATFORM=offscreen` (the pytest runs) QMediaPlayer stays in `LoadingMedia` on macOS and never plays, so pytest only checks the control and the cached file. Playback is checked in the real-window media smoke run, which samples state and position. Tests open files through `QDesktopServices.setUrlHandler("file", ...)` (the `opened` fixture), so no viewer is ever launched.
- `http.server` looks up the host name on bind, which can take 30 s on macOS; `tests/media_server.py` skips that.
- Several app builds can run at once. The cache records the build that last served a project (`buildType` in the `project` metadata), and a project is reported "No longer available" only once that build has listed its projects without it; while that build is not connected the project reads "Not connected".
