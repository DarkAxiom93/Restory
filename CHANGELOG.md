# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
