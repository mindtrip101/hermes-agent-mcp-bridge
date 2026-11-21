"""OAuth public-client session against the bridge MCP endpoint."""

from __future__ import annotations

import re
import secrets
import time
from base64 import urlsafe_b64encode
from hashlib import sha256
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

import httpx


class BridgeError(Exception):
    """A fixed, secret-free description of a bridge failure."""


class Requester(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response: ...


class BridgeClient:
    def __init__(
        self,
        http: Requester,
        *,
        base_url: str,
        resource: str,
        password: str,
        client_name: str = "Hermes PR Reviewer",
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.resource = resource
        self._password = password
        self.client_name = client_name
        self._token = ""
        self._expires_at = 0.0
        self._client_id = ""
        self._redirect = "http://127.0.0.1/callback"

    def ask(self, prompt: str, *, timeout: float) -> str:
        self._ensure_token()
        body = self._rpc("tools/call", {"name": "ask_hermes", "arguments": {"prompt": prompt}})
        result = _tool_result(body)
        job_id = result.get("job_id")
        if not isinstance(job_id, str):
            raise BridgeError("the bridge did not queue a job")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            polled = self._rpc("tools/call", {"name": "get_job", "arguments": {"job_id": job_id}})
            job = _tool_result(polled)
            status = job.get("status")
            if status == "succeeded":
                return str(job.get("result") or "")
            if status == "failed":
                raise BridgeError("the local agent run failed")
            time.sleep(0.05)
        raise BridgeError("the bridge job did not finish before the wait elapsed")

    def _ensure_token(self) -> None:
        if self._token and time.time() < self._expires_at - 30:
            return
        if not self._client_id:
            self._client_id = self._register()
        token, expires = self._login(self._client_id)
        self._token = token
        self._expires_at = time.time() + expires

    def _register(self) -> str:
        response = self._send(
            "POST",
            "/oauth/register",
            json={
                "client_name": self.client_name,
                "redirect_uris": [self._redirect],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        if response.status_code != 201:
            raise BridgeError("bridge client registration failed")
        client_id = response.json().get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise BridgeError("bridge client registration failed")
        return client_id

    def _login(self, client_id: str) -> tuple[str, int]:
        verifier = secrets.token_urlsafe(48)
        state = secrets.token_urlsafe(18)
        page = self._send(
            "GET",
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": self._redirect,
                "state": state,
                "code_challenge": _s256(verifier),
                "code_challenge_method": "S256",
                "resource": self.resource,
                "scope": "hermes:ask",
            },
        )
        if page.status_code != 200:
            raise BridgeError("bridge authorization did not open")
        match = re.search(r'name="request_id" value="([^"]+)"', page.text)
        if match is None:
            raise BridgeError("bridge authorization did not open")
        approved = self._send(
            "POST",
            "/oauth/authorize",
            data={"request_id": match.group(1), "password": self._password, "decision": "approve"},
        )
        if approved.status_code != 302:
            raise BridgeError("bridge owner password was not accepted")
        query = parse_qs(urlsplit(approved.headers.get("location", "")).query)
        code = (query.get("code") or [""])[0]
        if not code or (query.get("state") or [""])[0] != state:
            raise BridgeError("bridge authorization code was missing")
        token = self._send(
            "POST",
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": self._redirect,
                "code_verifier": verifier,
                "resource": self.resource,
            },
        )
        if token.status_code != 200:
            raise BridgeError("bridge token request failed")
        body = token.json()
        access = body.get("access_token")
        if not isinstance(access, str) or not access:
            raise BridgeError("bridge token request failed")
        expires = body.get("expires_in")
        ttl = expires if isinstance(expires, int) and expires > 0 else 300
        return access, ttl

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self._send(
            "POST",
            "/mcp",
            headers={
                "authorization": f"Bearer {self._token}",
                "accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        if response.status_code == 401:
            self._token = ""
            raise BridgeError("bridge rejected the access token")
        if response.status_code != 200:
            raise BridgeError("bridge MCP call failed")
        body = response.json()
        if not isinstance(body, dict):
            raise BridgeError("bridge MCP call failed")
        return body

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        kwargs.setdefault("follow_redirects", False)
        try:
            return self.http.request(method, self.base_url + path, **kwargs)
        except httpx.HTTPError as exc:
            raise BridgeError("the bridge request failed") from exc


def _s256(verifier: str) -> str:
    return urlsafe_b64encode(sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def _tool_result(body: dict[str, Any]) -> dict[str, Any]:
    result = body.get("result")
    if not isinstance(result, dict):
        raise BridgeError("bridge MCP call failed")
    if result.get("isError"):
        raise BridgeError("the bridge rejected the tool call")
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        raise BridgeError("bridge MCP call failed")
    return structured
