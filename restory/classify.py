"""Blast-radius classification of Claude Code PreToolUse payloads.

Given a tool call ``{"tool_name": str, "tool_input": {...}}`` this module
returns a :class:`ClassifyResult` describing which blast-radius tags apply,
whether the call is dangerous, and a one-line human-readable reason.

All path handling uses :class:`pathlib.Path` so behavior is correct on
Windows as well as POSIX platforms.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import urlparse

from .config import find_repo_root

# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #


@dataclass
class ClassifyResult:
    tags: list[str] = field(default_factory=list)
    danger: bool = False
    reason: str = ""


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_SECRET_GLOBS = (".env*", "*.pem", "*id_rsa*", "*.key", "*credentials*", "*.aws/*")

_NET_VERBS = {
    "curl",
    "wget",
    "invoke-webrequest",
    "iwr",
    "nc",
    "ncat",
    "netcat",
    "scp",
}

_ENCODE_CMDS = {"base64", "xxd"}

_DELETE_CMDS = {"rm", "remove-item", "ri", "del", "erase", "rmdir", "rd"}

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}

# Redirect targets that discard output rather than writing a real file.
_NULL_SINKS = {"/dev/null", "nul"}

# Shells that, when fed on stdin, execute arbitrary constructed code.
_SHELL_CMDS = {"sh", "bash", "zsh", "dash", "ash"}

# Interpreters whose ``-c``/``-e`` one-liner body we cannot statically inspect.
_INTERP_CMDS = {"python", "python3", "node", "nodejs", "perl", "ruby"}

# Commands that can execute a script file passed as an argument. Used to detect
# the "download a file, then run it" pattern across command segments.
_SCRIPT_RUNNERS = _SHELL_CMDS | _INTERP_CMDS | {"source", "."}

# Permission/ownership mutators.
_PERM_CMDS = {"chmod", "chown", "chgrp"}

# Shell startup / history files whose contents are sourced on every new shell,
# making them a persistence foothold. Compared by basename (lowercased).
_SHELL_CONFIG_FILES = {
    ".bashrc",
    ".bash_profile",
    ".bash_login",
    ".bash_logout",
    ".profile",
    ".bash_history",
    ".zshrc",
    ".zprofile",
    ".zshenv",
    ".zlogin",
    ".zlogout",
}

# Rooted system locations a symlink should never point into.
_SENSITIVE_PATH_PREFIXES = (
    "/etc/",
    "/root/",
    "/var/",
    "/boot/",
    "/sys/",
    "/proc/",
    "/usr/",
)

# Dot-directories anywhere in a path that hold credentials/keys.
_SENSITIVE_DOTDIRS = {".ssh", ".aws", ".gnupg"}

# Keywords that betray a delete or network verb inside an interpreter one-liner.
_CODE_DELETE_KW = (
    "unlink",
    "rmtree",
    "os.remove",
    "remove(",
    "rmdir",
    "shutil",
    "fs.rm",
    "rimraf",
    "rm -",
    " rm ",
    "del ",
)
_CODE_NET_KW = (
    "socket",
    "urllib",
    "urlopen",
    "requests",
    "http://",
    "https://",
    "fetch(",
    "http.client",
    "httplib",
    "net::http",
    "wget",
    "curl",
    "connect(",
)

# Max recursion depth when unrolling nested command substitutions.
_MAX_SUBST_DEPTH = 4

# Tag → reason priority (most severe first).
_TAG_PRIORITY = (
    "pipe-to-shell",
    "download-execute",
    "uninspectable",
    "persistence",
    "shell-config-tamper",
    "symlink-attack",
    "mass-delete",
    "perm-change",
    "git-hook-write",
    "git-destructive",
    "net-egress",
    "read-secret",
    "write-outside-repo",
)


# --------------------------------------------------------------------------- #
# Bash tokenizing helpers
# --------------------------------------------------------------------------- #


def _split_segments(command: str) -> list[str]:
    """Split a shell command on ``;`` ``&&`` ``||`` ``|`` respecting quotes."""
    segments: list[str] = []
    buf = ""
    in_single = in_double = False
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if c == "'" and not in_double:
            in_single = not in_single
            buf += c
            i += 1
            continue
        if c == '"' and not in_single:
            in_double = not in_double
            buf += c
            i += 1
            continue
        if not in_single and not in_double:
            two = command[i : i + 2]
            if two in ("&&", "||"):
                segments.append(buf)
                buf = ""
                i += 2
                continue
            if c in ";|":
                segments.append(buf)
                buf = ""
                i += 1
                continue
        buf += c
        i += 1
    segments.append(buf)
    return [s.strip() for s in segments if s.strip()]


def _tokenize(segment: str) -> list[str]:
    """Tokenize a single command segment, tolerating malformed quoting."""
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _split_on_pipe(command: str) -> list[str]:
    """Split a command into pipeline stages on single ``|`` (respecting quotes).

    ``||`` (logical OR) is not a pipe and is left intact within a stage.
    """
    parts: list[str] = []
    buf = ""
    in_single = in_double = False
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if c == "'" and not in_double:
            in_single = not in_single
            buf += c
            i += 1
            continue
        if c == '"' and not in_single:
            in_double = not in_double
            buf += c
            i += 1
            continue
        if not in_single and not in_double:
            if command[i : i + 2] == "||":
                buf += "||"
                i += 2
                continue
            if c == "|":
                parts.append(buf)
                buf = ""
                i += 1
                continue
        buf += c
        i += 1
    parts.append(buf)
    return parts


def _extract_substitutions(segment: str) -> tuple[list[str], bool]:
    """Return ``(inner_commands, unparseable)`` for ``$(...)`` and backticks.

    ``unparseable`` is True when a substitution is opened but never balanced
    (an unbalanced ``$(`` or an odd number of backticks) — meaning we cannot
    recover the inner command to inspect it.
    """
    inners: list[str] = []
    unparseable = False

    # $( ... ) with balanced parens; single quotes suppress substitution.
    i = 0
    n = len(segment)
    in_single = False
    while i < n:
        c = segment[i]
        if c == "'":
            in_single = not in_single
            i += 1
            continue
        if not in_single and c == "$" and i + 1 < n and segment[i + 1] == "(":
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                if segment[j] == "(":
                    depth += 1
                elif segment[j] == ")":
                    depth -= 1
                j += 1
            if depth != 0:
                unparseable = True
                break
            inners.append(segment[i + 2 : j - 1])
            i = j
            continue
        i += 1

    # Backticks: pair them up, ignoring those inside single quotes.
    bt_positions: list[int] = []
    in_single = False
    for k, ch in enumerate(segment):
        if ch == "'":
            in_single = not in_single
        elif ch == "`" and not in_single:
            bt_positions.append(k)
    if len(bt_positions) % 2 == 1:
        unparseable = True
    else:
        for a in range(0, len(bt_positions), 2):
            inners.append(segment[bt_positions[a] + 1 : bt_positions[a + 1]])

    return inners, unparseable


def _extract_redirect_targets(segment: str) -> list[str]:
    """Return file targets of ``>``/``>>`` redirections in ``segment``.

    File-descriptor duplications like ``>&2`` are ignored (no file target).
    """
    targets: list[str] = []
    i = 0
    n = len(segment)
    in_single = in_double = False
    while i < n:
        c = segment[i]
        if c == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if c == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        if c == ">" and not in_single and not in_double:
            j = i + 1
            if j < n and segment[j] == ">":  # ">>"
                j += 1
            while j < n and segment[j] in " \t":
                j += 1
            if j < n and segment[j] == "&":  # ">&2" fd dup, not a file
                i = j + 1
                continue
            tok = ""
            t_single = t_double = False
            while j < n:
                ch = segment[j]
                if ch == "'" and not t_double:
                    t_single = not t_single
                    j += 1
                    continue
                if ch == '"' and not t_single:
                    t_double = not t_double
                    j += 1
                    continue
                if not t_single and not t_double and ch in " \t|;&<>":
                    break
                tok += ch
                j += 1
            if tok:
                targets.append(tok)
            i = j
            continue
        i += 1
    return targets


def _basename_cmd(token: str) -> str:
    """Return the lowercased command name, stripping any path prefix."""
    norm = token.replace("\\", "/")
    return norm.rsplit("/", 1)[-1].lower()


def _has_unquoted_tilde_target(segment: str) -> bool:
    """True if the segment contains an unquoted ``~`` used as a path argument.

    This is the CVE class: ``rm -rf ~`` expands to ``$HOME`` while ``rm -rf
    "~"`` targets a literal directory named ``~``.
    """
    in_single = in_double = False
    prev = " "
    i = 0
    n = len(segment)
    while i < n:
        c = segment[i]
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif c == "~" and not in_single and not in_double and prev.isspace():
            nxt = segment[i + 1] if i + 1 < n else " "
            if nxt in ("/", "\\") or nxt.isspace():
                return True
        prev = c
        i += 1
    return False


# --------------------------------------------------------------------------- #
# Path / secret helpers
# --------------------------------------------------------------------------- #


def _resolve_target(raw: str, repo_root: Path) -> Path:
    """Expand ``~`` and ``%VAR%``/``$VAR`` then resolve relative to repo root."""
    expanded = os.path.expandvars(os.path.expanduser(raw))
    p = Path(expanded)
    if not p.is_absolute():
        p = repo_root / p
    try:
        return p.resolve()
    except (OSError, ValueError):
        return p


def _is_inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _matches_secret(token: str) -> bool:
    """True if ``token`` looks like a reference to a secret-bearing file."""
    cand = token.lstrip("@").replace("\\", "/")
    if not cand:
        return False
    base = cand.rsplit("/", 1)[-1]
    for pat in _SECRET_GLOBS:
        if fnmatch(base, pat) or fnmatch(cand, pat):
            return True
    return False


def _extract_host(token: str) -> str | None:
    """Extract a hostname from a URL or scp-style ``user@host:path`` token."""
    if "://" in token:
        host = urlparse(token).hostname
        return host
    # scp / rsync style: user@host:path or host:path
    if ":" in token and "/" not in token.split(":", 1)[0]:
        left = token.split(":", 1)[0]
        host = left.rsplit("@", 1)[-1]
        if host and "." in host:
            return host
    return None


def _is_local(host: str) -> bool:
    h = host.lower()
    if h in _LOCAL_HOSTS:
        return True
    return h.startswith(("127.", "10.", "192.168.", "169.254.")) or h == "::1"


def _is_sensitive_symlink_target(token: str, repo_root: Path) -> bool:
    """True if ``token`` points at a secret file, a system location, or anything
    outside the repo — the destinations a malicious symlink is built to reach.

    The rooted-prefix and dot-directory checks are platform-independent (they
    work off the literal path text), so ``/etc/passwd`` and ``~/.ssh`` are
    flagged even when tested on Windows, where they would otherwise resolve
    *inside* the repo root.
    """
    if _matches_secret(token):
        return True
    expanded = os.path.expandvars(os.path.expanduser(token))
    low = expanded.replace("\\", "/").lower()
    parts = [p for p in low.split("/") if p]
    if any(p in _SENSITIVE_DOTDIRS for p in parts):
        return True
    if low in ("/etc/passwd", "/etc/shadow", "/etc/sudoers", "/root"):
        return True
    if low.startswith(_SENSITIVE_PATH_PREFIXES):
        return True
    resolved = _resolve_target(token, repo_root)
    if not _is_inside(resolved, repo_root):
        return True
    return False


def _is_world_writable_mode(token: str) -> bool:
    """True for a chmod mode that grants write to *others* (e.g. ``777``, ``666``,
    ``o+w``, ``a+w``). Numeric modes whose others-digit lacks the write bit
    (``755``, ``700``, ``644``) and pure ``+x`` are not world-writable."""
    if token.isdigit() and len(token) in (3, 4):
        return bool(int(token[-1]) & 0o2)
    low = token.lower()
    return any(sym in low for sym in ("o+w", "a+w", "+rwx", "go+w")) or low == "+w"


# --------------------------------------------------------------------------- #
# Per-command detectors
# --------------------------------------------------------------------------- #


def _detect_find_delete(cmd: str, tokens: list[str]) -> str | None:
    if cmd != "find":
        return None
    lowered = [t.lower() for t in tokens[1:]]
    if "-delete" in lowered:
        return "mass-delete: find -delete removes every matched file"
    if "-exec" in lowered or "-execdir" in lowered:
        # any rm in the -exec body
        if any(_basename_cmd(t) == "rm" for t in tokens[1:]):
            return "mass-delete: find -exec rm deletes every matched file"
    return None


def _detect_mass_delete(cmd: str, tokens: list[str], segment: str, repo_root: Path):
    r = _detect_find_delete(cmd, tokens)
    if r:
        return r
    if cmd not in _DELETE_CMDS:
        return None
    recursive = False
    dangerous_target = None
    for tok in tokens[1:]:
        low = tok.lower()
        if tok.startswith("-") and not tok.startswith("--"):
            if "r" in low:  # -r, -rf, -fr, -R ...
                recursive = True
        if low in ("-recurse", "--recursive"):
            recursive = True
        if not tok.startswith("-"):
            target = _classify_delete_target(tok, repo_root)
            if target:
                dangerous_target = target
    if _has_unquoted_tilde_target(segment):
        dangerous_target = dangerous_target or "home directory (~)"
    if recursive or dangerous_target:
        target = dangerous_target or "recursively"
        return f"mass-delete: '{cmd}' deletes {target}"
    return None


def _classify_delete_target(token: str, repo_root: Path) -> str | None:
    if token in ("~", "~/", "$HOME", "%USERPROFILE%"):
        return "home directory"
    resolved = _resolve_target(token, repo_root)
    try:
        anchor = Path(resolved.anchor)
    except (OSError, ValueError):
        anchor = None
    if anchor is not None and resolved == anchor:
        return "filesystem root"
    if resolved == repo_root.resolve():
        return "repository root"
    try:
        if resolved == Path.home().resolve():
            return "home directory"
    except (OSError, ValueError):
        pass
    return None


def _detect_git_destructive(cmd: str, tokens: list[str]) -> str | None:
    if cmd != "git":
        return None
    args = [t.lower() for t in tokens[1:]]
    argset = set(args)
    if "reset" in args and "--hard" in argset:
        return "git-destructive: git reset --hard discards working-tree changes"
    if "clean" in args and any(
        t.startswith("-") and "f" in t for t in args
    ):
        return "git-destructive: git clean removes untracked files"
    if "push" in args and ("--force" in argset or "-f" in argset or "--force-with-lease" in argset):
        return "git-destructive: git push --force rewrites remote history"
    if "branch" in args and ("-d" in argset or "--delete" in argset):
        return "git-destructive: git branch deletion"
    if "push" in args and any(a.startswith(":") for a in args):
        return "git-destructive: git push deletes a remote branch"
    return None


def _detect_net_egress(cmd: str, tokens: list[str]) -> str | None:
    if cmd not in _NET_VERBS:
        return None
    for tok in tokens[1:]:
        host = _extract_host(tok)
        if host and not _is_local(host):
            return f"net-egress: {cmd} sends data to external host {host}"
    return None


def _detect_pipe_to_shell(command: str) -> str | None:
    """True if output is piped into a shell, or code is fed to eval/iex.

    Covers ``curl ... | sh``, ``base64 -d | bash``, and PowerShell
    ``Invoke-Expression`` / ``iex``.
    """
    stages = _split_on_pipe(command)
    for idx, stage in enumerate(stages):
        tokens = _tokenize(stage)
        for tok in tokens:
            if tok.lower() in ("iex", "invoke-expression"):
                return "pipe-to-shell: Invoke-Expression runs constructed code"
        if idx > 0 and tokens:
            name = _basename_cmd(tokens[0])
            if name in _SHELL_CMDS:
                return f"pipe-to-shell: output piped into '{name}'"
    return None


def _detect_interpreter_oneliner(cmd: str, tokens: list[str]) -> str | None:
    """Flag ``python -c`` / ``node -e`` / ``perl -e`` one-liners whose inline
    body performs a delete or network operation (statically uninspectable)."""
    if cmd not in _INTERP_CMDS:
        return None
    body = None
    for idx in range(1, len(tokens) - 1):
        if tokens[idx] in ("-c", "-e"):
            body = tokens[idx + 1]
            break
    if body is None:
        return None
    low = body.lower()
    if any(kw in low for kw in _CODE_DELETE_KW):
        return f"uninspectable: {cmd} inline code performs a delete"
    if any(kw in low for kw in _CODE_NET_KW):
        return f"uninspectable: {cmd} inline code performs network I/O"
    return None


def _detect_redirect_writes(segment: str, repo_root: Path):
    """Yield ``(tag, reason)`` for ``>``/``>>`` redirections to sensitive targets."""
    results: list[tuple[str, str]] = []
    for tgt in _extract_redirect_targets(segment):
        norm = tgt.replace("\\", "/")
        # Null sinks (`2>/dev/null`, `>NUL`) discard output; not a real write.
        if norm.lower() in _NULL_SINKS:
            continue
        if ".git/hooks/" in norm or norm.endswith("/.git/hooks"):
            results.append(
                ("git-hook-write", f"git-hook-write: redirect writes to git hook {tgt}")
            )
            continue
        if _matches_secret(tgt):
            results.append(
                ("read-secret", f"read-secret: redirect writes to secret file {tgt}")
            )
        resolved = _resolve_target(tgt, repo_root)
        if not _is_inside(resolved, repo_root):
            results.append(
                (
                    "write-outside-repo",
                    f"write-outside-repo: redirect writes to {resolved} outside repo root {repo_root}",
                )
            )
    return results


def _detect_symlink_attack(cmd: str, tokens: list[str], repo_root: Path) -> str | None:
    """Flag ``ln -s`` whose target is a secret file, a system path, or anything
    outside the repo. A symlink into the repo (``ln -s ./dist ./latest``) is a
    normal build convenience and is left alone."""
    if cmd != "ln":
        return None
    short = [t for t in tokens[1:] if t.startswith("-") and not t.startswith("--")]
    longf = [t.lower() for t in tokens[1:] if t.startswith("--")]
    symbolic = any("s" in f.lower() for f in short) or "--symbolic" in longf
    if not symbolic:
        return None
    for op in (t for t in tokens[1:] if not t.startswith("-")):
        if _is_sensitive_symlink_target(op, repo_root):
            return f"symlink-attack: ln -s points at sensitive location {op}"
    return None


def _detect_perm_change(
    cmd: str, tokens: list[str], segment: str, repo_root: Path
) -> str | None:
    """Flag permission/ownership changes with a broad or sensitive blast radius:
    a world-writable mode, a recursive change over the repo/home, or any change
    to a secret file or a target outside the repo. A scoped, non-world-writable
    change inside the repo (``chmod +x build.sh``) is left alone."""
    if cmd not in _PERM_CMDS:
        return None
    recursive = False
    for t in tokens[1:]:
        low = t.lower()
        if low in ("-r", "--recursive"):
            recursive = True
        elif t.startswith("-") and not t.startswith("--") and "r" in low:
            recursive = True
    operands = [t for t in tokens[1:] if not t.startswith("-")]
    mode = operands[0] if operands else None
    targets = operands[1:]

    if cmd == "chmod" and mode and _is_world_writable_mode(mode):
        return f"perm-change: chmod grants world-write ({mode})"

    if recursive:
        for tgt in targets:
            scope = _classify_delete_target(tgt, repo_root)
            if scope:
                return f"perm-change: {cmd} -R over the {scope}"
        if _has_unquoted_tilde_target(segment):
            return f"perm-change: {cmd} -R over the home directory (~)"

    for tgt in targets:
        if _matches_secret(tgt):
            return f"perm-change: {cmd} on secret file {tgt}"
        resolved = _resolve_target(tgt, repo_root)
        if not _is_inside(resolved, repo_root):
            return f"perm-change: {cmd} on {tgt} outside the repo"
    if _has_unquoted_tilde_target(segment):
        return f"perm-change: {cmd} targets the home directory (~)"
    return None


def _download_output_names(tokens: list[str]) -> list[str]:
    """Basenames of files a ``curl``/``wget`` invocation writes to disk.

    ``-O-`` / ``-o -`` (write to stdout) yield no file and are ignored — that is
    the pipe-to-shell case, handled elsewhere.
    """
    names: list[str] = []
    i = 1
    n = len(tokens)
    while i < n:
        t = tokens[i]
        if t in ("-o", "-O", "--output", "--output-document"):
            if i + 1 < n:
                val = tokens[i + 1]
                if val != "-" and not val.startswith("-"):
                    names.append(_basename_cmd(val))
                i += 2
                continue
        elif (t.startswith("-o") or t.startswith("-O")) and len(t) > 2:
            val = t[2:]
            if val != "-" and not val.startswith("-"):
                names.append(_basename_cmd(val))
        i += 1
    return names


def _detect_download_execute(command: str, repo_root: Path) -> str | None:
    """Flag "download a file, then run it" chains that pipe-to-shell misses:
    ``curl -o s.sh URL && ./s.sh`` or ``wget -O s URL; bash s``. The download
    and the execution live in separate segments, so this correlates across the
    whole command."""
    segments = _split_segments(command)
    downloaded: set[str] = set()
    for seg in segments:
        toks = _tokenize(seg)
        if not toks or _basename_cmd(toks[0]) not in ("curl", "wget"):
            continue
        downloaded.update(_download_output_names(toks))
        for tgt in _extract_redirect_targets(seg):
            if tgt.replace("\\", "/").lower() not in _NULL_SINKS:
                downloaded.add(_basename_cmd(tgt))
    downloaded.discard("")
    if not downloaded:
        return None
    for seg in segments:
        toks = _tokenize(seg)
        if not toks:
            continue
        first = _basename_cmd(toks[0])
        if first in downloaded:
            return f"download-execute: downloaded file is executed ({toks[0]})"
        if first in _SCRIPT_RUNNERS:
            for a in toks[1:]:
                if not a.startswith("-") and _basename_cmd(a) in downloaded:
                    return f"download-execute: downloaded file run via {first} ({a})"
    return None


def _detect_shell_config_tamper(
    cmd: str, tokens: list[str], segment: str, repo_root: Path
) -> str | None:
    """Flag writes/appends into a shell startup or history file *outside* the
    repo (the user's real ``~/.bashrc`` etc.), where they would persist across
    sessions. A same-named file tracked inside the repo (a dotfiles project) is
    left alone."""

    def _is_home_config(raw: str) -> bool:
        if _basename_cmd(raw) not in _SHELL_CONFIG_FILES:
            return False
        return not _is_inside(_resolve_target(raw, repo_root), repo_root)

    for tgt in _extract_redirect_targets(segment):
        if _is_home_config(tgt):
            return f"shell-config-tamper: write to shell startup file {tgt}"
    if cmd == "tee":
        for t in tokens[1:]:
            if not t.startswith("-") and _is_home_config(t):
                return f"shell-config-tamper: tee writes to shell startup file {t}"
    return None


def _detect_persistence(cmd: str, tokens: list[str]) -> str | None:
    """Flag installation of cron jobs / scheduled tasks. Read-only listings
    (``crontab -l``, ``schtasks /query``) are not persistence and are ignored."""
    low_args = [t.lower() for t in tokens[1:]]
    if cmd == "crontab":
        if "-l" in low_args:
            return None
        return "persistence: crontab installs scheduled jobs"
    if cmd == "at":
        if "-l" in low_args:
            return None
        return "persistence: 'at' schedules a deferred job"
    if cmd == "schtasks":
        if any(a in ("/create", "/change") for a in low_args):
            return "persistence: schtasks creates a scheduled task"
        return None
    for t in tokens:
        if t.lower() in (
            "new-scheduledtask",
            "register-scheduledtask",
            "new-scheduledtasktrigger",
        ):
            return f"persistence: PowerShell {t} registers a scheduled task"
    return None


# --------------------------------------------------------------------------- #
# Tool handlers
# --------------------------------------------------------------------------- #


def _classify_bash(command: str, repo_root: Path):
    tags: list[str] = []
    reasons: dict[str, str] = {}

    def add(tag: str, reason: str) -> None:
        if tag not in tags:
            tags.append(tag)
            reasons[tag] = reason

    _process_command(command, repo_root, add, depth=0)
    return tags, reasons


def _process_command(command: str, repo_root: Path, add, depth: int) -> None:
    """Classify one command string (recursing into command substitutions)."""
    r = _detect_pipe_to_shell(command)
    if r:
        add("pipe-to-shell", r)

    r = _detect_download_execute(command, repo_root)
    if r:
        add("download-execute", r)

    for segment in _split_segments(command):
        tokens = _tokenize(segment)
        if not tokens:
            continue
        cmd = _basename_cmd(tokens[0])

        # Command substitutions: recursively classify the inner command; if it
        # cannot be parsed (unbalanced), it is uninspectable.
        if depth < _MAX_SUBST_DEPTH:
            inners, unparseable = _extract_substitutions(segment)
            if unparseable:
                add(
                    "uninspectable",
                    "uninspectable: unbalanced command substitution could not be parsed",
                )
            for inner in inners:
                if inner.strip():
                    _process_command(inner, repo_root, add, depth + 1)

        r = _detect_mass_delete(cmd, tokens, segment, repo_root)
        if r:
            add("mass-delete", r)

        r = _detect_git_destructive(cmd, tokens)
        if r:
            add("git-destructive", r)

        r = _detect_net_egress(cmd, tokens)
        if r:
            add("net-egress", r)

        r = _detect_interpreter_oneliner(cmd, tokens)
        if r:
            add("uninspectable", r)

        r = _detect_symlink_attack(cmd, tokens, repo_root)
        if r:
            add("symlink-attack", r)

        r = _detect_perm_change(cmd, tokens, segment, repo_root)
        if r:
            add("perm-change", r)

        r = _detect_shell_config_tamper(cmd, tokens, segment, repo_root)
        if r:
            add("shell-config-tamper", r)

        r = _detect_persistence(cmd, tokens)
        if r:
            add("persistence", r)

        for tag, reason in _detect_redirect_writes(segment, repo_root):
            add(tag, reason)

        for tok in tokens:
            if _matches_secret(tok):
                add("read-secret", f"read-secret: references secret file {tok.lstrip('@')}")
                break


def _classify_fileop(tool_name: str, tool_input: dict, repo_root: Path):
    tags: list[str] = []
    reasons: dict[str, str] = {}
    raw = tool_input.get("file_path") or tool_input.get("path")
    if not raw:
        return tags, reasons

    if _matches_secret(str(raw)):
        tags.append("read-secret")
        reasons["read-secret"] = f"read-secret: {tool_name} targets secret file {raw}"

    resolved = _resolve_target(str(raw), repo_root)
    if _basename_cmd(str(raw)) in _SHELL_CONFIG_FILES and not _is_inside(resolved, repo_root):
        tags.append("shell-config-tamper")
        reasons["shell-config-tamper"] = (
            f"shell-config-tamper: {tool_name} writes to shell startup file {raw}"
        )
    if not _is_inside(resolved, repo_root):
        tags.append("write-outside-repo")
        reasons["write-outside-repo"] = (
            f"write-outside-repo: {tool_name} writes to {resolved} outside repo root {repo_root}"
        )
    return tags, reasons


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def classify(tool_call: dict, repo_root: Path | None = None) -> ClassifyResult:
    """Classify a Claude Code PreToolUse payload.

    ``repo_root`` may be supplied for testing; it defaults to the resolved
    repository root of the current working directory.
    """
    root = Path(repo_root).resolve() if repo_root is not None else find_repo_root()
    tool_name = tool_call.get("tool_name", "")
    tool_input = tool_call.get("tool_input") or {}

    if tool_name == "Bash":
        tags, reasons = _classify_bash(str(tool_input.get("command", "")), root)
    elif tool_name in ("Write", "Edit", "MultiEdit"):
        tags, reasons = _classify_fileop(tool_name, tool_input, root)
    else:
        tags, reasons = [], {}

    danger = bool(tags)
    reason = "no blast-radius indicators"
    for tag in _TAG_PRIORITY:
        if tag in reasons:
            reason = reasons[tag]
            break
    return ClassifyResult(tags=tags, danger=danger, reason=reason)
