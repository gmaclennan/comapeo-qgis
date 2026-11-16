"""Starts the `@comapeo/local-api` dev server (`node src/dev/cli.ts`) for tests.

Uses a sibling checkout, or COMAPEO_LOCAL_API_DIR, and Node 22.18+ (COMAPEO_NODE,
else fnm when available, else `node`). `skip_reason()` says why it cannot run here.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from tests.fake_server import short_tempdir

SERVER_DIR = Path(
    os.environ.get(
        "COMAPEO_LOCAL_API_DIR",
        Path(__file__).resolve().parents[2] / "comapeo-local-api",
    )
)
START_TIMEOUT = 120


def node_command() -> list[str] | None:
    candidates = []
    if os.environ.get("COMAPEO_NODE"):
        candidates.append([os.environ["COMAPEO_NODE"]])
    if shutil.which("fnm"):
        candidates.append(["fnm", "exec", "--using=24", "node"])
    if shutil.which("node"):
        candidates.append(["node"])
    for command in candidates:
        try:
            out = subprocess.run(
                [*command, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            ).stdout
            major, minor = (int(p) for p in out.strip().lstrip("v").split(".")[:2])
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        # Running .ts directly needs type stripping, on by default from 22.18.
        if (major, minor) >= (22, 18):
            return command
    return None


class DevServer:
    def __init__(
        self,
        command: list[str],
        *,
        observations: int = 25,
        tracks: int = 3,
        photos: int = 4,
        projects: int = 1,
        discovery_dir: str | None = None,
    ) -> None:
        self._owns_discovery_dir = discovery_dir is None
        self.discovery_dir = discovery_dir or short_tempdir()
        self.stderr = tempfile.TemporaryFile()  # noqa: SIM115 - closed in stop()
        self.lines: queue.Queue[str] = queue.Queue()
        self.proc = subprocess.Popen(
            [
                *command,
                "src/dev/cli.ts",
                "--discovery-dir",
                self.discovery_dir,
                "--observations",
                str(observations),
                "--tracks",
                str(tracks),
                "--photos",
                str(photos),
                "--projects",
                str(projects),
            ],
            cwd=SERVER_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            text=True,
            start_new_session=True,
        )
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line.rstrip("\n"))

    def next_line(self, prefix: tuple[str, ...], timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.proc.poll() is not None:
                raise RuntimeError(f"dev server gave no {prefix} line:\n{self.log()}")
            try:
                line = self.lines.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if line.startswith(prefix):
                return line

    def log(self) -> str:
        self.stderr.seek(0)
        return self.stderr.read().decode(errors="replace")[-2000:]

    def command(self, text: str) -> str:
        assert self.proc.stdin is not None
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()
        line = self.next_line(("OK ", "ERROR "), timeout=30)
        assert line.startswith("OK "), line
        return line

    def stop(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            assert self.proc.stdin is not None
            self.proc.stdin.write("quit\n")
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._kill()
        self.reader.join(timeout=5)
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        self.stderr.close()
        if self._owns_discovery_dir:
            shutil.rmtree(self.discovery_dir, ignore_errors=True)

    def _kill(self) -> None:
        if os.name != "posix":
            self.proc.kill()
            self.proc.wait(timeout=10)
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, sig)
            try:
                self.proc.wait(timeout=10)
                return
            except subprocess.TimeoutExpired:
                continue

    def run(self, text: str) -> dict:
        """Run a stdin command (create, update, delete, category); parse its reply."""
        return json.loads(self.command(text)[3:])

    def wait_ready(self) -> None:
        self.next_line(("READY ",), timeout=START_TIMEOUT)


def skip_reason() -> str | None:
    if not (SERVER_DIR / "src" / "dev" / "cli.ts").is_file():
        return f"no @comapeo/local-api checkout at {SERVER_DIR}"
    if not (SERVER_DIR / "node_modules").is_dir():
        return "@comapeo/local-api dependencies are not installed"
    if node_command() is None:
        return "Node 22.18 or later not found"
    return None


def start(**kwargs) -> DevServer:
    command = node_command()
    assert command is not None
    server = DevServer(command, **kwargs)
    try:
        server.wait_ready()
    except BaseException:
        server.stop()
        raise
    return server
