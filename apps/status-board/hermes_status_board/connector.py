"""MCP client snippet for the bridge status tools.

The tools themselves are served by hermes-agent-mcp-bridge. This module only
builds the connector document a Grok Bot or Cursor client saves.
"""

from __future__ import annotations

from urllib.parse import urlsplit


def connector_document(resource_url: str) -> dict[str, object]:
    parts = urlsplit(resource_url)
    host = (parts.hostname or "").lower()
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    if parts.scheme == "https" and host and not loopback:
        allowed = True
    elif parts.scheme == "http" and loopback:
        allowed = True
    else:
        allowed = False
    if not allowed or parts.username or parts.password or parts.fragment:
        raise ValueError("resource URL must be https, or http on loopback, with no userinfo")
    if not parts.path.endswith("/mcp"):
        raise ValueError("resource URL must end in /mcp")
    return {"mcpServers": {"hermes-bridge-status": {"url": resource_url}}}
