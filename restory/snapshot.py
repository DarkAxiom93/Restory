"""Shadow-git snapshotting for restory.

We never touch the user's real ``.git``. Instead we keep a *shadow* git
repository whose ``GIT_DIR`` lives under the restory data directory and whose
work tree is the user's repo root. Every git invocation is an explicit
``subprocess`` call with its own ``cwd`` and ``env`` so the shadow database
and the (possibly present) real ``.git`` never interfere. Designed to work
with Git for Windows.

Layout::

    <data_dir>/<repo-hash>/shadow/   <- GIT_DIR (objects, refs, HEAD, config)

The shadow lives outside the work tree, so git cannot try to version its own
database. We additionally always ignore ``node_modules`` and ``.restory`` via
the shadow's ``info/exclude`` (the repo's own ``.gitignore`` is respected
automatically).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import find_repo_root, get_data_dir

_ALWAYS_IGNORE = ("node_modules/", ".restory/")


class SnapshotError(RuntimeError):
    """Raised when a shadow git operation fails."""


def _git_exe() -> str:
    exe = shutil.which("git")
    if not exe:
        raise SnapshotError("git executable not found on PATH (need Git for Windows)")
    return exe


@dataclass
class ChangeEntry:
    status: str  # A / M / D / R...
    path: str


class Shadow:
    """A shadow git repository shadowing one work tree."""

    def __init__(self, repo_root: Path, git_dir: Path) -> None:
        self.repo_root = repo_root
        self.git_dir = git_dir

    # -- environment / process helpers ------------------------------------ #

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["GIT_DIR"] = str(self.git_dir)
        env["GIT_WORK_TREE"] = str(self.repo_root)
        # Deterministic, non-interactive, don't inherit the user's system config.
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_AUTHOR_NAME"] = "restory"
        env["GIT_AUTHOR_EMAIL"] = "restory@localhost"
        env["GIT_COMMITTER_NAME"] = "restory"
        env["GIT_COMMITTER_EMAIL"] = "restory@localhost"
        return env

    def _git(
        self,
        args: list[str],
        *,
        check: bool = True,
        index: str | None = None,
    ) -> subprocess.CompletedProcess:
        env = self._env()
        if index is not None:
            # Operate against a throwaway index so the shadow's real index is
            # left untouched (used by read-only diffing).
            env["GIT_INDEX_FILE"] = index
        proc = subprocess.run(
            [_git_exe(), *args],
            cwd=str(self.repo_root),
            env=env,
            capture_output=True,
            text=True,
        )
        if check and proc.returncode != 0:
            raise SnapshotError(
                f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc

    # -- lifecycle -------------------------------------------------------- #

    def exists(self) -> bool:
        return (self.git_dir / "HEAD").exists()

    def initialize(self) -> None:
        """Create the shadow repo and take the initial snapshot."""
        self.git_dir.mkdir(parents=True, exist_ok=True)
        self._git(["init", "-q"])
        # Persist configuration so the shadow works even without the env vars.
        self._git(["config", "core.worktree", str(self.repo_root)])
        self._git(["config", "core.autocrlf", "false"])  # keep bytes identical
        self._git(["config", "core.safecrlf", "false"])
        self._git(["config", "core.fileMode", "false"])
        self._git(["config", "core.longpaths", "true"])  # Windows long paths
        self._git(["config", "commit.gpgsign", "false"])
        self._git(["config", "user.name", "restory"])
        self._git(["config", "user.email", "restory@localhost"])
        self._write_exclude()
        self._git(["add", "-A"])
        self._git(["commit", "--allow-empty", "-q", "-m", "restory: initial snapshot"])

    def _write_exclude(self) -> None:
        info_dir = self.git_dir / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        (info_dir / "exclude").write_text(
            "\n".join(("# restory always-ignore", *_ALWAYS_IGNORE, "")),
            encoding="utf-8",
        )

    # -- snapshot / undo -------------------------------------------------- #

    def _has_changes(self) -> bool:
        self._git(["add", "-A"])
        proc = self._git(["status", "--porcelain"])
        return bool(proc.stdout.strip())

    def snapshot(self, event_id: int | str) -> str | None:
        """Stage and commit the work tree. Returns the commit hash or None.

        Returns None when there is nothing to commit.
        """
        self._git(["add", "-A"])
        proc = self._git(["status", "--porcelain"])
        if not proc.stdout.strip():
            return None
        self._git(["commit", "-q", "-m", f"restory: event {event_id}"])
        return self._git(["rev-parse", "HEAD"]).stdout.strip()

    def session_baseline(self) -> str:
        """Commit the current tree as a session baseline and return its hash.

        Stages everything; commits only if there is something new (so a
        session that starts on an already-clean tree still yields a valid
        anchor at the current HEAD). The returned commit is the anchor to
        reset back to for a whole-session undo.
        """
        self._git(["add", "-A"])
        proc = self._git(["status", "--porcelain"])
        if proc.stdout.strip():
            self._git(["commit", "-q", "-m", "restory: session baseline"])
        return self._git(["rev-parse", "HEAD"]).stdout.strip()

    def _changes_between(self, old: str, new: str) -> list[ChangeEntry]:
        proc = self._git(["diff", "--name-status", old, new])
        entries: list[ChangeEntry] = []
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            status = parts[0]
            path = parts[-1]
            entries.append(ChangeEntry(status=status, path=path))
        return entries

    def undo(self) -> list[ChangeEntry]:
        """Revert the most recent shadow commit into the work tree.

        Restores modified/deleted files and removes files that the reverted
        commit had added, leaving the work tree byte-identical to the previous
        snapshot. Returns the list of reverted changes.
        """
        head = self._git(["rev-parse", "HEAD"]).stdout.strip()
        parent = self._git(["rev-parse", "--verify", "-q", "HEAD~1"], check=False)
        if parent.returncode != 0:
            raise SnapshotError("nothing to undo: only the initial snapshot exists")
        prev = parent.stdout.strip()

        # Summary is computed prev..head (what the last snapshot changed).
        changes = self._changes_between(prev, head)

        # Restore the work tree (and index and HEAD) exactly to the previous
        # snapshot. reset --hard removes files the reverted commit added and
        # restores ones it modified or deleted.
        self._git(["reset", "--hard", "-q", prev])
        return changes

    def undo_to(self, commit: str) -> list[ChangeEntry]:
        """Reset the work tree to ``commit`` in one shot (whole-session undo).

        Restores every file to the state at ``commit``: files added since then
        are removed, modified ones reverted, deleted ones restored, leaving the
        work tree byte-identical to that anchor. Returns the reverted changes.
        """
        head = self._git(["rev-parse", "HEAD"]).stdout.strip()
        verify = self._git(
            ["rev-parse", "--verify", "-q", f"{commit}^{{commit}}"], check=False
        )
        if verify.returncode != 0:
            raise SnapshotError(f"unknown anchor commit: {commit}")
        target = verify.stdout.strip()

        # Summary is computed target..head (everything since the anchor).
        changes = self._changes_between(target, head)

        self._git(["reset", "--hard", "-q", target])
        return changes

    # -- read-only diffing ------------------------------------------------ #

    def diff_against(self, anchor: str, *, fmt: str = "full") -> str:
        """Return the diff between ``anchor`` and the current work tree.

        Read-only: seeds a throwaway index from ``anchor``'s tree, stages the
        work tree into it (so added/deleted/untracked files are all captured,
        honouring the shadow's ignore rules), and diffs the anchor against that
        temporary index. Neither the work tree, the shadow's real index, nor
        any commit is modified.

        ``fmt`` selects the git diff format: ``"full"`` (patch), ``"stat"``,
        ``"name-only"``, or ``"name-status"``.
        """
        verify = self._git(
            ["rev-parse", "--verify", "-q", f"{anchor}^{{commit}}"], check=False
        )
        if verify.returncode != 0:
            raise SnapshotError(f"unknown anchor commit: {anchor}")
        target = verify.stdout.strip()

        fmt_args = {
            "full": [],
            "stat": ["--stat"],
            "name-only": ["--name-only"],
            "name-status": ["--name-status"],
        }.get(fmt)
        if fmt_args is None:
            raise ValueError(f"unknown diff format: {fmt!r}")

        import tempfile

        fd, tmp = tempfile.mkstemp(
            prefix="restory-diff-", suffix=".index", dir=str(self.git_dir)
        )
        os.close(fd)
        try:
            # Populate the temp index from the anchor tree, then reconcile it
            # with the current work tree.
            self._git(["read-tree", target], index=tmp)
            self._git(["add", "-A"], index=tmp)
            proc = self._git(
                ["diff", "--cached", *fmt_args, target], index=tmp
            )
            return proc.stdout
        finally:
            try:
                Path(tmp).unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Module-level convenience API
# --------------------------------------------------------------------------- #


def _repo_hash(repo_root: Path) -> str:
    return hashlib.sha1(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:12]


def get_shadow(repo_root: Path | None = None) -> Shadow:
    """Return the :class:`Shadow` for ``repo_root`` (no side effects)."""
    root = Path(repo_root).resolve() if repo_root is not None else find_repo_root()
    git_dir = get_data_dir() / _repo_hash(root) / "shadow"
    return Shadow(root, git_dir)


def ensure_shadow(repo_root: Path | None = None) -> tuple[Shadow, bool]:
    """Return the shadow, creating and initializing it if absent.

    Returns ``(shadow, created)`` where ``created`` is True if it was just
    initialized.
    """
    shadow = get_shadow(repo_root)
    if shadow.exists():
        return shadow, False
    shadow.initialize()
    return shadow, True


def describe_changes(changes: list[ChangeEntry]) -> list[str]:
    """Human-readable one-liners describing an undo's reverted changes."""
    verb = {"A": "removed (was added)", "D": "restored (was deleted)", "M": "reverted (was modified)"}
    lines = []
    for c in changes:
        lines.append(f"  {c.path}: {verb.get(c.status[0], f'reverted ({c.status})')}")
    return lines
