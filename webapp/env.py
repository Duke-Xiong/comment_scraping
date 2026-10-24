"""Load a small .env file without adding a dependency.

The upload configuration belongs in ``.env`` rather than in shell commands.
Values already present in the real environment win, so deployments can still
override the file.

The file is looked up here:
- source checkout: <project root>/.env
- frozen executable: <folder containing the exe>/.env
- REVIEW_ENV_FILE can point at a different file explicitly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def env_path() -> Path:
    override = os.environ.get("REVIEW_ENV_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / ".env"
    return Path(__file__).resolve().parent.parent / ".env"


def _parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].strip()
    if "=" not in line:
        return None

    key, value = line.split("=", 1)
    key = key.strip()
    if not key:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return key, value


def load_env(path: Path | None = None) -> bool:
    """Read KEY=VALUE lines into os.environ without overriding existing keys."""
    target = Path(path) if path is not None else env_path()
    if not target.is_file():
        return False

    try:
        text = target.read_text(encoding="utf-8-sig")
    except OSError:
        return False

    for line in text.splitlines():
        parsed = _parse_line(line)
        if parsed is None:
            continue
        key, value = parsed
        os.environ.setdefault(key, value)
    return True
