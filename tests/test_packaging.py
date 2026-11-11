import configparser
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "comapeo_qgis"

BLOCK_QT = """
import importlib, importlib.abc, pathlib, sys

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("qgis", "PyQt5", "PyQt6", "sip"):
            raise ImportError("blocked: " + name)

sys.path.insert(0, sys.argv[1])
sys.meta_path.insert(0, Block())
for module in sorted(pathlib.Path(sys.argv[1], "comapeo_qgis/core").glob("*.py")):
    importlib.import_module("comapeo_qgis.core." + module.stem)
import comapeo_qgis
print("ok")
"""


def test_core_imports_without_qgis_or_qt():
    out = subprocess.run(
        [sys.executable, "-s", "-c", BLOCK_QT, str(ROOT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


REQUIRED_METADATA = (
    "name",
    "qgisMinimumVersion",
    "description",
    "about",
    "version",
    "author",
    "email",
    "repository",
)


def metadata():
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(PLUGIN / "metadata.txt", encoding="utf-8")
    return parser["general"]


def test_metadata_declares_what_plugins_qgis_org_needs():
    general = metadata()
    for key in REQUIRED_METADATA:
        assert general.get(key), key
    assert general["name"] == "CoMapeo"
    assert general["qgisMinimumVersion"] == "3.40"
    assert general["qgisMaximumVersion"] == "4.99"
    assert general.getboolean("experimental")
    assert (PLUGIN / general["icon"]).is_file()


@pytest.mark.xfail(
    strict=True, reason="contact email, repository and tracker not decided yet"
)
def test_metadata_has_no_placeholders_left():
    general = metadata()
    for key in (*REQUIRED_METADATA, "tracker", "homepage"):
        assert general.get(key, "").strip().upper() != "TODO", key


def test_licence_is_gpl_2():
    text = (ROOT / "LICENSE").read_text()
    assert "GNU GENERAL PUBLIC LICENSE" in text
    assert "Version 2, June 1991" in text
