#!/bin/sh
# Smoke check in a real QGIS window against the @comapeo/local-api dev server.
# See scripts/smoke_e2e.py for options. macOS only for now.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON=$ROOT/.venv/bin/python
[ -x "$PYTHON" ] || PYTHON=python3
exec "$PYTHON" "$ROOT/scripts/smoke_e2e.py" "$@"
