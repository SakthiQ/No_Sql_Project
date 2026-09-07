#!/usr/bin/env bash
# Recreate the development environment from scratch.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[web,dev]"
echo "OK — activate with: source .venv/bin/activate"
