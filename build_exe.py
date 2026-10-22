"""Build a standalone Windows executable.

    python build_exe.py

Produces dist/AmazonReviewDownloader.exe - a single file with Python and every
dependency inside it, so users need nothing installed. It still drives the
Chrome or Edge already on their machine rather than shipping a browser.

Why one file: the ZIP-and-extract dance is the single most common way a
non-technical user gets stuck, and "run it from inside the ZIP" fails in a way
that looks like the app is broken. One file that can be double-clicked from the
Downloads folder removes that whole class of problem.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "AmazonReviewDownloader"


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is missing. Install it with:\n    pip install pyinstaller")
        return 1

    for stale in (ROOT / "build", ROOT / "dist", ROOT / f"{NAME}.spec"):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
        elif stale.exists():
            stale.unlink()

    sep = ";" if sys.platform == "win32" else ":"

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",
        "--name", NAME,
        # Keep the console: it carries the preflight results, the "keep this
        # window open" warning, and any error text. A windowed build would
        # hide every failure from the people least able to diagnose one.
        "--console",

        # The UI files. Paths must match resource_dir() in webapp/server.py.
        "--add-data", f"{ROOT / 'webapp' / 'templates'}{sep}webapp/templates",
        "--add-data", f"{ROOT / 'webapp' / 'static'}{sep}webapp/static",

        # Playwright ships a node driver that PyInstaller cannot infer.
        "--collect-all", "playwright",

        # Imported dynamically or via Flask internals.
        "--hidden-import", "amazon_reviews.preflight",
        "--hidden-import", "openpyxl",
        "--hidden-import", "lxml._elementpath",
        "--collect-submodules", "amazon_reviews",
        "--collect-submodules", "webapp",

        # Nothing here needs a GUI toolkit or plotting stack; excluding them
        # keeps the download meaningfully smaller.
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "numpy",
        "--exclude-module", "pandas",
        "--exclude-module", "PIL",
        "--exclude-module", "pytest",

        str(ROOT / "exe_entry.py"),
    ]

    print("Building - this takes a few minutes.\n")
    result = subprocess.run(args, cwd=ROOT)
    if result.returncode != 0:
        print("\nBuild failed.")
        return result.returncode

    exe = ROOT / "dist" / (f"{NAME}.exe" if sys.platform == "win32" else NAME)
    if not exe.exists():
        print("\nBuild reported success but no executable was produced.")
        return 1

    print(f"\nBuilt: {exe}")
    print(f"Size:  {exe.stat().st_size / 1_000_000:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
