import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from comapeo_qgis.core.discovery import (
    BUILD_DEVELOPMENT,
    BUILD_INTERNAL,
    BUILD_OVERRIDE,
    BUILD_PRODUCTION,
    BUILD_RC,
    DISCOVERY_FILENAME,
    ENV_OVERRIDE,
    Environment,
    InvalidDiscoveryFile,
    candidate_paths,
    find_servers,
    parse_discovery,
)
from tests.fake_server import short_tempdir

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX files")

VALID = {
    "discoveryVersion": 1,
    "socketPath": "/tmp/comapeo-1a2b3c/api.sock",
    "secret": "abc",
    "protocolVersions": [1],
    "pid": 12345,
    "app": {"name": "CoMapeo Desktop", "version": "1.2.3"},
}


def always_alive(_info):
    return True


def test_parses_the_example_from_the_protocol_document():
    info = parse_discovery(json.dumps(VALID))
    assert info.socket_path == "/tmp/comapeo-1a2b3c/api.sock"
    assert info.secret == "abc"
    assert info.protocol_versions == (1,)
    assert info.pid == 12345
    assert (info.app_name, info.app_version) == ("CoMapeo Desktop", "1.2.3")


def test_ignores_unknown_properties_and_optional_ones_missing():
    data = {k: VALID[k] for k in ("discoveryVersion", "socketPath", "secret")}
    data |= {"protocolVersions": [1, 2], "future": {"x": 1}}
    info = parse_discovery(json.dumps(data).encode())
    assert info.protocol_versions == (1, 2)
    assert info.pid is None and info.app_name is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{trunc",
        "[]",
        json.dumps({**VALID, "discoveryVersion": 2}),
        json.dumps({**VALID, "discoveryVersion": True}),
        json.dumps({**VALID, "socketPath": ""}),
        json.dumps({**VALID, "secret": None}),
        json.dumps({**VALID, "protocolVersions": "1"}),
        json.dumps({**VALID, "protocolVersions": [True]}),
        json.dumps({**VALID, "secret": "x" * 5000}),
        "[" * 50_000 + "]" * 50_000,
        b"\xff\xfe",
    ],
)
def test_rejects_corrupt_or_unsupported_files(text):
    with pytest.raises(InvalidDiscoveryFile):
        parse_discovery(text)


def test_macos_locations_for_every_build_type():
    env = Environment(platform="darwin", env={}, home="/Users/ana")
    paths = candidate_paths(env)
    base = PurePosixPath("/Users/ana/Library/Application Support")
    assert paths[0].path == base / "CoMapeo Desktop" / "comapeo" / DISCOVERY_FILENAME
    assert paths[1].path == base / "CoMapeo Desktop" / DISCOVERY_FILENAME
    by_type = {c.build_type: c.path.parent for c in paths[::2]}
    assert by_type == {
        BUILD_PRODUCTION: base / "CoMapeo Desktop" / "comapeo",
        BUILD_RC: base / "CoMapeo Desktop RC" / "comapeo",
        BUILD_INTERNAL: base / "CoMapeo Desktop Internal" / "comapeo",
        BUILD_DEVELOPMENT: base / "CoMapeo Desktop Dev" / "comapeo",
    }


def test_windows_uses_appdata():
    env = Environment(
        platform="win32", env={"APPDATA": r"C:\Users\ana\AppData\Roaming"}, home="x"
    )
    first = candidate_paths(env)[0].path
    assert first == PureWindowsPath(
        r"C:\Users\ana\AppData\Roaming\CoMapeo Desktop\comapeo", DISCOVERY_FILENAME
    )


def test_windows_without_appdata_falls_back_to_the_home_folder():
    env = Environment(platform="win32", env={}, home=r"C:\Users\ana")
    assert str(candidate_paths(env)[0].path).startswith(
        r"C:\Users\ana\AppData\Roaming\CoMapeo Desktop"
    )


def test_linux_honours_xdg_config_home_only_when_absolute():
    env = Environment(platform="linux", env={"XDG_CONFIG_HOME": "/cfg"}, home="/h")
    assert str(candidate_paths(env)[0].path).startswith("/cfg/CoMapeo Desktop/")
    env = Environment(platform="linux", env={"XDG_CONFIG_HOME": "rel"}, home="/h")
    assert str(candidate_paths(env)[0].path).startswith("/h/.config/CoMapeo Desktop/")


def test_override_from_environment_variable_replaces_app_locations():
    env = Environment(platform="darwin", env={ENV_OVERRIDE: "/dev/api"}, home="/h")
    paths = candidate_paths(env)
    assert [c.build_type for c in paths] == [BUILD_OVERRIDE, BUILD_OVERRIDE]
    assert paths[0].path == PurePosixPath("/dev/api") / DISCOVERY_FILENAME


def test_setting_takes_precedence_over_environment_and_may_name_the_file():
    env = Environment(platform="darwin", env={ENV_OVERRIDE: "/env"}, home="/h")
    paths = candidate_paths(env, override="~/dev/server.json")
    assert [c.path for c in paths] == [PurePosixPath("/h/dev/server.json")]


def fake_fs(files):
    def read(path):
        value = files.get(str(path))
        if isinstance(value, Exception):
            raise value
        return value

    return read


def test_finds_one_result_per_build_type_and_reports_corrupt_files():
    env = Environment(platform="darwin", env={}, home="/h")
    base = "/h/Library/Application Support"
    files = {
        f"{base}/CoMapeo Desktop/comapeo/{DISCOVERY_FILENAME}": json.dumps(VALID),
        f"{base}/CoMapeo Desktop/{DISCOVERY_FILENAME}": json.dumps(VALID),
        f"{base}/CoMapeo Desktop RC/{DISCOVERY_FILENAME}": "{half",
        f"{base}/CoMapeo Desktop Dev/comapeo/{DISCOVERY_FILENAME}": PermissionError(
            13, "Permission denied"
        ),
    }
    results = find_servers(env, read_file=fake_fs(files), is_alive=always_alive)
    assert [(r.build_type, r.usable) for r in results] == [
        (BUILD_PRODUCTION, True),
        (BUILD_RC, False),
        (BUILD_DEVELOPMENT, False),
    ]
    assert results[0].path.parent.name == "comapeo"
    assert "JSON" in results[1].error
    assert "Permission denied" in results[2].error


def test_dead_server_is_reported_stale():
    env = Environment(platform="darwin", env={ENV_OVERRIDE: "/x.json"}, home="/h")
    files = {"/x.json": json.dumps(VALID)}
    [result] = find_servers(env, read_file=fake_fs(files), is_alive=lambda info: False)
    assert result.stale and not result.usable and result.info is not None


def test_nothing_running_finds_nothing():
    env = Environment(platform="linux", env={}, home="/h")
    assert find_servers(env, read_file=fake_fs({})) == []


def write_private(path: Path, data) -> Path:
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    path.chmod(0o600)
    return path


def test_real_files_on_disk_with_directory_override(tmp_path: Path):
    sock = tmp_path / "api.sock"
    sock.touch()
    write_private(
        tmp_path / DISCOVERY_FILENAME,
        {**VALID, "socketPath": str(sock), "pid": os.getpid()},
    )
    [result] = find_servers(override=str(tmp_path))
    assert result.usable
    assert result.path == tmp_path / DISCOVERY_FILENAME


def test_real_oversized_file_is_reported_not_parsed(tmp_path: Path):
    write_private(tmp_path / DISCOVERY_FILENAME, " " * (65 * 1024))
    [result] = find_servers(override=str(tmp_path))
    assert result.error and not result.usable


def test_real_file_with_a_missing_socket_is_stale(tmp_path: Path):
    write_private(
        tmp_path / DISCOVERY_FILENAME,
        {**VALID, "socketPath": str(tmp_path / "gone.sock")},
    )
    [result] = find_servers(override=str(tmp_path))
    assert result.stale


def test_the_secret_never_appears_in_repr():
    info = parse_discovery(json.dumps({**VALID, "secret": "s3cr3t-value"}))
    env = Environment(platform="darwin", env={ENV_OVERRIDE: "/x.json"}, home="/h")
    files = {"/x.json": json.dumps({**VALID, "secret": "s3cr3t-value"})}
    [result] = find_servers(env, read_file=fake_fs(files), is_alive=always_alive)
    assert result.info == info
    for text in (repr(info), str(info), repr(result)):
        assert "s3cr3t-value" not in text
    assert info.secret == "s3cr3t-value"


@pytest.mark.parametrize("pid", [2**40, 2**70, -1, 0, 1.5])
def test_out_of_range_pids_are_treated_as_unknown(pid):
    info = parse_discovery(json.dumps({**VALID, "pid": pid}))
    assert info.pid is None


@posix_only
@pytest.mark.parametrize("pid", [2**40, 2**70])
def test_out_of_range_pid_does_not_break_the_real_liveness_check(tmp_path, pid):
    sock = tmp_path / "api.sock"
    sock.touch()
    write_private(
        tmp_path / DISCOVERY_FILENAME, {**VALID, "socketPath": str(sock), "pid": pid}
    )
    [result] = find_servers(override=str(tmp_path))
    assert result.usable


def two_location_files(first, second):
    base = "/h/Library/Application Support/CoMapeo Desktop"
    return {
        f"{base}/comapeo/{DISCOVERY_FILENAME}": first,
        f"{base}/{DISCOVERY_FILENAME}": second,
    }


def alive_unless_dead(info):
    return info.secret != "dead"


@pytest.mark.parametrize(
    "first",
    [json.dumps({**VALID, "secret": "dead"}), "{corrupt", PermissionError(13, "no")],
)
def test_a_usable_file_beats_a_stale_or_broken_one_of_the_same_build(first):
    env = Environment(platform="darwin", env={}, home="/h")
    files = two_location_files(first, json.dumps({**VALID, "secret": "live"}))
    [result] = find_servers(env, read_file=fake_fs(files), is_alive=alive_unless_dead)
    assert result.usable and result.info.secret == "live"
    assert result.path.parent.name == "CoMapeo Desktop"


def test_when_nothing_is_usable_the_first_file_found_is_reported():
    env = Environment(platform="darwin", env={}, home="/h")
    files = two_location_files("{corrupt", json.dumps({**VALID, "secret": "dead"}))
    [result] = find_servers(env, read_file=fake_fs(files), is_alive=alive_unless_dead)
    assert result.error and result.path.parent.name == "comapeo"


def test_empty_override_folder_finds_nothing(tmp_path: Path):
    assert find_servers(override=str(tmp_path)) == []


@posix_only
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o602])
def test_a_discovery_file_other_users_can_read_or_write_is_refused(tmp_path, mode):
    sock = tmp_path / "api.sock"
    sock.touch()
    path = write_private(
        tmp_path / DISCOVERY_FILENAME,
        {**VALID, "socketPath": str(sock), "pid": os.getpid()},
    )
    path.chmod(mode)
    [result] = find_servers(override=str(tmp_path))
    assert not result.usable and result.info is None
    assert "other users" in result.error


@posix_only
def test_a_dead_pid_with_its_socket_file_left_behind_is_stale():
    directory = short_tempdir()
    try:
        sock_path = os.path.join(directory, "api.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(sock_path)
        listener.close()
        child = subprocess.run(
            [sys.executable, "-c", "import os; print(os.getpid())"],
            capture_output=True,
            text=True,
            check=True,
        )
        dead_pid = int(child.stdout)
        write_private(
            Path(directory, DISCOVERY_FILENAME),
            {**VALID, "socketPath": sock_path, "pid": dead_pid},
        )
        assert os.path.exists(sock_path)
        [result] = find_servers(override=directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    assert result.stale and not result.usable
