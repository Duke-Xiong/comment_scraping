#!/usr/bin/env bash
# ===================================================================
#  Amazon / Walmart Review Downloader - reproducible uv launcher
#  Run with:   ./run-uv.sh
#
#  Requires uv: https://docs.astral.sh/uv/
#  uv sync --frozen restores the exact versions in uv.lock, including
#  Python 3.13 if it is not already installed.
# ===================================================================
cd "$(dirname "$0")" || exit 1

echo
echo "  ==============================================="
echo "    Amazon / Walmart Review Downloader (uv)"
echo "  ==============================================="
echo

if ! command -v uv >/dev/null 2>&1; then
  echo "  uv was not found."
  echo
  echo "  Install it first:"
  echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo
  echo "  Then run this file again."
  echo
  exit 1
fi

echo "  Restoring the exact environment from uv.lock..."
if ! uv sync --frozen; then
  echo
  echo "  Could not restore dependencies."
  echo "  Check your network, then run:  uv sync --frozen"
  echo
  exit 1
fi

echo "  Starting the app..."
echo
uv run python -m webapp.server
CODE=$?

echo
if [ "$CODE" -ne 0 ]; then
  echo "  The app stopped because of a problem (see above)."
else
  echo "  The app has closed. Your files are in the 'output' folder."
fi
echo
exit "$CODE"
