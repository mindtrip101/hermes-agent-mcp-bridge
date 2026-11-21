"""Live probes: discovery, the 401 challenge, and an owner-password login."""

from __future__ import annotations

import re
import secrets
from base64 import urlsafe_b64encode
from hashlib import sha256
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

import httpx

from hermes_bridge_doctor.report import Check

_REQUIRED_TOOLS = ("ask_hermes", "get_job", "bridge_status", "recent_calls")


class Requester(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response: ...


def probe_health(http: Requester, base: str) -> tuple[Check, str]:
    try:
        response = http.request("GET", _url(base, "/healthz"), follow_redirects=False)
    except httpx.HTTPError:
        return _fail("healthz", "the bridge did not answer", _reach_fix()), ""
    if response.status_code != 200:
        return _fail("healthz", f"HTTP {response.status_code}", _reach_fix()), ""
    try:
        body = response.json()
    except ValueError:
        return _fail("healthz", "the health response was not JSON", _reach_fix()), ""
    if not isinstance(body, dict) or body.get("ok") is not True:
        return _fail("healthz", "the health response did not report ok", _reach_fix()), ""
    version = body.get("version")
    if not isinstance(version, str) or not version:
        return _fail("healthz", "the health response omitted a version", _reach_fix()), ""
    return Check("healthz", "pass", f"version {version}"), version


def probe_version(found: str, expected: str) -> Check:
    if found == expected:
        return Check("version", "pass", found)
    return _fail(
        "version",
        f"bridge reports {found}, expected {expected}",
        "install the expected bridge version or pass --expect-version",
    )


def probe_resource(http: Requester, base: str) -> tuple[Check, str]:
    try:
        response = http.request("GET", _url(base, "/.well-known/oauth-protected-resource"), follow_redirects=False)
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return _fail("resource", "protected-resource metadata did not answer", _reach_fix()), ""
    resource = body.get("resource") if isinstance(body, dict) else None
    if response.status_code != 200 or not isinstance(resource, str) or not resource:
        return _fail("resource", "protected-resource metadata did not name a resource", _reach_fix()), ""
    return Check("resource", "pass", "protected resource advertised"), resource


def probe_discovery(http: Requester, base: str) -> Check:
    try:
        response = http.request("GET", _url(base, "/.well-known/oauth-authorization-server"), follow_redirects=False)
    except httpx.HTTPError:
        return _fail("discovery", "authorization-server metadata did not answer", _reach_fix())
    if response.status_code != 200:
        return _fail("discovery", f"HTTP {response.status_code}", _reach_fix())
    try:
        body = response.json()
    except ValueError:
        return _fail("discovery", "metadata was not JSON", _reach_fix())
    if not isinstance(body, dict):
        return _fail("discovery", "metadata was not a document", _reach_fix())
    methods = body.get("code_challenge_methods_supported")
    auth = body.get("token_endpoint_auth_methods_supported")
    if "S256" not in (methods or []) or "none" not in (auth or []) or not body.get("registration_endpoint"):
        return _fail(
            "discovery",
            "metadata is missing S256, public-client auth, or registration",
            "point --online at this bridge's issuer origin",
        )
    return Check("discovery", "pass", "S256 public client with registration")


def probe_auth_required(http: Requester, base: str) -> Check:
    try:
        response = http.request(
            "POST",
            _url(base, "/mcp"),
            headers={"accept": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            follow_redirects=False,
        )
    except httpx.HTTPError:
        return _fail("auth", "the MCP endpoint did not answer", _reach_fix())
    challenge = response.headers.get("www-authenticate", "")
    if response.status_code == 401 and "bearer" in challenge.lower() and "resource_metadata" in challenge.lower():
        return Check("auth", "pass", "unauthenticated MCP returns 401")
    return _fail(
        "auth",
        f"expected 401 with a bearer challenge, got HTTP {response.status_code}",
        "confirm POST /mcp is the bridge and not an open proxy",
    )


def probe_tools(http: Requester, base: str, *, resource: str, password: str) -> Check:
    try:
        names = _tool_names(http, base, resource=resource, password=password)
    except httpx.HTTPError:
        return _fail("tools", "the bridge request failed", _reach_fix())
    except ProbeError as exc:
        return _fail("tools", str(exc), "confirm the owner password and that dev_mode matches the issuer scheme")
    missing = [name for name in _REQUIRED_TOOLS if name not in names]
    if missing:
        return _fail("tools", "missing " + ", ".join(missing), "upgrade the bridge so these tools are listed")
    return Check("tools", "pass", ", ".join(_REQUIRED_TOOLS))


class ProbeError(Exception):
    pass


def _tool_names(http: Requester, base: str, *, resource: str, password: str) -> list[str]:
    registered = http.request(
        "POST",
        _url(base, "/oauth/register"),
        json={
            "client_name": "Bridge Doctor",
            "redirect_uris": ["http://127.0.0.1/callback"],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
        follow_redirects=False,
    )
    if registered.status_code != 201:
        raise ProbeError("client registration failed")
    client_id = registered.json().get("client_id")
    if not isinstance(client_id, str):
        raise ProbeError("client registration failed")
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(18)
    page = http.request(
        "GET",
        _url(base, "/oauth/authorize"),
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1/callback",
            "state": state,
            "code_challenge": urlsafe_b64encode(sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii"),
            "code_challenge_method": "S256",
            "resource": resource,
            "scope": "hermes:ask",
        },
        follow_redirects=False,
    )
    if page.status_code != 200:
        raise ProbeError("authorization did not open")
    match = re.search(r'name="request_id" value="([^"]+)"', page.text)
    if match is None:
        raise ProbeError("authorization did not open")
    approved = http.request(
        "POST",
        _url(base, "/oauth/authorize"),
        data={"request_id": match.group(1), "password": password, "decision": "approve"},
        follow_redirects=False,
    )
    if approved.status_code != 302:
        raise ProbeError("owner password was not accepted")
    query = parse_qs(urlsplit(approved.headers.get("location", "")).query)
    code = (query.get("code") or [""])[0]
    if not code:
        raise ProbeError("authorization code was missing")
    token = http.request(
        "POST",
        _url(base, "/oauth/token"),
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": "http://127.0.0.1/callback",
            "code_verifier": verifier,
            "resource": resource,
        },
        follow_redirects=False,
    )
    if token.status_code != 200:
        raise ProbeError("token request failed")
    access = token.json().get("access_token")
    if not isinstance(access, str):
        raise ProbeError("token request failed")
    listed = http.request(
        "POST",
        _url(base, "/mcp"),
        headers={"authorization": f"Bearer {access}", "accept": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        follow_redirects=False,
    )
    if listed.status_code != 200:
        raise ProbeError("tools/list failed")
    tools = listed.json().get("result", {}).get("tools", [])
    if not isinstance(tools, list):
        raise ProbeError("tools/list failed")
    return [item.get("name") for item in tools if isinstance(item, dict) and isinstance(item.get("name"), str)]


def _url(base: str, path: str) -> str:
    return base.rstrip("/") + path


def _fail(name: str, detail: str, fix: str) -> Check:
    return Check(name, "fail", detail, fix)


def _reach_fix() -> str:
    return "start the bridge and pass --online with its issuer origin, including the scheme and port"
