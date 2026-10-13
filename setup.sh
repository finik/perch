#!/usr/bin/env bash
# Fetch Owl and give it its own Python environment. Safe to run again.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "Perch needs uv to install Owl: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

git submodule update --init vendor/Owl
(cd vendor/Owl && uv sync --quiet)
vendor/Owl/.venv/bin/python -c "import owlplanner, highspy, openpyxl" \
  && echo "Owl is ready. Start Perch with: python3 perch.py"
