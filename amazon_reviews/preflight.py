"""Startup checks, written for someone who has never opened a terminal.

Every problem this catches would otherwise surface as a Python traceback five
minutes into a run. Each check returns plain English and, where possible, a fix
the user can carry out without admin rights - which matters on a work laptop.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

MIN_PYTHON = (3, 10)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    fatal: bool = True


# --------------------------------------------------------------------------
# Browser discovery
# --------------------------------------------------------------------------

def _windows_browser_paths() -> list[Path]:
    local = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    return [
        Path(pf) / r"Google\Chrome\Application\chrome.exe",
        Path(pf86) / r"Google\Chrome\Application\chrome.exe",
        Path(local) / r"Google\Chrome\Application\chrome.exe",
        Path(pf86) / r"Microsoft\Edge\Application\msedge.exe",
        Path(pf) / r"Microsoft\Edge\Application\msedge.exe",
    ]


def find_browser() -> str | None:
    """Return a path/name for an installed Chrome or Edge, else None."""
    if sys.platform == "win32":
        for path in _windows_browser_paths():
            if path.exists():
                return str(path)
        return None

    if sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
        for c in candidates:
            if Path(c).exists():
                return c
        return None

    for exe in ("google-chrome", "google-chrome-stable", "chromium",
                "chromium-browser", "microsoft-edge"):
        found = shutil.which(exe)
        if found:
            return found
    return None


# --------------------------------------------------------------------------
# Port
# --------------------------------------------------------------------------

def find_free_port(preferred: int = 5000, tries: int = 40) -> int:
    """Return a usable localhost port.

    Port 5000 is a common collision (AirPlay on macOS, other dev servers), and
    "Address already in use" is not a message this audience can act on.
    """
    for port in range(preferred, preferred + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    # Let the OS pick anything at all.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def check_python() -> Check:
    ok = sys.version_info >= MIN_PYTHON
    return Check(
        "Python version",
        ok,
        detail=f"found {sys.version_info.major}.{sys.version_info.minor}",
        fix=(
            f"This needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer. "
            "Install it from the Microsoft Store (search 'Python') - that does "
            "not need administrator rights."
        ),
    )


def check_dependencies() -> Check:
    missing = []
    for mod, label in (
        ("requests", "requests"), ("bs4", "beautifulsoup4"), ("lxml", "lxml"),
        ("flask", "flask"), ("playwright", "playwright"), ("openpyxl", "openpyxl"),
    ):
        try:
            __import__(mod)
        except ImportError:
            missing.append(label)
    return Check(
        "Required packages",
        not missing,
        detail="all present" if not missing else f"missing: {', '.join(missing)}",
        fix=(
            "Close this window and run the start file again - it installs these "
            "automatically. If it keeps failing you may be behind a company "
            "proxy; see TROUBLESHOOTING in the README."
        ),
    )


def check_browser() -> Check:
    browser = find_browser()
    return Check(
        "Google Chrome or Microsoft Edge",
        browser is not None,
        detail=Path(browser).name if browser else "not found",
        fix=(
            "This app drives a real browser to read Amazon. Install Google "
            "Chrome from google.com/chrome. On Windows, Microsoft Edge is "
            "usually already installed and works too."
        ),
    )


def check_writable(out_dir: Path) -> Check:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        ok, detail = True, "can save files"
    except Exception as exc:
        ok, detail = False, f"cannot write: {type(exc).__name__}"
    return Check(
        "Permission to save files",
        ok,
        detail=detail,
        fix=(
            "Move this folder somewhere you own - your Desktop or Documents - "
            "and start it again. Running it from inside a ZIP file, a network "
            "drive, or Program Files will cause this."
        ),
    )


def check_not_in_temp() -> Check:
    """Running straight out of a double-clicked ZIP puts us under Temp.

    A one-file executable legitimately unpacks itself into Temp, so when
    frozen we must judge by where the .exe sits, not where the code ran from.
    """
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).resolve()
    else:
        here = Path(__file__).resolve()
    parts = {p.lower() for p in here.parts}
    suspicious = bool({"temp", "tmp"} & parts) or "appdata\\local\\temp" in str(here).lower()
    return Check(
        "Extracted properly",
        not suspicious,
        detail="looks extracted" if not suspicious else f"running from {here.parent}",
        fix=(
            "It looks like this is running from inside the ZIP file. Right-click "
            "the ZIP, choose 'Extract All', then open the extracted folder and "
            "start it from there."
        ),
        fatal=False,
    )


def run_all(out_dir: Path) -> list[Check]:
    return [
        check_python(),
        check_dependencies(),
        check_browser(),
        check_writable(out_dir),
        check_not_in_temp(),
    ]


def report(checks: list[Check]) -> bool:
    """Print results. Returns False if anything fatal failed."""
    blocked = False
    print("  Checking your computer is ready...\n")
    for c in checks:
        mark = "OK  " if c.ok else ("!!  " if c.fatal else " ?  ")
        print(f"   {mark} {c.name}: {c.detail}")
        if not c.ok:
            if c.fatal:
                blocked = True
            print()
            for line in _wrap(c.fix, 68):
                print(f"        {line}")
            print()
    return not blocked


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines
