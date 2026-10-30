# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-10-30

A feature release folding in six parallel branches. All 1.0.x security
behavior is preserved: the token + Origin/Host middleware on every `/api/*`
route, per-repo session scoping, shadow-git config isolation, Markdown
backtick escaping, and the shared `session_repo_mismatch` guard.

### Added

- **Severity levels (WARN / BLOCK / CRITICAL).** Every intercepted command now
  carries a severity, surfaced in the block reason shown to the agent, a new
  column in the TUI and browser timelines, and the report/export. `WARN` is an
  opt-in, per-tag "approve but still record" level; default blocks are
  unchanged — a blocking tag always blocks.
- **Expanded classifier.** New danger classes: symlink attacks, permission
  changes (`chmod`/`chown`), download-and-execute, shell-config tampering
  (`~/.bashrc` and friends), and persistence (`crontab`/`schtasks`/`at`).
- **Experimental Cursor support.** A native Cursor adapter (`preToolUse`,
  `permission: deny`) alongside the existing Claude Code and Gemini adapters.
- **User allowlist.** An exact-command, user-directory-only carve-out that
  approves a specific command while still recording it with an audited
  `allowlisted-override` tag. The classifier itself is never weakened — only
  the decision is overridden, and only on an exact match read from the user's
  own directory (never the repo).
- **Browser timeline: filtering, search, and expandable event details**, plus a
  collapsible **session statistics panel** — totals, blocked rate, a breakdown
  by danger class and severity, and an activity sparkline.
- **TUI monitor upgrades.** Filtering, sorting, search, and expandable event
  details in `restory monitor`.
- **Optional desktop notifications on a BLOCK**, opt-in via `RESTORY_NOTIFY=1`
  (real WinRT toasts via `windows-toasts` on Windows, `plyer` elsewhere). Absent
  the backend, notifications no-op; approvals are always silent.
- **`restory init --all`** detects every supported agent installed on the system
  and installs hooks for all of them in a single run.
- **CI**: coverage measurement with a self-contained badge, a packaging job that
  verifies the exported UI is bundled in the wheel, and a pre-commit config.

### Security

- **Closed two known classifier bypass gaps**: base64-decoded blobs executed
  behind `eval`, and nested shell strings (`bash -c "…"`). The self-check now
  reports 49/49 with **zero** known gaps (previously 33/33 with 2 gaps caught
  only by the undo safety net).

## [1.0.6] - 2026-09-03

### Security

- **Locked down the local `restory open` server.** Previously any website you
  visited while the server was running could quietly POST to
  `127.0.0.1:<port>/api/undo` and destroy your uncommitted session work (a
  simple-content-type request needs no CORS preflight), and could read the
  timeline — which exposes absolute file paths and executed commands. The server
  now:
  - binds strictly to loopback (`127.0.0.1`, never `0.0.0.0`);
  - mints a per-session token (`secrets.token_urlsafe(32)`) at startup, kept in
    memory and handed to the browser out-of-band in the URL fragment (never sent
    to the server and never written to a log); the UI keeps it in same-origin
    `sessionStorage` (whose tab-scoped lifetime matches the per-session token and
    leaves nothing on disk) so a plain reload keeps authenticating, while a
    request with no token still fails closed with a `403`;
  - requires that token in a custom `X-Restory-Token` header on **every** API
    request — the read-only timeline included — compared with
    `hmac.compare_digest`. A custom header also forces a CORS preflight, which is
    itself a defense;
  - validates the `Origin` header (a foreign origin is refused) and the `Host`
    header (anything other than loopback is refused, which blocks DNS
    rebinding);
  - rejects any failure with a plain `403` that never echoes the token.
- **Scoped every session and event to its repository.** Records now store the
  repository root (absolute, normalized, case-folded on Windows), and every
  command — `undo`, `report`, `diff`, `status`, `open`, `export`, `monitor` —
  operates only on the current repo's sessions. This removes a data-loss hazard
  where, with more than one repo sharing the store, `undo --session` could reset
  a work tree to a baseline anchor recorded for a *different* repository;
  `undo --session` now hard-fails with a clear message rather than ever falling
  back to the most recent session from elsewhere. Older databases are migrated
  automatically (the new column is added and legacy rows are left unscoped) so an
  existing install keeps working.
- **Isolated the shadow git repo from your global/system git config.** All
  shadow-git subprocesses now run with `GIT_CONFIG_GLOBAL` and
  `GIT_CONFIG_SYSTEM` pointed at the null device and with `core.hooksPath` and
  `core.attributesFile` overridden per invocation, so external filters,
  attributes, or hooks can neither alter nor execute during a snapshot.

## [1.0.7] - 2026-09-05

### Security

- **Stopped attacker-influenced command text from injecting live Markdown into
  exported reports.** A `restory export` Markdown table renders each recorded
  command inside a `` `…` `` code span, but command text is attacker-influenced
  and cell-escaping did not neutralize backticks. A crafted command containing a
  backtick could close the code span early and make the rest render as live
  Markdown — an image or link outside the span — letting a dangerous, blocked
  command read as clean in a report meant for a GitHub issue, PR, or social post.
  Backticks in table cells are now replaced with a Markdown-inert look-alike, so
  the whole value stays inside the code span as inert text.
- **Applied the cross-repository undo guard in the terminal dashboard too.** The
  `restory undo --session` command already refused to reset the work tree when a
  session's recorded repository did not match the current one; the `restory
  monitor` dashboard's undo action relied only on per-repo scoping and lacked the
  same explicit check. Both entry points now share one guard and hard-fail
  identically, so neither can ever reset a work tree to an anchor recorded for a
  different repository.

### Changed

- Clarified in the README ("What restory does NOT do") that the Read tool is not
  covered by restory's interception.

### CI

- Pinned the httpx test dependency and capped the starlette version range to
  prevent transitive dependency drift, and made the UI test stub structurally
  faithful (a `<head>` bootstrap plus a `/_next/static` bundle script) so the
  server-shell tests pass on every runner.

## [1.0.5] - 2026-08-31

### Changed

- Sharpened the README and docs so every claim matches what the code actually
  does: `undo` is described as restoring the **Git-visible working-tree
  changes** captured during a session to the session baseline (no "whole
  tree" / "exact" / "ground-truth" overreach); "records" refers specifically to
  the intercepted Bash/Write/Edit tool calls; and the CVE-2026-22708 reference
  is framed as an *example* of why command allowlists are brittle, not something
  restory claims to remediate.
- Enriched PyPI metadata: fuller description, keywords, trove classifiers, and
  project URLs.
- Corrected the contributor setup order in `CONTRIBUTING.md` — build the UI
  before `pip install -e .`, which is the order that works on a clean clone.

### Added

- `SECURITY.md` with a private vulnerability-disclosure route (GitHub private
  advisories or a dedicated security email).

## [1.0.4] - 2026-08-29

First stable release.

### Added

- **Effect-based classifier** that inspects a command's blast radius — secret
  reads, network egress, mass-deletes, git-history nukes, writes outside the
  repo — *before* it runs, and blocks dangerous effects with a reason.
- **Local timeline** of the intercepted tool calls, backed by a SQLite store,
  viewable in the browser (`restory open`) or as a full-screen terminal
  dashboard (`restory monitor`).
- **Session rollback** (`restory undo --session`) that restores the Git-visible
  working-tree changes to the session baseline via a shadow git repo that never
  touches your real `.git`.
- Adapter layer with support for **Claude Code** (hooks) and **experimental
  Gemini CLI** support.
- **Local-only by design** — no accounts, no cloud, no telemetry.
