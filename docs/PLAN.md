# CoMapeo for QGIS: plan and scope

Agreed 11 Nov 2026. Background and evidence are in `docs/research/`.

## Goal

A QGIS plugin that shows the observations and tracks of a CoMapeo project as layers over the user's own QGIS map, with category icons and viewable photos and audio. It connects to CoMapeo Desktop running on the same computer. Editing follows as a second phase.

## Scope

### In, version 1 (view only)

- QGIS and CoMapeo Desktop on the same machine, same operating-system user. macOS, Windows, Linux.
- QGIS 3.44 (long-term release, Qt5) and 4.x (Qt6) from one codebase. Minimum 3.40.
- Observations (points) and tracks (lines). Deleted documents left out. Observations with no location kept as rows without geometry.
- Category icon per observation, category colour for tracks (the tracks legend lists only categories in use, since the protocol does not say which categories apply to tracks), applied as a default style on add and then left alone. Icons embedded in the QGIS project. "Reset to CoMapeo style" action.
- One attribute column per field defined in the project's categories, plus a catch-all column of all tags, plus built-in columns.
- A CoMapeo side panel: connection status, last updated, refresh, and the selected observation's photos (thumbnails, larger view) and audio.
- A local cache per CoMapeo project in the QGIS user profile, so layers still show the last data fetched when the app is closed.
- Manual refresh and refresh on reconnect first; automatic updates when data syncs into desktop as a following step.
- Published on plugins.qgis.org under GPL-2.0-or-later.

### Phase 2 (editing)

- Change field values and notes, change category, move a point, delete an observation. This matches what desktop's own UI allows.
- The role rule (members edit only their own observations) is enforced by the local API, not the plugin.
- A save is refused if the observation changed in CoMapeo since QGIS fetched it; the layer is refreshed. QGIS never creates a fork.

### Out

- Remote access: cloud server REST API, another machine on the network, mobile.
- Creating observations, changing attachments, editing tracks.
- Starting or stopping sync from QGIS.
- Remote detection alerts (can be added later as another layer).
- A "split by category" option. Standardised per-structure layers will arrive through the layer definitions described below.

## Architecture

Three pieces in three repositories.

```
QGIS plugin (Python)  ──local socket──▶  @comapeo/local-api (Node)  ──▶  MapeoManager
        │                                  mounted by CoMapeo Desktop
        └──HTTP 127.0.0.1──▶ core's existing media server (photos, audio)
```

### 1. `@comapeo/local-api` (new npm package, folder `comapeo-local-api`)

Takes a `MapeoManager` and serves a small, versioned API on a local socket (Unix domain socket on macOS/Linux, named pipe on Windows). It is purpose-built, not the full core API over `@comapeo/ipc`, for three reasons: it cannot invite, leave projects or sync; its contract does not move with ipc's wire format; and because it runs beside core it can subscribe to core's `updated-docs` event directly, so live updates need no ipc change.

Wire format: 4-byte unsigned little-endian length + UTF-8 JSON, in a JSON-RPC style (requests with ids, responses, server notifications).

Handshake: the server sends nothing until the client's first message presents the protocol versions it speaks and a secret. A wrong secret closes the connection. A version mismatch returns a clear error so the plugin can say "update CoMapeo Desktop" or "update the plugin".

The full contract is `docs/PROTOCOL.md` in the `comapeo-local-api` repo; that file is the source of truth. Methods in outline (version 1):

| Method | Returns |
|---|---|
| `hello` | agreed protocol version, server name and version, app name and version |
| `projects.list` | id, name, colour, description |
| `layers.list(projectId, lang)` | layer definitions: id, title, document type, geometry type, columns (name, type, label, options) |
| `layers.getFeatures(projectId, layerId, lang, limit, cursor, ids)` | paged rows: id, version, geometry, column values, attachments with media URLs |
| `categories.list(projectId, lang)` | id, name, colour, icon (SVG inline, PNG fallback) |
| `subscribe(projectId)`, `unsubscribe(projectId)` | then notifications: `layer.changed` (ids of features created, updated or deleted) and `layer.schemaChanged` (columns or categories changed) |
| phase 2: `features.update`, `features.delete` | new version, or a conflict / not-permitted error |

There is no notification for projects joined or left, and `hello` has no build-type field (its app name carries the build, such as "CoMapeo Desktop Internal"): the plugin re-lists projects every 10 seconds, and tells builds apart by the location its discovery file was found in. Installed builds keep it in their data folder under the system's app-data folder; development builds keep their data in the desktop checkout, so the user lists such locations in the panel or in `COMAPEO_LOCAL_API_DISCOVERY_EXTRA`, and each listed location counts as a build of its own.

A layer is a definition (document type + filter + columns) supplied by the server. Version 1 returns exactly two per project: all observations, all tracks. When CoMapeo standardises its data structures, the server can return more definitions and existing plugins pick them up.

The package includes a dev command that serves a generated test project from an in-memory core, so the plugin can be built and tested without desktop.

### 2. CoMapeo Desktop change (worktree `comapeo-desktop-local-api`, branch `feat/local-api`, from `origin/main`)

- A setting, off by default ("Allow QGIS to connect" or similar).
- When on: mount `@comapeo/local-api` in the core utility process, giving it the app's data folder as its discovery directory. The package itself generates the secret and writes the discovery file (socket path, secret, protocol versions; readable only by the user), and removes it on close, so desktop, the dev command and tests share one code path.
- Socket path: a short path under the system temp directory inside a folder only the user can open (the data folder path is too long for the socket limit); on Windows a named pipe with a random suffix.
- Not pushed and no pull request until Gregor says so. The settings screen, wording and translation need the desktop team; raise this early.

### 3. QGIS plugin (this repo, plugin name "CoMapeo")

Three tiers, kept separate so most logic is testable without QGIS:

- `core/` (no QGIS imports): protocol client state machine, row-to-feature mapping, cache reconciliation (diff of ids and versions), discovery-file lookup.
- `qgis_tier/`: `QLocalSocket` transport on the main thread, GeoPackage cache and layers, default styling, Browser panel entry (CoMapeo → project → layers; one entry per running build type), side panel, media cache.
- Plugin entry point and metadata.

Data handling:

- Cache: one GeoPackage per CoMapeo project in the QGIS profile folder, shared by every QGIS project that uses it. Not beside the `.qgz`, so sending someone a project file does not hand over the data.
- Layers are flagged read-only in version 1.
- Built-in columns in plain snake_case: id, category, created and updated time, created-by and updated-by device names, GPS accuracy, altitude, photo and audio counts. A `comapeo_` prefix only where a tag key collides. Field columns are named by tag key and are not translated; display values and labels follow the QGIS interface language where CoMapeo has a translation.
- Multi-value fields are list columns. When categories change, the layer schema is rebuilt on refresh.
- Tracks: one line per track with start time, end time and category; per-point timestamps as the geometry's M value.
- Media: thumbnails and preview-size images cached on demand; originals only on "open original" or "download all media"; no size limit in version 1. A 404 on a known attachment is shown as "not yet synced" and retried on refresh. Video opens in the system player. Audio plays in the panel if playback proves reliable, otherwise opens externally.
- If a cached project is no longer available in CoMapeo, its layers are marked disconnected and the plugin offers to delete the cached data. It does not delete automatically. "Remove cached data" is always available.
- A cache file that exists but cannot be read is renamed aside (`<file>.damaged-<time>`) and downloaded again once the app is connected; map layers and their styling stay and are pointed at the new tables. Windows will not rename an open file, so a damaged file the map layers still hold open (a GeoPackage lacking its metadata table) is repaired in place there. Offline, the project is listed with a message naming "Remove cached data".

## Testing

- Plain pytest for `core/`.
- pytest under QGIS's own Python (`tests/qgis/`, with its own fixtures rather than `pytest-qgis`) in the official QGIS Docker images for 3.44 and 4.x, and in OSGeo4W QGIS 3.44 and 4.x on Windows.
- End-to-end in CI against the real `@comapeo/local-api` dev server with an in-memory core, on Linux, macOS and, over a named pipe, Windows. CI fails if any of these tests is skipped.
- Desktop branch: a test in desktop's Playwright suite that switches the setting on and connects a small client, proving listener, secret file and discovery in the real app.
- Full chain (QGIS → built desktop branch): a scripted local smoke test on a developer Mac, not in CI.
- Tooling: `pyproject.toml`, ruff, `qgis-plugin-ci` for packaging and release.

## Milestones

| # | Work | Done when |
|---|---|---|
| 1 | Spikes | Done. Named pipes and Unix sockets work under Qt5 and Qt6 inside real QGIS on Windows and Linux; multimedia modules import. See `docs/research/2026-11-11-local-socket-spike.md`. |
| 2 | `@comapeo/local-api`, read-only | Handshake with secret, the read methods and `subscribe` pass tests against an in-memory core on all three platforms; dev command serves a test project. |
| 3 | Plugin: connect, Browser entry, layers, cache, icons | Against the dev command, a project's observations and tracks appear with category icons in QGIS 3.44 and 4.x; reopening with the server stopped shows cached data marked stale. |
| 4 | Media panel | Selecting an observation shows its photos and plays or opens its audio; unsynced media shows a placeholder. |
| 5 | Desktop branch | With the setting on in a locally built desktop, the plugin finds and connects to it; Playwright test passes. |
| 6 | Live updates | A document created or synced in desktop appears in QGIS without pressing refresh. |
| 7 | Editing (phase 2) | The agreed edits round-trip; a stale save is refused; a member cannot edit another's observation. |

## Risks and open points

- **Desktop release dependency.** Until desktop ships the setting, the plugin only works against the dev command or a local build.
- **Data outside CoMapeo's control.** The cache is an unencrypted file. The opt-in setting is the consent point; wording should say so.
- **Windows pipe permissions.** Another local user can open the pipe read-only. The secret handshake is what protects the data; the server must send nothing before it.
- **Media server has no token.** Core's HTTP server on `127.0.0.1` serves blobs to any local process that finds the port. This is existing behaviour and unchanged by this work.
- **Audio playback.** Modules import everywhere tested, but playback is unproven, and the Qt5 and Qt6 player APIs differ.
- **Core behaviours to verify for phase 2.** Whether an update from a stale version forks or errors; that core does not enforce the role rule itself.
- **Not yet tested.** Electron's Node as the socket server; the client inside a running QGIS window on Windows.
- **Spike repo.** `gmaclennan/comapeo-qgis-spike` can be deleted once this repo's Windows CI job replaces it.
