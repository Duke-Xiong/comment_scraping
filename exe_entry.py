"""Entry point for the standalone executable.

Kept separate from webapp/server.py so PyInstaller has one obvious script to
analyse, and so the frozen build can show a friendly banner before the slower
first-run unpack finishes.
"""

from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    # Without this a frozen app can re-launch itself when anything spawns a
    # process, producing an endless stack of windows.
    multiprocessing.freeze_support()

    print()
    print("  ===============================================")
    print("    Amazon Review Downloader")
    print("  ===============================================")
    print()
    print("  Starting up (the first launch is the slow one)...")
    print()

    try:
        from webapp.server import main as serve
    except Exception as exc:
        print(f"  Could not start: {type(exc).__name__}: {exc}")
        print()
        print("  If this keeps happening, please report it with the text above.")
        input("  Press Enter to close. ")
        return 1

    try:
        return serve()
    except KeyboardInterrupt:
        print("\n  Stopped. Your files are in the 'output' folder.\n")
        return 0
    except Exception as exc:
        print(f"\n  Unexpected problem: {type(exc).__name__}: {exc}\n")
        try:
            input("  Press Enter to close. ")
        except EOFError:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
