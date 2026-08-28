"""Configuration and path resolution for restory.

All paths use :class:`pathlib.Path` so behavior is correct on Windows,
macOS, and Linux alike.
"""

from __future__ import annotations

import os
from pathlib import Path


def find_repo_root(start: Path | None = None) -> Path:
    """Resolve the repository root.

    Walk up from ``start`` (default: current working directory) looking for a
    ``.git`` directory. If none is found, fall back to the starting directory.
    """
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").is_dir():
            return candidate
    return current


def get_data_dir() -> Path:
    """Return the restory data directory, creating it if necessary.

    Located at ``%USERPROFILE%/.restory`` on Windows, falling back to the
    user's home directory on other platforms.
    """
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    data_dir = home / ".restory"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


# Resolved at import time for convenient access.
REPO_ROOT = find_repo_root()
DATA_DIR = get_data_dir()
