"""Minimal HTML status page. It renders the redacted status document and nothing else."""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from typing import Any


def render_status(document: dict[str, Any]) -> str:
    version = escape(str(document.get("version", "")))
    queue = escape(str(document.get("queue_depth", "")))
    reachable = "yes" if document.get("hermes_reachable") else "no"
    sync = "on" if document.get("sync_ask_enabled") else "off"
    rows = _rows(document.get("calls"))
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes bridge status</title>
<style>
  body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 2rem; color: #1c1917; background: #fafaf9; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
  p {{ color: #44403c; }}
  dl {{ display: grid; grid-template-columns: 12rem 1fr; gap: 0.35rem 1rem; max-width: 40rem; }}
  dt {{ color: #57534e; }}
  table {{ border-collapse: collapse; width: 100%; max-width: 48rem; margin-top: 1.5rem; }}
  th, td {{ text-align: left; border-bottom: 1px solid #e7e5e4; padding: 0.4rem 0.6rem; font-size: 0.95rem; }}
  th {{ color: #57534e; font-weight: 600; }}
</style>
</head>
<body>
<h1>Hermes bridge status</h1>
<p>Health and recent tool-call metadata. Prompts and job ids are not shown on this page.</p>
<dl>
  <dt>Version</dt><dd>{version}</dd>
  <dt>Queue depth</dt><dd>{queue}</dd>
  <dt>Hermes reachable</dt><dd>{reachable}</dd>
  <dt>Sync ask</dt><dd>{sync}</dd>
</dl>
<table>
  <thead><tr><th>When (UTC)</th><th>Tool</th><th>Result</th><th>Error</th></tr></thead>
  <tbody>
{rows}
  </tbody>
</table>
</body>
</html>
"""


def _rows(calls: object) -> str:
    if not isinstance(calls, list) or not calls:
        return '    <tr><td colspan="4">No tool calls yet.</td></tr>'
    lines: list[str] = []
    for item in calls:
        if not isinstance(item, dict):
            continue
        when = escape(_when(item.get("at")))
        tool = escape(str(item.get("tool", "")))
        result = "ok" if item.get("ok") else "fail"
        error = escape(str(item.get("error_code") or ""))
        lines.append(f"    <tr><td>{when}</td><td>{tool}</td><td>{result}</td><td>{error}</td></tr>")
    return "\n".join(lines) or '    <tr><td colspan="4">No tool calls yet.</td></tr>'


def _when(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        return ""
    return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%d %H:%M:%S")
