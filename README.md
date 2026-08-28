# 🐕‍🦺 restory

**restory records everything your AI coding agent touches, blocks the dangerous stuff, and restores your tree with one command.** Local-only.

![restory in action](docs/demo.gif)

```bash
pip install restory && restory init
```

---

## Why this exists

AI coding agents (Claude Code, Cursor, Copilot) run as *you* — with your files, your keys, your shell. A single hallucination or a poisoned file in a repo can `rm -rf` your home dir or POST your `.env` to a pastebin. Allowlists don't catch it ([CVE-2026-22708](https://www.docker.com/blog/coding-agent-horror-stories-the-command-you-already-approved/)), and a container is too heavy for the inner loop.

**restory** sits between the agent and your machine: it inspects the *effect* of every command before it runs, records the whole session, and restores it with one keystroke.

## What it does

- 🛑 **Blocks dangerous effects** — secret reads, network exfiltration, mass deletes, git-history nukes, writes outside the repo — *before* they execute.
- 📼 **Records everything** — a live, local timeline of every file your agent touched and every command it ran.
- ↩️ **One-command restore** — `restory undo --session` snaps your whole working tree back to where the session started.
- 🔒 **Local-only** — no accounts, no cloud, nothing leaves your machine.

## Install

```bash
pip install restory
restory init        # installs hooks into .claude/settings.json
restory open        # opens the live timeline at http://127.0.0.1:8765
```

Works on Windows, macOS, and Linux. Hook-based — no kernel driver, no container.

## What restory does NOT do

Being honest, because security tools that overpromise get torn apart:

- It's **not a sandbox.** A determined attacker with code execution can bypass any command inspection — "argument inspection is theater." restory is defense-in-depth + visibility + instant restore, not a kernel jail.
- It **won't catch every obfuscation.** It nails the common, high-value danger classes (exfil, mass-delete, secret reads, pipe-to-shell). Novel encodings can slip the classifier — that's what the restore net is for.
- It **doesn't replace your judgment.** It surfaces what the agent tried and lets you reverse it. You're still the human in the loop.

## How it works

`restory init` installs `PreToolUse` / `PostToolUse` / `SessionStart` hooks. Every tool call the agent makes is classified for blast radius; dangerous effects are blocked with a reason, everything is logged to a local SQLite store, and the working tree is snapshotted to a shadow git repo so `undo` is exact.

## License

MIT
