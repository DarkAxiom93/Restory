# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.4] - 2026-08-29

First public release.

### Added

- **Effect-based blast-radius classifier** — inspects the *effect* of each
  command before it runs and blocks dangerous classes (secret reads, network
  egress, mass deletes, git-history nukes, writes outside the repo), covering
  command-substitution, pipe-to-shell, `find -delete`, redirect writes, and the
  unquoted-`~` expansion class.
- **Session recording** — every tool call the agent makes is logged to a local
  SQLite store with its blast-radius tags.
- **One-command undo** — `restory undo --session` snaps the working tree back to
  the session's starting state via an exact shadow git repo.
- **Live browser timeline** — `restory open` serves a blast-radius timeline UI
  (exported Next.js app) at `http://127.0.0.1:8765`.
- **Terminal dashboard (TUI)** — `restory monitor` gives a full-screen live
  dashboard, the terminal counterpart to `restory open`.
- **CLI commands** — `init`, `open`, `monitor`, `report`, `status`, `diff`,
  `export`, and `undo`.
- **Agent support** — Claude Code hooks by default (`.claude/settings.json`),
  with experimental Gemini CLI support (`--agent gemini`,
  `.gemini/settings.json`) via a normalizing adapter layer.
- **Local-only** — no accounts, no cloud, no telemetry; all state lives in a
  local SQLite store and shadow git repo.

[Unreleased]: https://github.com/DarkAxiom93/Restory/compare/v1.0.4...HEAD
[1.0.4]: https://github.com/DarkAxiom93/Restory/releases/tag/v1.0.4
