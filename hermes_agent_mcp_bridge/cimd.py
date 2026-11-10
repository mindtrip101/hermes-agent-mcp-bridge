"""Client ID Metadata Documents. The client_id is an HTTPS URL we fetch once and cache."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from hermes_agent_mcp_bridge.errors import OAuthError
from hermes_agent_mcp_bridge.redirects import filter_redirects

_MAX_BODY = 65_536
Resolver = Callable[[str, int], list[str]]


def is_cimd_id(client_id: str) -> bool:
    parts = urlsplit(client_id)
    return parts.scheme == "https" and bool(parts.hostname)


def assert_public_https(url: str, *, resolve: Resolver | None = None) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.query or parts.fragment:
        raise OAuthError(400, "invalid_client", "Client metadata URL is not allowed.")
    if parts.port not in {None, 443}:
        raise OAuthError(400, "invalid_client", "Client metadata URL is not allowed.")
    host = parts.hostname or ""
    if not host or host.lower() == "localhost":
        raise OAuthError(400, "invalid_client", "Client metadata URL is not allowed.")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise OAuthError(400, "invalid_client", "Client metadata URL is not allowed.")
        return
    resolver = resolve or _resolve
    try:
        addresses = resolver(host, 443)
    except OSError as exc:
        raise OAuthError(400, "invalid_client", "Client metadata URL is not allowed.") from exc
    if not addresses:
        raise OAuthError(400, "invalid_client", "Client metadata URL is not allowed.")
    for item in addresses:
        ip = ipaddress.ip_address(item)
        if not ip.is_global:
            raise OAuthError(400, "invalid_client", "Client metadata URL is not allowed.")


def fetch_document(
    url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    resolve: Resolver | None = None,
    check_network: bool = True,
) -> dict[str, Any]:
    if check_network:
        assert_public_https(url, resolve=resolve)
    client = httpx.Client(transport=transport, timeout=5.0, follow_redirects=False, trust_env=False)
    try:
        response = client.get(url, headers={"accept": "application/json"})
    except httpx.HTTPError as exc:
        raise OAuthError(400, "invalid_client", "Client metadata could not be fetched.") from exc
    finally:
        client.close()
    if response.status_code != 200:
        raise OAuthError(400, "invalid_client", "Client metadata could not be fetched.")
    if len(response.content) > _MAX_BODY:
        raise OAuthError(400, "invalid_client", "Client metadata is too large.")
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type.lower():
        raise OAuthError(400, "invalid_client", "Client metadata is not JSON.")
    try:
        loaded = response.json()
    except ValueError as exc:
        raise OAuthError(400, "invalid_client", "Client metadata is not JSON.") from exc
    if not isinstance(loaded, dict):
        raise OAuthError(400, "invalid_client", "Client metadata is not JSON.")
    return loaded


def client_from_document(
    client_id: str,
    document: dict[str, Any],
    *,
    private_schemes: list[str],
) -> tuple[str, list[str]]:
    published = document.get("client_id")
    if published != client_id:
        raise OAuthError(400, "invalid_client", "Client metadata identity does not match.")
    methods = _methods(document)
    if "none" not in methods:
        raise OAuthError(400, "invalid_client", "This server only accepts public clients.")
    try:
        redirects = filter_redirects(document.get("redirect_uris"), private_schemes=private_schemes)
    except Exception as exc:
        raise OAuthError(400, "invalid_client", "Client metadata has no usable redirect URI.") from exc
    name = document.get("client_name")
    if not isinstance(name, str) or not name.strip():
        name = "Remote MCP client"
    return _clean_name(name), redirects


def _methods(document: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    plural = document.get("token_endpoint_auth_methods_supported")
    if isinstance(plural, list):
        found.update(item for item in plural if isinstance(item, str))
    singular = document.get("token_endpoint_auth_method")
    if isinstance(singular, str):
        found.add(singular)
    return found


def _clean_name(value: str) -> str:
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()
    return (cleaned or "Remote MCP client")[:80]


def _resolve(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    found: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and sockaddr[0] not in found:
            found.append(sockaddr[0])
    return found
