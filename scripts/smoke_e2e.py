"""End-to-end smoke check in a real QGIS window against the local API dev server.

    scripts/smoke-e2e.sh [--qgis-version 4.2.3] [--out docs/evidence] [--keep] [--media]

1. Starts the dev server with its discovery file in a short temporary folder.
2. Creates a throwaway QGIS profile folder, links the plugin into it and enables it.
3. Launches the QGIS app (a real window) with scripts/smoke_in_qgis.py, which adds
   the project's layers through the Browser, saves screenshots and a report, and
   waits for an observation this driver creates on the server to reach the map.
4. Stops the server and launches QGIS again on the saved project, which must show
   the cached data marked as not connected.

With --media it instead runs scripts/smoke_media_in_qgis.py: the CoMapeo panel
with an observation's photos, a photo not yet synced, audio playback and
"Download all media", with screenshots prefixed `media-`.

With --edit it runs scripts/smoke_edit_in_qgis.py against a dev server with
editing allowed, serving as a member: a field, a category, an attempted move,
a delete and a stale save, through QGIS's own editing actions, with
screenshots prefixed `edit-`.

Exits non-zero if any check fails. QGIS is killed if it hangs.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from comapeo_qgis.core.discovery import find_servers  # noqa: E402
from tests import dev_server  # noqa: E402
from tests.driver import BlockingClient  # noqa: E402

IN_QGIS = ROOT / "scripts" / "smoke_in_qgis.py"
MEDIA_IN_QGIS = ROOT / "scripts" / "smoke_media_in_qgis.py"
EDIT_IN_QGIS = ROOT / "scripts" / "smoke_edit_in_qgis.py"
PLUGIN = "comapeo_qgis"
PROJECT_PREFIX = "Tapajós"


def qgis_executable(version: str) -> Path:
    app = Path(
        os.environ.get("QGIS_APP")
        or f"/Applications/QGIS-final-{version.replace('.', '_')}.app"
    )
    found = sorted((app / "Contents" / "MacOS").glob("QGIS*"))
    if not found:
        raise SystemExit(f"No QGIS executable in {app}")
    return found[0]


def make_profile(profiles: Path) -> None:
    default = profiles / "profiles" / "default"
    plugins = default / "python" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / PLUGIN).symlink_to(ROOT / PLUGIN, target_is_directory=True)
    settings = default / "qgis.org"
    settings.mkdir(parents=True)
    # Without these QGIS may store a random master password in the login keychain.
    ini = (
        "[PythonPlugins]\ncomapeo_qgis=true\n\n"
        "[qgis]\ncheckVersion=false\n\n"
        "[auth]\nuse_password_helper=false\n\n"
        "[authentication]\ngenerate-random-password-for-keychain=false\n"
        "use-password-helper=false\n"
    )
    for name in ("QGIS4.ini", "QGIS3.ini"):
        (settings / name).write_text(ini, encoding="utf-8")


class Oracle:
    """What the server serves, over a connection of its own."""

    def __init__(self, discovery_dir: str) -> None:
        [found] = find_servers(override=discovery_dir)
        assert found.info is not None
        self.client = BlockingClient(
            found.info.socket_path, found.info.secret, request_timeout=60
        )
        self.client.connect()

    def projects(self) -> list[dict[str, Any]]:
        return self.client.call("projects.list")["projects"]

    def counts(self, project_id: str) -> dict[str, int]:
        result = {}
        layers = self.client.call("layers.list", {"projectId": project_id})["layers"]
        for layer in layers:
            total, cursor = 0, None
            while True:
                params: dict[str, Any] = {
                    "projectId": project_id,
                    "layerId": layer["layerId"],
                }
                if cursor is not None:
                    params["cursor"] = cursor
                page = self.client.call("layers.getFeatures", params)
                total += len(page["features"])
                cursor = page["nextCursor"]
                if cursor is None:
                    break
            result[layer["layerId"]] = total
        return result

    def close(self) -> None:
        self.client.close()


class Run:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.out = Path(args.out).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        kind = "media-" if args.media else "edit-" if args.edit else ""
        self.prefix = kind + f"qgis-{args.qgis_version}"
        self.script = (
            MEDIA_IN_QGIS if args.media else EDIT_IN_QGIS if args.edit else IN_QGIS
        )
        self.media_targets: dict[str, Any] = {}
        # Short: the dev server's socket lives under the discovery folder.
        self.work = Path(tempfile.mkdtemp(prefix="cqs-", dir="/tmp"))
        self.discovery = self.work / "d"
        self.discovery.mkdir()
        self.profiles = self.work / "p"
        self.checks: list[dict[str, Any]] = []
        self.server: dev_server.DevServer | None = None

    def check(self, name: str, ok: bool, detail: Any = None) -> bool:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""))
        return ok

    def env(self, mode: str, project_id: str) -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("PYTHON", "QT_", "QGIS_", "GDAL_", "PROJ_"))
        }
        env.update(
            {
                "COMAPEO_LOCAL_API_DISCOVERY": str(self.discovery),
                "COMAPEO_SMOKE_WORK": str(self.work),
                "COMAPEO_SMOKE_OUT": str(self.out),
                "COMAPEO_SMOKE_MODE": mode,
                "COMAPEO_SMOKE_PREFIX": self.prefix,
                "COMAPEO_SMOKE_PROJECT": project_id,
                "COMAPEO_SMOKE_MEDIA": json.dumps(self.media_targets),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONFAULTHANDLER": "1",
            }
        )
        return env

    def launch(
        self, mode: str, project_id: str, extra: list[str], on_tick: Any = None
    ) -> dict[str, Any] | None:
        log_path = self.out / f"{self.prefix}-{mode}-qgis.log"
        command = [
            str(qgis_executable(self.args.qgis_version)),
            "--profiles-path",
            str(self.profiles),
            "--nologo",
            "--noversioncheck",
            *extra,
            "--code",
            str(self.script),
        ]
        with open(log_path, "w", encoding="utf-8") as log:
            proc = subprocess.Popen(
                command,
                env=self.env(mode, project_id),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = time.monotonic() + self.args.timeout
            while proc.poll() is None and time.monotonic() < deadline:
                if on_tick is not None:
                    on_tick()
                time.sleep(0.25)
            timed_out = proc.poll() is None
            if timed_out:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, sig)
                    try:
                        proc.wait(timeout=10)
                        break
                    except subprocess.TimeoutExpired:
                        continue
        self.check(
            f"{mode}: QGIS exited by itself within {self.args.timeout:.0f} s",
            not timed_out,
            None if not timed_out else "killed",
        )
        self.check(f"{mode}: QGIS exit status 0", proc.returncode == 0, proc.returncode)
        scrub_file(log_path)
        report_path = self.out / f"{self.prefix}-{mode}.json"
        if not report_path.exists():
            self.check(f"{mode}: report written", False)
            return None
        return json.loads(report_path.read_text(encoding="utf-8"))

    def common_checks(self, mode: str, report: dict[str, Any]) -> None:
        self.check(f"{mode}: plugin loaded by QGIS", report.get("pluginLoaded"))
        self.check(
            f"{mode}: no failures in the QGIS script",
            not report.get("failures"),
            report.get("failures") or None,
        )
        self.check(
            f"{mode}: no Python exceptions",
            not report.get("exceptions"),
            report.get("exceptions") or None,
        )
        python_errors = [
            m for m in report.get("messageLog", []) if m["tag"] == "Python error"
        ]
        self.check(
            f"{mode}: no Python errors in the QGIS message log",
            not python_errors,
            python_errors or None,
        )
        for layer in report.get("layers", []):
            name = layer["layerId"]
            self.check(f"{mode}: {name} layer valid", layer["valid"])
            self.check(f"{mode}: {name} layer read-only", layer["readOnly"])
            self.check(
                f"{mode}: {name} drawn with the CoMapeo categories",
                layer.get("renderer") == "categorizedSymbol"
                and layer.get("categoryCount", 0) > 1,
                f"{layer.get('categoryCount')} categories",
            )
        for name, fraction in report.get("canvasPaintedFraction", {}).items():
            self.check(
                f"{mode}: {name} screenshot has something drawn",
                fraction > 0.0002,
                f"{fraction:.2%} of pixels",
            )

    def live(self) -> None:
        print("Starting the dev server…")
        self.server = dev_server.start(
            discovery_dir=str(self.discovery),
            observations=self.args.observations,
            tracks=self.args.tracks,
            photos=self.args.photos,
            projects=2,
        )
        oracle = Oracle(str(self.discovery))
        projects = oracle.projects()
        project = next(
            (p for p in projects if (p.get("name") or "").startswith(PROJECT_PREFIX)),
            projects[0],
        )
        project_id = project["projectId"]
        expected = oracle.counts(project_id)
        print(f"Server serves {expected} in {project.get('name')}")
        make_profile(self.profiles)
        state: dict[str, Any] = {}

        def on_tick() -> None:
            if "created" in state or not (self.work / "phase1.json").exists():
                return
            assert self.server is not None
            created = self.server.run("create 1")
            state["created"] = created
            state["after"] = oracle.counts(project_id)
            temp = self.work / "mutated.tmp"
            temp.write_text(json.dumps(created), encoding="utf-8")
            temp.replace(self.work / "mutated.json")

        print(f"Launching QGIS {self.args.qgis_version}…")
        report = self.launch("live", project_id, [], on_tick)
        oracle.close()
        if report is None:
            return
        self.common_checks("live", report)
        self.check(
            "live: Browser items keep their CoMapeo classes",
            {"RootItem", "ProjectItem", "LayerItem"}
            <= set(report.get("browserItemClasses", [])),
            report.get("browserItemClasses"),
        )
        self.check(
            "live: layers added with a mouse double-click in the Browser",
            report.get("addedBy")
            and all("mouse" in v for v in report["addedBy"].values()),
            report.get("addedBy"),
        )
        shown = {
            layer["layerId"]: layer["featuresIterated"]
            for layer in report.get("layers", [])
        }
        self.check(
            "live: every feature the server serves is on the map",
            shown == expected,
            {"map": shown, "server": expected},
        )
        status = report.get("status", {}).get("project", {})
        self.check(
            "live: panel shows the project up to date",
            status.get("connected") and status["describe"].startswith("Up to date"),
            status.get("describe"),
        )
        observations = next(
            (
                lay
                for lay in report.get("layers", [])
                if lay["layerId"] == "observations"
            ),
            {},
        )
        self.check(
            "live: observation categories carry icons",
            observations.get("categoriesWithIcon", 0) > 0,
            f"{observations.get('categoriesWithIcon')} of "
            f"{observations.get('categoryCount')}",
        )
        if "created" in state:
            after = {
                layer["layerId"]: layer["featuresIterated"]
                for layer in report.get("layersAfterUpdate", [])
            }
            self.check(
                "live: an observation created on the server reaches the map",
                report.get("createdDocId") == state["created"]["docId"]
                and after == state["after"],
                {
                    "seconds": report.get("liveUpdateSeconds"),
                    "map": after,
                    "server": state["after"],
                },
            )
        else:
            self.check("live: mutation sent", False, "QGIS never reached phase 1")
        self.expected_offline = state.get("after", expected)
        self.project_id = project_id

    def offline(self) -> None:
        if self.server is not None:
            self.server.stop()
            self.server = None
        project_file = self.work / "smoke.qgz"
        if not self.check("offline: saved project exists", project_file.exists()):
            return
        print("Launching QGIS on the saved project with the server stopped…")
        report = self.launch(
            "offline", self.project_id, ["--project", str(project_file)]
        )
        if report is None:
            return
        self.common_checks("offline", report)
        shown = {
            layer["layerId"]: layer["featuresIterated"]
            for layer in report.get("layers", [])
        }
        self.check(
            "offline: cached features drawn",
            shown == self.expected_offline,
            {"map": shown, "cachedFromServer": self.expected_offline},
        )
        status = report.get("status", {}).get("project", {})
        self.check(
            "offline: project reads as not connected",
            not status.get("connected")
            and status.get("describe", "").startswith(
                "Not connected. Showing data cached on"
            ),
            status.get("describe"),
        )
        badges = report.get("status", {}).get("layerTreeBadges", {})
        self.check(
            "offline: every CoMapeo layer has a stale badge",
            badges and all(badges.values()),
            badges,
        )

    def media(self) -> None:
        print("Starting the dev server…")
        self.server = dev_server.start(
            discovery_dir=str(self.discovery),
            observations=self.args.observations,
            tracks=self.args.tracks,
            photos=self.args.photos,
            projects=1,
        )
        oracle = Oracle(str(self.discovery))
        project_id = oracle.projects()[0]["projectId"]
        targets = pick_media(oracle, project_id)
        oracle.close()
        self.media_targets = targets
        print(f"Media targets: {targets}")
        make_profile(self.profiles)
        print(f"Launching QGIS {self.args.qgis_version}…")
        report = self.launch("media", project_id, [])
        if report is None:
            return
        self.check("media: plugin loaded by QGIS", report.get("pluginLoaded"))
        self.check(
            "media: no failures in the QGIS script",
            not report.get("failures"),
            report.get("failures") or None,
        )
        self.check(
            "media: no Python exceptions",
            not report.get("exceptions"),
            report.get("exceptions") or None,
        )
        python_errors = [
            m for m in report.get("messageLog", []) if m["tag"] == "Python error"
        ]
        self.check("media: no Python errors in the log", not python_errors)
        thumbnails = report.get("thumbnails", {})
        self.check(
            "media: every photo shows a thumbnail",
            thumbnails.get("captions") == [""] * targets["photoCount"]
            and not any(thumbnails.get("iconsNull", [True])),
            thumbnails,
        )
        preview = report.get("preview", {})
        self.check(
            "media: a click shows a larger view",
            preview.get("width", 0) > 96
            and preview.get("labelVisible")
            and preview.get("labelHeight", 0) >= preview.get("height", 1),
            preview,
        )
        opened = report.get("openedOriginal", {})
        self.check(
            "media: Open original hands the downloaded original to the system",
            opened.get("name") == "original.jpg"
            and opened.get("bytes") == targets["originalBytes"],
            opened,
        )
        self.check(
            "media: a photo not yet synced shows the placeholder",
            "Not yet synced" in report.get("notSynced", {}).get("captions", []),
            report.get("notSynced"),
        )
        audio = report.get("audio", {})
        samples = audio.get("samples", [])
        positions = [s["position"] for s in samples if s["state"] == "playing"]
        self.check(
            "media: audio cached as .wav and played in the panel",
            audio.get("file") == "original.wav" and len(set(positions)) >= 3,
            {"file": audio.get("file"), "positions": positions},
        )
        self.check(
            "media: playback reaches the end",
            any(s["mediaStatus"] == "EndOfMedia" for s in samples),
            samples[-1] if samples else None,
        )
        self.check(
            "media: hints for no and several selected",
            report.get("hintNoSelection", "").startswith("Select an observation")
            and "observations selected" in report.get("hintSeveral", ""),
            [report.get("hintNoSelection"), report.get("hintSeveral")],
        )
        self.check(
            "media: Download all media reports what it did",
            "downloaded" in report.get("downloadAll", ""),
            report.get("downloadAll"),
        )

    def edit(self) -> None:
        print("Starting the dev server with editing allowed…")
        self.server = dev_server.start(
            discovery_dir=str(self.discovery),
            observations=self.args.observations,
            tracks=self.args.tracks,
            photos=self.args.photos,
            projects=1,
            allow_edits=True,
            as_member=True,
        )
        oracle = Oracle(str(self.discovery))
        project_id = oracle.projects()[0]["projectId"]
        before = features(oracle, project_id)
        categories = oracle.client.call("categories.list", {"projectId": project_id})
        plant = next(
            c["categoryId"] for c in categories["categories"] if c["name"] == "Plant"
        )
        mine = [
            i
            for i in self.server.run("list own 20")["ids"]
            if not str(before[i]["properties"]["notes"]).startswith("An example")
        ]
        spring = next(
            i
            for i, f in before.items()
            if f["editable"] and f["properties"]["notes"] == "An example Spring"
        )
        targets = {
            "field": mine[0],
            "move": mine[1],
            "delete": mine[2],
            "stale": mine[3],
            "category": spring,
            "plant": plant,
        }
        self.media_targets = targets
        make_profile(self.profiles)
        state: dict[str, Any] = {}

        def on_tick() -> None:
            ask = self.work / "stale.json"
            if "stale" in state or not ask.exists():
                return
            assert self.server is not None
            state["stale"] = self.server.run(f"update {targets['stale']}")
            done = self.work / "stale-done.tmp"
            done.write_text(json.dumps(state["stale"]), encoding="utf-8")
            done.replace(self.work / "stale-done.json")

        print(f"Launching QGIS {self.args.qgis_version}…")
        report = self.launch("edit", project_id, [], on_tick)
        after = features(oracle, project_id)
        oracle.close()
        if report is None:
            return
        self.check("edit: plugin loaded by QGIS", report.get("pluginLoaded"))
        self.check(
            "edit: no failures in the QGIS script",
            not report.get("failures"),
            report.get("failures") or None,
        )
        self.check(
            "edit: no Python exceptions",
            not report.get("exceptions"),
            report.get("exceptions") or None,
        )
        python_errors = [
            m for m in report.get("messageLog", []) if m["tag"] == "Python error"
        ]
        self.check("edit: no Python errors in the log", not python_errors)
        self.check(
            "edit: Toggle Editing starts a session, with the attribute table open",
            report.get("editingStarted") and report.get("attributeTableOpen"),
        )
        saves = report.get("saves", {})
        field = after.get(targets["field"], {})
        self.check(
            "edit: a field saved through the layer reaches CoMapeo",
            field.get("properties", {}).get("notes") == "Checked from QGIS"
            and report.get("fieldAfter") == "Checked from QGIS"
            and "1 change saved to CoMapeo." in saves.get("field", {}).get("bar", []),
            saves.get("field"),
        )
        self.check(
            "edit: a category change reaches CoMapeo",
            after.get(targets["category"], {}).get("categoryId") == plant
            and report.get("categoryAfter") == "Plant",
            {"bar": saves.get("category"), "shown": report.get("categoryAfter")},
        )
        moved = after.get(targets["move"], {})
        self.check(
            "edit: an attempted move is undone at once and never saved",
            moved.get("version") == before[targets["move"]]["version"]
            and report.get("moveAfter") == report.get("moveBefore")
            and report.get("moveLeftUnsaved") is False
            and any(
                "change of location was undone" in m for m in report.get("shown", [])
            ),
            {"after": report.get("moveAfter"), "shown": report.get("shown")},
        )
        shown_rows = report.get("tableRows") or []
        others = {i for i in shown_rows if not before.get(i, {}).get("editable")}
        self.check(
            "edit: the attribute table greys exactly the other device's rows",
            bool(others)
            and len(others) < len(shown_rows)
            and set(report.get("greyRows") or []) == others,
            {"grey": len(report.get("greyRows") or []), "others": len(others)},
        )
        self.check(
            "edit: a delete reaches CoMapeo",
            targets["delete"] not in after and not report.get("deletedStillShown"),
            saves.get("delete"),
        )
        stale = after.get(targets["stale"], {})
        self.check(
            "edit: a save of an observation changed in CoMapeo is refused",
            "stale" in state
            and stale.get("version") == state["stale"]["versionId"]
            and report.get("staleAfter") == stale.get("properties", {}).get("notes")
            and any(
                "changed in CoMapeo since QGIS" in m for m in report.get("shown", [])
            ),
            {"bar": saves.get("stale"), "shown": report.get("staleAfter")},
        )
        self.check(
            "edit: Toggle Editing ends the session", report.get("editingStopped")
        )

    def close(self) -> None:
        if self.server is not None:
            self.server.stop()
            self.server = None
        if not self.args.keep:
            shutil.rmtree(self.work, ignore_errors=True)


def http_status(url: str) -> tuple[int, int]:
    """Status and body size; only the dev server's own 127.0.0.1 URLs come here."""
    try:
        with urllib.request.urlopen(url, timeout=20) as reply:  # noqa: S310
            return reply.status, len(reply.read())
    except urllib.error.HTTPError as err:
        return err.code, 0


def pick_media(oracle: Oracle, project_id: str) -> dict[str, Any]:
    """Observations to select: photos that all load, a photo not yet synced, audio."""
    targets: dict[str, Any] = {}
    cursor = None
    while True:
        params: dict[str, Any] = {"projectId": project_id, "layerId": "observations"}
        if cursor is not None:
            params["cursor"] = cursor
        page = oracle.client.call("layers.getFeatures", params)
        for feature in page["features"]:
            attachments = feature["attachments"]
            photos = [a for a in attachments if a["type"] == "photo"]
            statuses = [http_status(a["urls"]["original"]) for a in attachments]
            loads = all(code == 200 for code, _size in statuses)
            if len(photos) >= 2 and loads and "photos" not in targets:
                targets["photos"] = feature["id"]
                targets["photoCount"] = len(photos)
                targets["originalBytes"] = http_status(photos[0]["urls"]["original"])[1]
            if photos and not loads and "notSynced" not in targets:
                targets["notSynced"] = feature["id"]
            if loads and any(a["type"] == "audio" for a in attachments):
                targets.setdefault("audio", feature["id"])
        cursor = page["nextCursor"]
        if cursor is None:
            break
    missing = {"photos", "notSynced", "audio"} - set(targets)
    if missing:
        raise SystemExit(f"The dev server served no observation for {missing}")
    return targets


def features(oracle: Oracle, project_id: str) -> dict[str, Any]:
    found: dict[str, Any] = {}
    cursor = None
    while True:
        params: dict[str, Any] = {"projectId": project_id, "layerId": "observations"}
        if cursor is not None:
            params["cursor"] = cursor
        page = oracle.client.call("layers.getFeatures", params)
        found.update({f["id"]: f for f in page["features"]})
        cursor = page["nextCursor"]
        if cursor is None:
            return found


def scrub_file(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    path.write_text(text.replace(str(Path.home()), "~"), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--qgis-version", default="4.2.3")
    parser.add_argument("--out", default=str(ROOT / "docs" / "evidence"))
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--observations", type=int, default=150)
    parser.add_argument("--tracks", type=int, default=4)
    parser.add_argument("--photos", type=int, default=6)
    parser.add_argument("--keep", action="store_true", help="keep the work folder")
    parser.add_argument(
        "--media", action="store_true", help="check the photo and audio panel"
    )
    parser.add_argument(
        "--edit", action="store_true", help="check editing with QGIS's own tools"
    )
    args = parser.parse_args()
    reason = dev_server.skip_reason()
    if reason:
        print(f"Cannot run: {reason}", file=sys.stderr)
        return 2
    run = Run(args)
    try:
        if args.media:
            run.media()
        elif args.edit:
            run.edit()
        else:
            run.live()
            if hasattr(run, "project_id"):
                run.offline()
    finally:
        run.close()
    summary = run.out / f"{run.prefix}-summary.json"
    failed = [c for c in run.checks if not c["ok"]]
    summary.write_text(
        json.dumps({"checks": run.checks, "failed": len(failed)}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"{len(run.checks) - len(failed)} passed, {len(failed)} failed")
    if args.keep:
        print(f"Work folder kept: {run.work}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
