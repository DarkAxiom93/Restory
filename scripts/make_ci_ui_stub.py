#!/usr/bin/env python3
"""Write the canonical CI stand-in for the exported Next.js UI.

The real UI (``restory/ui/out``) is a gitignored build artifact that
``pyproject.toml`` force-includes into the wheel, so a fresh checkout has no
``index.html`` and ``pip install -e .`` fails with "Forced include not found".
In CI we skip the JS toolchain and drop in this placeholder instead.

The stub must mirror the structural shape ``tests/test_server.py`` asserts on:
a ``<head>`` for the token bootstrap to be injected into, and at least one
bundle-style ``<script src=…/_next/static/…>`` so the "bootstrap runs before the
bundle scripts" contract can be verified. A real ``npm run build`` produces the
same shape (head + ``/_next/static`` chunks).

Both the test matrix and the coverage-badge job call this one script so the two
placeholders cannot drift apart again (that drift is what let the badge job go
red while the matrix stayed green). Run it locally the same way CI does:

    python scripts/make_ci_ui_stub.py restory/ui/out/index.html
"""

from __future__ import annotations

import pathlib
import sys

# Keep this in sync with what tests/test_server.py looks for: a <head> plus a
# bundle-style <script src=…/_next/static/…>.
_STUB = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>restory UI (CI test stub)</title>
</head>
<body>
<div id="__next"></div>
<script src="/_next/static/chunks/main.js"></script>
</body>
</html>
"""

_DEFAULT_OUT = pathlib.Path("restory") / "ui" / "out" / "index.html"


def main(argv: list[str]) -> int:
    out = pathlib.Path(argv[1]) if len(argv) > 1 else _DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_STUB, encoding="utf-8")
    print(f"Wrote CI UI stub to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
