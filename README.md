# CoMapeo for QGIS

A QGIS plugin that shows the observations and tracks of a CoMapeo project as layers, with category icons, photos and audio, and lets you edit observations. It connects to CoMapeo Desktop running on the same computer through the CoMapeo local API.

Status: early development. Observations and tracks show as layers with category icons, kept in a local cache and updated live. Selecting one observation shows its photos and audio in the CoMapeo panel. Where CoMapeo Desktop allows it, observations can be edited with QGIS's normal editing tools (see [Editing](#editing)). Scope and design are in [docs/PLAN.md](docs/PLAN.md). The wire contract is `docs/PROTOCOL.md` in the `comapeo-local-api` repository.

Continuous integration runs the tests on QGIS 3.44 (Qt5) and the current 4.x release (Qt6), on Linux (the official `qgis/qgis` Docker images) and Windows (OSGeo4W), against the `@comapeo/local-api` dev server; on Windows the plugin connects to it over a named pipe. The plain-Python tests also run on macOS, Linux and Windows under Python 3.9 and 3.12. QGIS 4.0 and 4.2 on macOS are tested locally, and by hand in a real QGIS window. Not yet tested: QGIS 3.40, the QGIS macOS app in CI, and CoMapeo Desktop itself (only the dev server). On Windows the Python fake server and test driver use named pipes, so the same tests run there; only checks of POSIX file permissions are skipped.

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

Type `create`, `update`, `delete` or `category` into the dev server to see live updates. Add `--allow-edits` to try editing, and `--as-member` to serve as a participant of a project another, in-process device created (`help` lists the commands for that device).

`scripts/smoke-e2e.sh --media` checks the photo and audio panel in a real QGIS window the same way, with screenshots prefixed `media-`.

`scripts/desktop-smoke.mjs` runs a similar check against a packaged CoMapeo Desktop build rather than the dev server. It switches "Allow QGIS to Connect" on in the app's settings, creates test data there, and checks that QGIS finds the app through its standard discovery folder, shows a new observation without a refresh, and reads the project as not connected once the app quits. Build an internal desktop first (`out/` in its checkout), then on macOS with Node 24:

```sh
node scripts/desktop-smoke.mjs --desktop <desktop checkout> --plugin <this repo> --out <dir> --work <dir>
```

The app and QGIS both run under a throwaway HOME inside `--work`, which is emptied first, so neither touches your own CoMapeo data, keychain or QGIS profile. `--plugin` defaults to this repo.

## How it works

The plugin finds a running app through its discovery file, connects over a local socket and keeps a GeoPackage per CoMapeo project in the QGIS profile folder (`comapeo/cache/`). Map layers read from that file, so they still show the last data fetched when the app is closed; they are then marked with a badge in the Layers panel and in the CoMapeo panel. A layer whose first download did not finish says so in its own badge; the project's row in the panel names those layers. Layers are styled once, when first added, with one category per CoMapeo category (for tracks, only the categories tracks use, adding others as they appear); "Reset to CoMapeo style" in the layer's context menu restores it.

Select one observation on a CoMapeo layer and the CoMapeo panel shows its photos as thumbnails; click one for a larger view, and "Open original" downloads the full-size photo and opens it in the system's image viewer. Audio plays in the panel, with "Open in player" beside it in case it does not play there; video opens in the system's player. Photos that have not synced to this computer yet show a "Not yet synced" placeholder and are tried again when you press Refresh, as is a file whose download failed or was cut off (a partial file is never kept). Thumbnails and larger views are fetched when first shown and kept in `comapeo/media/` in the QGIS profile folder, beside the layer cache; originals are only fetched when opened, or for a whole project with "Download all media". With CoMapeo Desktop closed, the panel shows whatever was saved. "Remove cached data" deletes a project's media as well as its layers. There is no size limit on the media folder yet.

## Editing

When CoMapeo Desktop allows editing from QGIS (a separate setting, off unless switched on), the observations layer can be edited with Toggle Editing, the attribute table or form, Delete Selected and Save Layer Edits. You can change notes and the answers to the fields of an observation's own category, change its category (the "Category" column, a list of the project's categories) and delete it. Choices offer the field's options and "(no value)". Not offered yet: creating observations, moving a point, editing date and multiple-choice fields, and editing tracks. A point you move goes straight back, with a message. A change the plugin cannot send (a new feature, a field the observation's category does not have, a locked column) is discarded at save with a message saying so.

Saving sends each changed observation to CoMapeo Desktop, which checks it and answers; QGIS waits for the answers (up to 15 seconds) and the layer then shows what CoMapeo holds. QGIS itself never writes your edits to the cache. A change is refused if the observation changed in CoMapeo since QGIS showed it (including a version arriving from another device), if your role does not allow it (a participant can change only observations created on this device; others are shown in grey in the attribute table), or if CoMapeo does not accept the value; the message bar says how many changes were saved and, under "Show more", why each of the others was not. If CoMapeo Desktop is not connected, has editing switched off, or is still answering an earlier save, the save is stopped and your edits stay in the edit session. While a layer has unsaved edits its live updates wait, so what you edit is what you saw. Above 500 changes the plugin asks before saving. If the connection is lost during a save, or CoMapeo Desktop does not answer in time, the plugin checks which of the unconfirmed changes reached CoMapeo and lists those that did not. When a save is stopped, QGIS adds its own "Could not commit changes" message, saying that a feature was not added because its geometry type does not fit: that is how the plugin stops QGIS writing the edits itself, and it can be ignored. Turning the plugin off or reloading it during an edit session discards the unsaved edits, with a message saying how many.

A CoMapeo Desktop that predates editing leaves the layers read-only, and the plugin says to update it.

`scripts/smoke-e2e.sh --edit` checks editing in a real QGIS window against the dev server (screenshots prefixed `edit-`). `scripts/desktop-smoke.mjs --edit` does the same against a packaged CoMapeo Desktop: the layer is read-only with only "Allow QGIS to Connect", then "Allow QGIS to Edit Observations" is ticked in desktop's Settings and the plugin reconnects by itself; a field and the notes are changed and read back in desktop's observation screen, a save is refused after desktop changed the observation meanwhile, an observation is deleted and one given another category, a move is undone, and unticking the setting leaves the layer read-only again (files prefixed `desktop-edit-`).

## Licence

GPL-2.0-or-later. See [LICENSE](LICENSE).
