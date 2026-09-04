"""Export the current/latest session as a shareable artifact.

``restory export`` renders the session summary (stats, tag breakdown, and the
table of blocked commands) as Markdown (default), JSON, or self-contained HTML,
ready to paste into a GitHub issue, a PR comment, or a tweet.

Read-only: it reads the recorded events via :mod:`restory.report` and never
modifies the store or the work tree.
"""

from __future__ import annotations

import html
import json
import shutil
import subprocess
from datetime import datetime, timezone

from . import report
from .config import find_repo_root

FORMATS = ("md", "json", "html")
_DEFAULT_URL = "https://github.com/DarkAxiom93/Restory"
_TITLE = "🐕‍🦺 Restory session report"
_TAGLINE = "a local safety gate & flight recorder for AI coding agents"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _repo_url() -> str:
    """Best-effort GitHub URL for the footer, from the git origin remote."""
    git = shutil.which("git")
    if git:
        try:
            proc = subprocess.run(
                [git, "config", "--get", "remote.origin.url"],
                cwd=str(find_repo_root()),
                capture_output=True,
                text=True,
            )
            url = proc.stdout.strip()
        except Exception:  # pragma: no cover - defensive
            url = ""
        normalized = _normalize_remote(url)
        if normalized:
            return normalized
    return _DEFAULT_URL


def _normalize_remote(url: str) -> str:
    """Turn a git remote URL into a browsable https URL (best effort)."""
    if not url:
        return ""
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("git@"):
        # git@github.com:user/repo -> https://github.com/user/repo
        host, _, path = url[4:].partition(":")
        if host and path:
            return f"https://{host}/{path}"
        return ""
    if url.startswith(("http://", "https://")):
        return url
    return ""


def _fmt_ts(ts: str | None) -> str:
    """Render an ISO timestamp as ``YYYY-MM-DD HH:MM:SS UTC`` (best effort)."""
    if not ts:
        return "-"
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, TypeError):
        return ts


def _session_line(session: dict | None) -> str:
    if session is None:
        return "No session anchor recorded — summarizing all recorded events."
    return (
        f"Session {session['id']} · started {_fmt_ts(session.get('started_at'))} "
        f"· anchor {str(session.get('anchor_commit', ''))[:12]}"
    )


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


# Visually similar, Markdown-inert stand-in for a backtick (U+02CB MODIFIER
# LETTER GRAVE ACCENT). A literal backtick cannot be backslash-escaped inside a
# code span, so we substitute it instead.
_BACKTICK_SUBSTITUTE = "ˋ"


def _md_cell(text: str) -> str:
    """Escape a value for safe rendering inside a GitHub-flavored Markdown table cell.

    Besides the table-structural characters (``|`` and newlines) and the
    backslash, this neutralizes backticks. Command text is attacker-influenced
    and is rendered wrapped in a `` `...` `` inline code span; a literal backtick
    in the value would close that span early and let the remainder render as live
    Markdown — an ``![](url)`` image or a ``[text](url)`` link outside any code
    span — which could make a blocked, dangerous command read as clean in a
    report meant to be pasted into an issue, PR, or social post. GFM table cells
    also can't contain literal newlines, and a backtick can't be backslash-
    escaped inside a code span, so the backtick is replaced with a look-alike
    (U+02CB) rather than escaped.
    """
    return (
        (text or "-")
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("`", _BACKTICK_SUBSTITUTE)
        .replace("\n", " ")
        .replace("\r", "")
    )


def render_markdown(data: dict, url: str) -> str:
    total = data["total_events"]
    blocked = data["blocked"]
    lines: list[str] = []
    lines.append(f"# {_TITLE}")
    lines.append("")
    lines.append(f"_{_session_line(data['session'])}_")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- **{total}** event{'s' if total != 1 else ''} recorded")
    lines.append(f"- **{blocked}** blocked 🚫")
    lines.append(f"- **{total - blocked}** approved ✅")
    lines.append("")

    tags = data["tags"]
    if tags:
        lines.append("### Blast-radius tags")
        lines.append("")
        lines.append("| Tag | Count |")
        lines.append("| --- | ----: |")
        for tag, count in tags.items():
            lines.append(f"| `{_md_cell(tag)}` | {count} |")
        lines.append("")

    blocked_commands = data["blocked_commands"]
    if blocked_commands:
        lines.append("## 🚫 Blocked commands")
        lines.append("")
        lines.append("| # | Time | Tool | Command | Reason |")
        lines.append("| --: | --- | --- | --- | --- |")
        for c in blocked_commands:
            cmd = _md_cell(c.get("command", ""))
            lines.append(
                f"| {c.get('id', '-')} | {_fmt_ts(c.get('timestamp'))} "
                f"| {_md_cell(c.get('tool_name', ''))} | `{cmd}` "
                f"| {_md_cell(c.get('reason', ''))} |"
            )
        lines.append("")
    elif total:
        lines.append("✅ **No commands were blocked in this session.**")
        lines.append("")

    lines.append("---")
    lines.append(f"*Generated by **restory** — {url}*")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #


def render_json(data: dict, url: str) -> str:
    payload = {
        "tool": "restory",
        "report": _TITLE,
        "url": url,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session": data["session"],
        "total_events": data["total_events"],
        "blocked": data["blocked"],
        "approved": data["total_events"] - data["blocked"],
        "tags": data["tags"],
        "blocked_commands": data["blocked_commands"],
    }
    return json.dumps(payload, indent=2)


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #


def _html_rows(rows: list[list[str]]) -> str:
    out = []
    for row in rows:
        cells = "".join(f"<td>{html.escape(str(c))}</td>" for c in row)
        out.append(f"<tr>{cells}</tr>")
    return "\n".join(out)


def render_html(data: dict, url: str) -> str:
    total = data["total_events"]
    blocked = data["blocked"]
    approved = total - blocked

    tag_rows = _html_rows([[tag, count] for tag, count in data["tags"].items()])
    tag_section = (
        f"""<h2>Blast-radius tags</h2>
<table><thead><tr><th>Tag</th><th>Count</th></tr></thead>
<tbody>{tag_rows}</tbody></table>"""
        if data["tags"]
        else ""
    )

    if data["blocked_commands"]:
        cmd_rows = _html_rows(
            [
                [
                    c.get("id", "-"),
                    _fmt_ts(c.get("timestamp")),
                    c.get("tool_name", ""),
                    c.get("command", ""),
                    c.get("reason", ""),
                ]
                for c in data["blocked_commands"]
            ]
        )
        cmd_section = (
            f"""<h2>🚫 Blocked commands</h2>
<table><thead><tr><th>#</th><th>Time</th><th>Tool</th><th>Command</th><th>Reason</th></tr></thead>
<tbody>{cmd_rows}</tbody></table>"""
        )
    else:
        cmd_section = "<p class='ok'>✅ No commands were blocked in this session.</p>"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(_TITLE)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         max-width: 820px; margin: 2.5rem auto; padding: 0 1.25rem; line-height: 1.55;
         background: #0d1117; color: #e6edf3; }}
  h1 {{ font-size: 1.7rem; margin-bottom: .25rem; }}
  .meta {{ color: #8b949e; margin: 0 0 1.5rem; font-size: .95rem; }}
  .stats {{ display: flex; gap: .75rem; flex-wrap: wrap; margin: 1rem 0 1.5rem; }}
  .stat {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px;
          padding: .7rem 1.1rem; min-width: 5.5rem; }}
  .stat .n {{ font-size: 1.5rem; font-weight: 700; display: block; }}
  .stat .l {{ color: #8b949e; font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; }}
  .blocked .n {{ color: #f85149; }}
  .approved .n {{ color: #3fb950; }}
  table {{ border-collapse: collapse; width: 100%; margin: .5rem 0 1.75rem; font-size: .92rem; }}
  th, td {{ text-align: left; padding: .5rem .65rem; border-bottom: 1px solid #21262d; vertical-align: top; }}
  th {{ color: #8b949e; font-weight: 600; }}
  td:nth-child(4), code {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }}
  .ok {{ color: #3fb950; font-weight: 600; }}
  footer {{ margin-top: 2rem; padding-top: 1rem; border-top: 1px solid #21262d;
            color: #8b949e; font-size: .9rem; }}
  a {{ color: #58a6ff; }}
</style>
</head>
<body>
<h1>{html.escape(_TITLE)}</h1>
<p class="meta">{html.escape(_session_line(data["session"]))}</p>
<div class="stats">
  <div class="stat"><span class="n">{total}</span><span class="l">events</span></div>
  <div class="stat blocked"><span class="n">{blocked}</span><span class="l">blocked</span></div>
  <div class="stat approved"><span class="n">{approved}</span><span class="l">approved</span></div>
</div>
{tag_section}
{cmd_section}
<footer>Generated by <strong>restory</strong> — {_TAGLINE}. <a href="{html.escape(url)}">{html.escape(url)}</a></footer>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


def render(data: dict, fmt: str = "md", url: str | None = None) -> str:
    """Render the report ``data`` in ``fmt`` (``md`` / ``json`` / ``html``)."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown format: {fmt!r} (expected one of {', '.join(FORMATS)})")
    resolved_url = url if url is not None else _repo_url()
    if fmt == "json":
        return render_json(data, resolved_url)
    if fmt == "html":
        return render_html(data, resolved_url)
    return render_markdown(data, resolved_url)


def gather() -> dict:
    """Read the store and build the report for the current/latest session."""
    return report.gather()
