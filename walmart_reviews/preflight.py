"""Small startup checks for the standalone Walmart CLI.

The web UI already runs the Amazon preflight before serving; this module is for
``python -m walmart_reviews`` and deliberately does not import amazon_reviews.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

MIN_PYTHON = (3, 10)


def missing_packages() -> list[str]:
    missing = []
    for module, label in (
        ("requests", "requests"),
        ("bs4", "beautifulsoup4"),
        ("lxml", "lxml"),
        ("playwright", "playwright"),
        ("openpyxl", "openpyxl"),
    ):
        try:
            __import__(module)
        except ImportError:
            missing.append(label)
    return missing


def find_browser() -> str | None:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / r"Google\Chrome\Application\chrome.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / r"Google\Chrome\Application\chrome.exe",
            Path(local) / r"Google\Chrome\Application\chrome.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / r"Microsoft\Edge\Application\msedge.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / r"Microsoft\Edge\Application\msedge.exe",
        ]
        for path in candidates:
            if path.exists():
                return str(path)
        return None

    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    for exe in ("google-chrome", "google-chrome-stable", "chromium",
                "chromium-browser", "microsoft-edge"):
        found = shutil.which(exe)
        if found:
            return found
    return None


def report() -> bool:
    ok = True
    print("  Walmart review downloader")
    if sys.version_info < MIN_PYTHON:
        print(f"  !! Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required")
        ok = False
    missing = missing_packages()
    if missing:
        print(f"  !! missing packages: {', '.join(missing)}")
        print("     pip install -r requirements.txt")
        ok = False
    if not find_browser():
        print("  !! Google Chrome or Microsoft Edge was not found")
        print("     Walmart needs a visible, real browser window.")
        ok = False
    return ok
