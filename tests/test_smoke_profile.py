"""The throwaway QGIS profile the real-window smoke check runs in."""

from __future__ import annotations

import configparser
import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def smoke_module():
    spec = importlib.util.spec_from_file_location(
        "smoke_e2e", ROOT / "scripts" / "smoke_e2e.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(os.name == "nt", reason="the smoke check is macOS only")
def test_the_smoke_profile_keeps_qgis_out_of_the_login_keychain(tmp_path):
    smoke_module().make_profile(tmp_path)
    settings = tmp_path / "profiles" / "default" / "qgis.org"
    for name in ("QGIS4.ini", "QGIS3.ini"):
        ini = configparser.ConfigParser(interpolation=None)
        ini.read(settings / name, encoding="utf-8")
        assert ini.getboolean("PythonPlugins", "comapeo_qgis")
        assert not ini.getboolean("auth", "use_password_helper")
        authentication = ini["authentication"]
        assert not authentication.getboolean("generate-random-password-for-keychain")
        assert not authentication.getboolean("use-password-helper")
