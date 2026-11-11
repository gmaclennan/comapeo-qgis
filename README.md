# CoMapeo for QGIS

A QGIS plugin that shows the observations and tracks of a CoMapeo project as layers, with category icons, photos and audio. It connects to CoMapeo Desktop running on the same computer through the CoMapeo local API. Version 1 is view-only.

Status: early development. Observations and tracks show as read-only layers with category icons, kept in a local cache and updated live. Selecting one observation shows its photos and audio in the CoMapeo panel. Scope and design are in [docs/PLAN.md](docs/PLAN.md). The wire contract is `docs/PROTOCOL.md` in the `comapeo-local-api` repository.

Tested so far on QGIS 4.0 and 4.2 on macOS. QGIS 3.40 and later (Qt5), Windows and Linux are intended but not yet tested.

## Development

```sh
uv sync                                   # creates .venv with pytest and ruff
.venv/bin/pytest                          # tests (QGIS-only tests are skipped)
.venv/bin/ruff check . && .venv/bin/ruff format --check .
uv run --isolated --no-project --python 3.9 --with 'pytest>=8' python -m pytest   # oldest Python QGIS 3.40 allows
```

`tests/test_real_server.py` and most of `tests/qgis/` start the `@comapeo/local-api` dev server from a sibling `../comapeo-local-api` checkout (or `COMAPEO_LOCAL_API_DIR`) with Node 22.18 or later, and skips itself when that is not available. Add `-m "not real_server"` to leave it out.

On macOS with QGIS installed in `/Applications`, run the same tests under QGIS's own Python:

```sh
scripts/qgis-python.sh --install-test-deps   # once per QGIS version
scripts/qgis-python.sh -m pytest
QGIS_VERSION=4.0.2 scripts/qgis-python.sh -m pytest
```

To point the plugin at a development server instead of an installed CoMapeo Desktop, set the QGIS setting `comapeo/discoveryPath`, or the environment variable `COMAPEO_LOCAL_API_DISCOVERY`, to the server's discovery file or the folder holding it. To try it in QGIS by hand:

```sh
cd ../comapeo-local-api && fnm exec --using=24 npm run dev -- --discovery-dir /tmp/comapeo-dev
# then start QGIS with COMAPEO_LOCAL_API_DISCOVERY=/tmp/comapeo-dev and this folder linked
# into the profile's python/plugins as comapeo_qgis
```

Type `create`, `update`, `delete` or `category` into the dev server to see live updates.

`scripts/smoke-e2e.sh --media` checks the photo and audio panel in a real QGIS window the same way, with screenshots prefixed `media-`.

## How it works

The plugin finds a running app through its discovery file, connects over a local socket and keeps a GeoPackage per CoMapeo project in the QGIS profile folder (`comapeo/cache/`). Map layers read from that file, so they still show the last data fetched when the app is closed; they are then marked with a badge in the Layers panel and in the CoMapeo panel. Layers are styled once, when first added, with one category per CoMapeo category; "Reset to CoMapeo style" in the layer's context menu restores it.

Select one observation on a CoMapeo layer and the CoMapeo panel shows its photos as thumbnails; click one for a larger view, and "Open original" downloads the full-size photo and opens it in the system's image viewer. Audio plays in the panel, with "Open in player" beside it in case it does not play there; video opens in the system's player. Photos that have not synced to this computer yet show a "Not yet synced" placeholder and are tried again when you press Refresh, as is a file whose download failed or was cut off (a partial file is never kept). Thumbnails and larger views are fetched when first shown and kept in `comapeo/media/` in the QGIS profile folder, beside the layer cache; originals are only fetched when opened, or for a whole project with "Download all media". With CoMapeo Desktop closed, the panel shows whatever was saved. "Remove cached data" deletes a project's media as well as its layers. There is no size limit on the media folder yet.

## Licence

GPL-2.0-or-later. See [LICENSE](LICENSE).
