#!/usr/bin/env bash
# Creates the venv and installs all dependencies.
# Run once: ./setup.sh

set -euo pipefail
cd "$(dirname "$0")"

echo "Fetching submodules (open-pit-wall, f1-race-replay)..."
git submodule update --init --recursive

echo "Creating virtual environment..."
python3 -m venv .venv

echo "Installing dependencies..."
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt

echo "Installing Open Pit Wall from submodule..."
.venv/bin/pip install -e vendor/open-pit-wall

echo ""
echo "Done. Run ./start_replay.sh or ./start_live.sh to get started."
