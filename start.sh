#!/usr/bin/env bash
# SupaVic DLNA on Linux / macOS (from source): creates .venv, installs the dependencies and runs
# the server in the foreground (Ctrl+C to stop). Control panel: http://localhost:8200/
# To run it as a service see contrib/supavic-dlna.service
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
    echo "[1/2] Creating .venv with $($PY --version)"
    "$PY" -m venv .venv
fi
if ! cmp -s requirements.txt .venv/requirements.ok; then
    echo "[2/2] Installing dependencies"
    .venv/bin/python -m pip install --disable-pip-version-check -q --upgrade pip
    .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
    cp requirements.txt .venv/requirements.ok
fi
exec .venv/bin/python launcher.py --sin-bandeja "$@"
