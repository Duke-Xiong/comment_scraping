#!/usr/bin/env bash
# ===================================================================
#  Amazon Review Downloader - macOS / Linux
#  Run with:   ./run.sh
#  If it says "permission denied", first run:   chmod +x run.sh
# ===================================================================
cd "$(dirname "$0")" || exit 1

echo
echo "  ==============================================="
echo "    Amazon Review Downloader"
echo "  ==============================================="
echo

# --- find Python -----------------------------------------------------
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "  Python 3.10 or newer is needed but was not found."
  echo
  echo "  macOS:   install from https://www.python.org/downloads/"
  echo "           (or, if you use Homebrew:  brew install python)"
  echo "  Ubuntu:  sudo apt install python3 python3-venv"
  echo
  exit 1
fi

# --- first-run setup -------------------------------------------------
if [ ! -x ".venv/bin/python" ]; then
  echo "  First-time setup. A few minutes, once only."
  echo "  Please leave this window open."
  echo
  echo "  [1 of 2] Preparing..."
  if ! "$PY" -m venv .venv; then
    echo
    echo "  Could not create the workspace."
    echo "  On Ubuntu/Debian you may need:  sudo apt install python3-venv"
    echo
    exit 1
  fi

  echo "  [2 of 2] Downloading components (this is the slow part)..."
  .venv/bin/python -m pip install --upgrade pip --quiet --disable-pip-version-check

  if ! .venv/bin/python -m pip install -r requirements.txt --quiet --disable-pip-version-check; then
    echo
    echo "  That failed. Retrying - some work networks inspect secure"
    echo "  connections, which needs an extra setting..."
    echo
    if ! .venv/bin/python -m pip install -r requirements.txt --quiet \
         --disable-pip-version-check \
         --trusted-host pypi.org --trusted-host files.pythonhosted.org \
         --trusted-host pypi.python.org; then
      echo
      echo "  Could not download the components."
      echo
      echo "  This is almost always the network blocking it. Try:"
      echo "    - a different network (phone hotspot), then run again"
      echo "    - or your company VPN, then run again"
      echo "    - or ask IT to allow pypi.org and files.pythonhosted.org"
      echo
      echo "  Nothing is broken - just run this again later."
      echo
      exit 1
    fi
  fi

  echo
  echo "  Setup complete."
  echo
fi

# --- go --------------------------------------------------------------
.venv/bin/python -m webapp.server
CODE=$?

echo
if [ "$CODE" -ne 0 ]; then
  echo "  The app stopped because of a problem (see above)."
else
  echo "  The app has closed. Your files are in the 'output' folder."
fi
echo
