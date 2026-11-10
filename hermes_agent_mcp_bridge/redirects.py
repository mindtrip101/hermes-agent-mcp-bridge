"""Redirect URI policy for public MCP clients.

HTTPS URIs must match a registered URI exactly. Loopback HTTP URIs ignore the
port (RFC 8252 section 7.3) and treat localhost, 127.0.0.1, and ::1 as the
same host, which desktop clients require when the port changes per login.

A registration may list a URI this server will not use. Those URIs are dropped
and the rest are kept (RFC 7591). An empty remainder is an error. One rejected
URI does not fail the others: desktop clients often send a private-use scheme
next to an HTTPS URI and a loopback URI.
"""

from __future__ import annotations

from urllib.parse import urlsplit

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


class RedirectRejected(ValueError):
    pass


def _no_controls(value: str) -> bool:
    return bool(value) and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    return host.lower().strip("[]") in _LOOPBACK


def validate_redirect(uri: str, *, private_schemes: list[str]) -> str:
    """Return the canonical URI or raise RedirectRejected. Does not contact the network."""
    if not _no_controls(uri) or len(uri) > 512 or any(ch.isspace() for ch in uri):
        raise RedirectRejected("unsafe")
    parts = urlsplit(uri)
    if parts.fragment or parts.username or parts.password:
        raise RedirectRejected("fragment_or_userinfo")
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme == "https":
        if not host or host in _LOOPBACK:
            raise RedirectRejected("https_host")
        return uri
    if scheme == "http":
        if not is_loopback_host(host):
            raise RedirectRejected("http_host")
        return uri
    allowed = {item.lower() for item in private_schemes}
    if scheme in allowed and parts.netloc and "." in parts.netloc:
        return uri
    # cursor://anysphere.cursor-mcp/oauth/callback has a dot in the authority.
    # Schemes without a dotted authority stay rejected (RFC 8252 section 8.4)
    # unless the operator listed the scheme AND the authority contains a dot.
    raise RedirectRejected("scheme")


def filter_redirects(uris: object, *, private_schemes: list[str]) -> list[str]:
    if not isinstance(uris, list) or not uris or len(uris) > 8:
        raise RedirectRejected("list")
    kept: list[str] = []
    for item in uris:
        if not isinstance(item, str):
            continue
        try:
            canonical = validate_redirect(item, private_schemes=private_schemes)
        except RedirectRejected:
            continue
        if canonical not in kept:
            kept.append(canonical)
    if not kept:
        raise RedirectRejected("none")
    return kept


def _path_query(parts: object) -> tuple[str, str]:
    path = parts.path or "/"  # type: ignore[attr-defined]
    if path == "":
        path = "/"
    return path, parts.query  # type: ignore[attr-defined]


def redirect_matches(requested: str, registered: str) -> bool:
    if len(requested) == len(registered) and requested == registered:
        return True
    left = urlsplit(requested)
    right = urlsplit(registered)
    if left.scheme.lower() != right.scheme.lower():
        return False
    if left.scheme.lower() != "http":
        return False
    if not is_loopback_host(left.hostname) or not is_loopback_host(right.hostname):
        return False
    return _path_query(left) == _path_query(right)


def matches_any(requested: str, registered: list[str]) -> bool:
    return any(redirect_matches(requested, item) for item in registered)


def warns_loopback(uri: str) -> bool:
    parts = urlsplit(uri)
    if parts.scheme.lower() != "https":
        return True
    return is_loopback_host(parts.hostname)
