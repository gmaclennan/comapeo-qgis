# CoMapeo for QGIS

A QGIS plugin that shows the observations and tracks of a CoMapeo project as layers, with category icons, photos and audio. It connects to CoMapeo Desktop running on the same computer through the CoMapeo local API. Version 1 is view-only.

Status: early development. Observations and tracks show as read-only layers with category icons, kept in a local cache and updated live; the photo and audio panel is not built yet. Scope and design are in [docs/PLAN.md](docs/PLAN.md). The wire contract is `docs/PROTOCOL.md` in the `comapeo-local-api` repository.

Continuous integration runs the tests on QGIS 3.44 (Qt5) and the current 4.x release (Qt6), on Linux (the official `qgis/qgis` Docker images) and Windows (OSGeo4W), against the `@comapeo/local-api` dev server; on Windows the plugin connects to it over a named pipe. The plain-Python tests also run on macOS, Linux and Windows under Python 3.9 and 3.12. QGIS 4.0 and 4.2 on macOS are tested locally, and by hand in a real QGIS window. Not yet tested: QGIS 3.40, the QGIS macOS app in CI, and CoMapeo Desktop itself (only the dev server). On Windows the tests that use the Python fake server (faults, schema rebuilds, several app builds) are skipped, because it needs Unix sockets.

## Development

```sh
uv sync                                   # creates .venv with pytest and ruff
.venv/bin/pytest                          # tests (QGIS-only tests are skipped)
.venv/bin/ruff check . && .venv/bin/ruff format --check .
uv run --isolated --no-project --python 3.9 --with 'pytest>=8' python -m pytest   # oldest Python QGIS 3.40 allows
```

`tests/test_real_server.py` and most of `tests/qgis/` start the `@comapeo/local-api` dev server from a sibling `../comapeo-local-api` checkout (or `COMAPEO_LOCAL_API_DIR`) with Node 22.18 or later, and skips itself when that is not available. Add `-m "not real_server"` to leave it out. CI sets `COMAPEO_REQUIRE_SERVER=1`, which turns any such skip into a failure, and `COMAPEO_REQUIRE_QGIS=1`, which makes a missing QGIS an error rather than leaving `tests/qgis/` out.

On macOS with QGIS installed in `/Applications`, run the same tests under QGIS's own Python:

```sh
scripts/qgis-python.sh --install-test-deps   # once per QGIS version
scripts/qgis-python.sh -m pytest
QGIS_VERSION=4.0.2 scripts/qgis-python.sh -m pytest
```

The plugin looks for the discovery file of each installed build ("CoMapeo Desktop", "CoMapeo Desktop RC", "CoMapeo Desktop Internal") in its data folder: `~/Library/Application Support/<build>/comapeo/` on macOS, `%APPDATA%\<build>\comapeo\` on Windows, and `$XDG_CONFIG_HOME/<build>/comapeo/` (or `~/.config/…`) on Linux. A build kept elsewhere, such as a development build of CoMapeo Desktop (whose data folder is `data/default` in its checkout, or `USER_DATA_PATH`), is found once its data folder, its `comapeo` folder or its discovery file is listed under "Other app locations…" in the CoMapeo panel, or in the environment variable `COMAPEO_LOCAL_API_DISCOVERY_EXTRA` (separated by `:`, or `;` on Windows). These are searched as well as the installed builds.

To look only in one place, for example at a development server and nothing else, set the QGIS setting `comapeo/discoveryPath`, or the environment variable `COMAPEO_LOCAL_API_DISCOVERY`, to the server's discovery file or the folder holding it; the tests do this so they never find a real app. To try it in QGIS by hand:

```sh
cd ../comapeo-local-api && fnm exec --using=24 npm run dev -- --discovery-dir /tmp/comapeo-dev
# then start QGIS with COMAPEO_LOCAL_API_DISCOVERY=/tmp/comapeo-dev and this folder linked
# into the profile's python/plugins as comapeo_qgis
```

Type `create`, `update`, `delete` or `category` into the dev server to see live updates.

## How it works

The plugin finds a running app through its discovery file, connects over a local socket and keeps a GeoPackage per CoMapeo project in the QGIS profile folder (`comapeo/cache/`). Map layers read from that file, so they still show the last data fetched when the app is closed; they are then marked with a badge in the Layers panel and in the CoMapeo panel. A layer whose first download did not finish says so in its own badge; the project's row in the panel names those layers. Layers are styled once, when first added, with one category per CoMapeo category (for tracks, only the categories tracks use, adding others as they appear); "Reset to CoMapeo style" in the layer's context menu restores it.

## Licence

GPL-2.0-or-later. See [LICENSE](LICENSE).
