"""Find the discovery files written by running CoMapeo apps."""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

DISCOVERY_FILENAME = "comapeo-local-api.json"
DISCOVERY_VERSION = 1
ENV_OVERRIDE = "COMAPEO_LOCAL_API_DISCOVERY"
MAX_DISCOVERY_FILE_SIZE = 64 * 1024
# The server's secret is 43 characters; anything far longer cannot be its own.
MAX_SECRET_LENGTH = 1024
_MAX_PID = 2**31 - 1

BUILD_PRODUCTION = "production"
BUILD_RC = "release-candidate"
BUILD_INTERNAL = "internal"
BUILD_DEVELOPMENT = "development"
BUILD_OVERRIDE = "override"

# Electron's userData folder is named after the product name of each build type.
APP_FOLDERS: tuple[tuple[str, str], ...] = (
    (BUILD_PRODUCTION, "CoMapeo Desktop"),
    (BUILD_RC, "CoMapeo Desktop RC"),
    (BUILD_INTERNAL, "CoMapeo Desktop Internal"),
    (BUILD_DEVELOPMENT, "CoMapeo Desktop Dev"),
)


class InvalidDiscoveryFile(ValueError):
    pass


@dataclass(frozen=True)
class DiscoveryInfo:
    socket_path: str
    secret: str = field(repr=False)
    protocol_versions: tuple[int, ...]
    pid: int | None = None
    app_name: str | None = None
    app_version: str | None = None


@dataclass(frozen=True)
class Candidate:
    build_type: str
    path: PurePath


@dataclass(frozen=True)
class DiscoveryResult:
    """A discovery file that exists. Exactly one of `info` and `error` is set."""

    build_type: str
    path: PurePath
    info: DiscoveryInfo | None = None
    error: str | None = None
    stale: bool = False

    @property
    def usable(self) -> bool:
        return self.info is not None and not self.stale


@dataclass
class Environment:
    """What discovery reads from the system; injectable so every OS is testable."""

    platform: str = field(default_factory=lambda: sys.platform)
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    home: str = field(default_factory=lambda: str(Path.home()))


def parse_discovery(text: str | bytes) -> DiscoveryInfo:
    try:
        data = json.loads(text)
    except (ValueError, UnicodeDecodeError, RecursionError) as err:
        raise InvalidDiscoveryFile(f"not valid JSON: {err}") from err
    if not isinstance(data, dict):
        raise InvalidDiscoveryFile("not a JSON object")
    version = data.get("discoveryVersion")
    if version != DISCOVERY_VERSION or isinstance(version, bool):
        raise InvalidDiscoveryFile(f"unsupported discoveryVersion {version!r}")
    socket_path = data.get("socketPath")
    secret = data.get("secret")
    versions = data.get("protocolVersions")
    if not isinstance(socket_path, str) or not socket_path:
        raise InvalidDiscoveryFile("missing socketPath")
    if not isinstance(secret, str) or not secret:
        raise InvalidDiscoveryFile("missing secret")
    if len(secret) > MAX_SECRET_LENGTH:
        raise InvalidDiscoveryFile("secret is too long")
    if not isinstance(versions, list) or not all(_is_int(v) for v in versions):
        raise InvalidDiscoveryFile("protocolVersions must be a list of integers")
    pid = data.get("pid")
    app = data.get("app") if isinstance(data.get("app"), dict) else {}
    return DiscoveryInfo(
        socket_path=socket_path,
        secret=secret,
        protocol_versions=tuple(versions),
        pid=pid if _is_int(pid) and 0 < pid <= _MAX_PID else None,
        app_name=_str_or_none(app.get("name")),
        app_version=_str_or_none(app.get("version")),
    )


def app_data_folders(environment: Environment) -> list[PurePath]:
    """The folders Electron uses as `appData` on this platform."""
    env = environment.env
    if environment.platform.startswith("win"):
        appdata = env.get("APPDATA")
        if appdata:
            return [PureWindowsPath(appdata)]
        return [PureWindowsPath(environment.home, "AppData", "Roaming")]
    home = PurePosixPath(environment.home)
    if environment.platform == "darwin":
        return [home / "Library" / "Application Support"]
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg and PurePosixPath(xdg).is_absolute():
        return [PurePosixPath(xdg)]
    return [home / ".config"]


def candidate_paths(
    environment: Environment | None = None, override: str | None = None
) -> list[Candidate]:
    """Discovery file paths to try, most specific first.

    An override (the setting, else the environment variable) may name the file
    or the folder holding it, and replaces the per-app locations.
    """
    environment = environment or Environment()
    chosen = override or environment.env.get(ENV_OVERRIDE)
    if chosen:
        path = _pure(environment, chosen)
        if path.suffix.lower() == ".json":
            return [Candidate(BUILD_OVERRIDE, path)]
        return [
            Candidate(BUILD_OVERRIDE, path / DISCOVERY_FILENAME),
            Candidate(BUILD_OVERRIDE, path),
        ]
    candidates = []
    for base in app_data_folders(environment):
        for build_type, folder in APP_FOLDERS:
            user_data = base / folder
            for directory in (user_data / "comapeo", user_data):
                candidates.append(Candidate(build_type, directory / DISCOVERY_FILENAME))
    return candidates


def find_servers(
    environment: Environment | None = None,
    override: str | None = None,
    *,
    read_file: Callable[[PurePath], bytes | None] | None = None,
    is_alive: Callable[[DiscoveryInfo], bool] | None = None,
) -> list[DiscoveryResult]:
    """Every discovery file found, one per build type.

    Within a build type the first usable file wins; failing that, the first
    file found. Unreadable, corrupt or insecure files are reported with
    `error`; files whose server is evidently gone are reported with
    `stale=True`. Callers connect only to `usable` results and still treat a
    failed connection as "not running".
    """
    environment = environment or Environment()
    read_file = read_file or _read_file
    is_alive = is_alive or (lambda info: _process_looks_alive(info, environment))
    chosen: dict[str, DiscoveryResult] = {}
    for candidate in candidate_paths(environment, override):
        current = chosen.get(candidate.build_type)
        if current is not None and current.usable:
            continue
        result = _examine(candidate, read_file, is_alive)
        if result is not None and (current is None or result.usable):
            chosen[candidate.build_type] = result
    return list(chosen.values())


def _examine(
    candidate: Candidate,
    read_file: Callable[[PurePath], bytes | None],
    is_alive: Callable[[DiscoveryInfo], bool],
) -> DiscoveryResult | None:
    try:
        raw = read_file(candidate.path)
    except OSError as err:
        return _error(candidate, f"cannot read: {err.strerror or err}")
    except InvalidDiscoveryFile as err:
        return _error(candidate, str(err))
    if raw is None:
        return None
    try:
        info = parse_discovery(raw)
    except InvalidDiscoveryFile as err:
        return _error(candidate, str(err))
    return DiscoveryResult(
        candidate.build_type, candidate.path, info=info, stale=not is_alive(info)
    )


def _error(candidate: Candidate, message: str) -> DiscoveryResult:
    return DiscoveryResult(candidate.build_type, candidate.path, error=message)


def _read_file(path: PurePath) -> bytes | None:
    # Windows raises PermissionError, not IsADirectoryError, when opening a folder.
    if os.path.isdir(path):
        return None
    try:
        with open(path, "rb") as f:
            _check_private(os.fstat(f.fileno()))
            data = f.read(MAX_DISCOVERY_FILE_SIZE + 1)
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return None
    if len(data) > MAX_DISCOVERY_FILE_SIZE:
        raise OSError(f"larger than {MAX_DISCOVERY_FILE_SIZE} bytes")
    return data


def _check_private(st: os.stat_result) -> None:
    """The secret is only trusted from a file only this user could have written."""
    if os.name != "posix":
        return
    if st.st_uid != os.getuid():
        raise InvalidDiscoveryFile("discovery file belongs to another user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise InvalidDiscoveryFile(
            "discovery file is readable or writable by other users"
        )


def _process_looks_alive(info: DiscoveryInfo, environment: Environment) -> bool:
    # Only POSIX can check cheaply; os.kill(pid, 0) on Windows sends CTRL_C_EVENT.
    if environment.platform.startswith("win"):
        return True
    if not os.path.exists(info.socket_path):
        return False
    if info.pid is None or info.pid <= 0:
        return True
    try:
        os.kill(info.pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError):
        return True
    return True


def _pure(environment: Environment, path: str) -> PurePath:
    expanded = path
    if path == "~" or path[:2] in ("~/", "~\\"):
        expanded = environment.home + path[1:]
    if environment.platform.startswith("win"):
        return PureWindowsPath(expanded)
    return PurePosixPath(expanded)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None
