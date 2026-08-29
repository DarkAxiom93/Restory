# Contributing to restory

Thanks for wanting to help! 🐕‍🦺 restory is a small project with a big job —
keeping AI coding agents from doing something you'll regret — and every bug
report, PR, and idea makes it more trustworthy. This guide gets you set up fast.

## Dev environment

```bash
git clone https://github.com/DarkAxiom93/Restory.git
cd Restory
pip install -e .
```

The live timeline UI is a Next.js app that gets exported to static files. Build
it once (and again whenever you change anything under `restory/ui/`):

```bash
cd restory/ui
npm install
npm run build
```

That writes the exported UI into `restory/ui/out`, which `restory open` serves.

## Running the tests

```bash
python -m pytest
```

**On Windows**, pytest's default temp directory can collide with the shadow git
repos the tests create. If you hit odd file-lock or permission errors, point
pytest at a short, local base temp dir:

```bash
python -m pytest --basetemp=.pytest-tmp
```

## Selfcheck

`scripts/selfcheck.py` runs an end-to-end sanity pass — it exercises the
classifier, store, and snapshot logic together, so it's a quick way to confirm
your changes didn't break the core flow:

```bash
python scripts/selfcheck.py
```

## Found a bypass? We especially want to hear it 🎯

restory's whole promise leans on the classifier catching dangerous effects, so
a command that *slips past it* is the most valuable report you can send. If you
find a payload that should have been blocked but wasn't, please
[open an issue](https://github.com/DarkAxiom93/Restory/issues/new?template=bypass_report.md)
with the exact command that got through. Use the **bypass report** template —
it's the fastest path to a fix.

## Pull requests

Small, focused PRs are the easiest to review and merge. Please run the tests and
the selfcheck before opening one. Not sure about an approach? Open an issue first
and let's talk it through — we're friendly.
