#!/bin/sh
# Run Python under the interpreter bundled with a local macOS QGIS app, headless.
#
#   scripts/qgis-python.sh script.py [args...]
#   scripts/qgis-python.sh -m pytest tests
#
# QGIS_VERSION=4.0.2 picks /Applications/QGIS-final-4_0_2.app (default 4.2.3);
# QGIS_APP=/path/to/QGIS.app overrides the app entirely.
# Packages installed with `scripts/qgis-python.sh --install-test-deps` go into
# .qgis-pytest/<version>/ in this repo, never into the app bundle.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
VERSION=${QGIS_VERSION:-4.2.3}
APP=${QGIS_APP:-/Applications/QGIS-final-$(echo "$VERSION" | tr . _).app}
A=$APP/Contents

if [ ! -d "$A" ]; then
	echo "QGIS app not found: $APP" >&2
	exit 1
fi

PYTHON=$(ls "$A"/MacOS/python3.* 2>/dev/null | head -n 1)
# 4.0.2 ships its site-packages under python3.11 although the interpreter is 3.12.
SITE=$(ls -d "$A"/Resources/python3*/site-packages 2>/dev/null | head -n 1)
DEPS=$ROOT/.qgis-pytest/$(basename "$APP" .app)

export QT_PLUGIN_PATH="$A/PlugIns"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
export PYTHONHOME="$A/Frameworks"
export PYTHONPATH="$ROOT:$SITE:$DEPS${PYTHONPATH:+:$PYTHONPATH}"
export QGIS_PREFIX_PATH="$APP"
export GDAL_DATA="$A/Resources/qgis/gdal"
export PROJ_DATA="$A/Resources/qgis/proj"
export PYTHONDONTWRITEBYTECODE=1

if [ "${1:-}" = "--install-test-deps" ]; then
	exec "$PYTHON" -s -m pip install --quiet --disable-pip-version-check \
		--target "$DEPS" --upgrade pytest
fi

exec "$PYTHON" -s "$@"
