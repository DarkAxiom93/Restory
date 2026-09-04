"""XSS-safety guards for the timeline UI and the served bootstrap.

Event data (file paths and shell commands captured from intercepted tool calls)
is attacker-influenced: a malicious or buggy agent can produce a command or path
containing ``<script>`` or ``<img onerror=...>``. Rendered unescaped, that would
be stored XSS on the exact page that holds ``window.__RESTORY_TOKEN__``, making
the token auth moot for that path.

The React UI renders every event field as a JSX text child, which React escapes
automatically, and the served token-bootstrap never inserts event data into the
DOM at all. The only way that safety regresses is if someone introduces a raw
HTML sink. These tests fail if that ever happens.

(The live "renders as inert text" behavior was also verified in a real browser
with a crafted event; React put the payload in ``textContent`` with no element
children and no script executed. A DOM-level assertion can't run in this
stdlib/pytest suite, so the durable guard here is the source/bundle scan, plus
the server-side HTML-escaping test in ``test_export``.)
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_UI_APP = _REPO_ROOT / "restory" / "ui" / "app"

# Raw-HTML sinks that bypass React's automatic escaping. None of these belong in
# code that renders event data.
_FORBIDDEN_SINKS = (
    "dangerouslySetInnerHTML",
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
)


def _ui_source_files() -> list[Path]:
    return sorted(_UI_APP.glob("*.tsx"))


def test_ui_app_sources_exist():
    files = _ui_source_files()
    assert files, f"expected UI source under {_UI_APP}"
    # page.tsx is the component that renders event commands/paths/reasons.
    assert any(f.name == "page.tsx" for f in files)


@pytest.mark.parametrize("sink", _FORBIDDEN_SINKS)
def test_ui_sources_use_no_raw_html_sink(sink):
    offenders = []
    for f in _ui_source_files():
        text = f.read_text(encoding="utf-8")
        if sink in text:
            offenders.append(f.name)
    assert not offenders, (
        f"raw-HTML sink {sink!r} found in {offenders}; event data must be "
        f"rendered as escaped JSX text, never injected as HTML"
    )


def test_page_renders_event_fields_as_jsx_text():
    """The command/reason/tag fields must be rendered as ``{...}`` JSX children.

    JSX text interpolation is what makes React escape the value; this is a
    lightweight check that the fields are still interpolated as text.
    """
    page = (_UI_APP / "page.tsx").read_text(encoding="utf-8")
    for expr in ("{ev.command", "{ev.reason}", "{ev.tool_name", "{t}"):
        assert expr in page, f"expected event field interpolation {expr!r} in page.tsx"
