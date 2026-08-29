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


def get_allowlist_path() -> Path:
    """Return the path to the user's command allowlist.

    SECURITY: this deliberately lives in the USER's own space
    (``~/.restory/allowlist.json``), never inside the project repository. The
    allowlist is a carve-out that tells restory to *stop blocking* a specific
    command, so a repo-committed allowlist would be a self-approving backdoor —
    a poisoned repo could ship an ``allowlist.json`` that greenlights its own
    ``curl … | sh`` payload, which is exactly the threat restory exists to catch.
    Anchoring to ``get_data_dir()`` (USERPROFILE/HOME) means only the user, on
    their own machine, can ever add an entry. Nothing here reads from the repo.
    """
    return get_data_dir() / "allowlist.json"


# Resolved at import time for convenient access.
REPO_ROOT = find_repo_root()
DATA_DIR = get_data_dir()
