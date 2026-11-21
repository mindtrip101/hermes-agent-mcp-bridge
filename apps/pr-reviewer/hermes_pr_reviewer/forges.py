"""Read a pull request diff and post a comment review on GitHub or Origin."""

from __future__ import annotations

import time
from typing import Any, Protocol

import httpx
import jwt

from hermes_pr_reviewer.bridge import Requester
from hermes_pr_reviewer.events import PullRequestEvent


class ReviewError(Exception):
    """A fixed, secret-free description of a review failure."""


class Forge(Protocol):
    def fetch_diff(self, event: PullRequestEvent) -> str: ...

    def post_review(self, event: PullRequestEvent, body: str) -> None: ...


class GitHubForge:
    def __init__(
        self,
        http: Requester,
        *,
        api_base: str,
        token: str = "",
        app_id: str = "",
        private_key: str = "",
    ) -> None:
        self.http = http
        self.api_base = api_base.rstrip("/")
        self.token = token
        self.app_id = app_id
        self.private_key = private_key

    def fetch_diff(self, event: PullRequestEvent) -> str:
        response = self._send(
            "GET",
            f"/repos/{event.owner}/{event.repo}/pulls/{event.number}",
            token=self._token(event),
            accept="application/vnd.github.diff",
        )
        if response.status_code != 200:
            raise ReviewError("the pull request diff was not readable")
        return response.text

    def post_review(self, event: PullRequestEvent, body: str) -> None:
        response = self._send(
            "POST",
            f"/repos/{event.owner}/{event.repo}/pulls/{event.number}/reviews",
            token=self._token(event),
            accept="application/vnd.github+json",
            json={"event": "COMMENT", "body": body},
        )
        if response.status_code not in {200, 201}:
            raise ReviewError("the review comment was not accepted")

    def _token(self, event: PullRequestEvent) -> str:
        if self.token:
            return self.token
        if not self.app_id or not self.private_key or not event.installation_id:
            raise ReviewError("github credentials are missing")
        now = int(time.time())
        assertion = _jwt(
            {"iat": now - 60, "exp": now + 300, "iss": self.app_id},
            self.private_key,
            algorithm="RS256",
        )
        response = self._send(
            "POST",
            f"/app/installations/{event.installation_id}/access_tokens",
            token=assertion,
            accept="application/vnd.github+json",
        )
        if response.status_code not in {200, 201}:
            raise ReviewError("the github installation token was not issued")
        minted = response.json().get("token")
        if not isinstance(minted, str) or not minted:
            raise ReviewError("the github installation token was not issued")
        return minted

    def _send(self, method: str, path: str, *, token: str, accept: str, json: dict[str, Any] | None = None) -> httpx.Response:
        try:
            return self.http.request(
                method,
                self.api_base + path,
                headers={"authorization": f"Bearer {token}", "accept": accept},
                json=json,
            )
        except httpx.HTTPError as exc:
            raise ReviewError("the github request failed") from exc


class OriginForge:
    def __init__(self, http: Requester, *, api_base: str, app_id: str, private_key: str) -> None:
        self.http = http
        self.api_base = api_base.rstrip("/")
        self.app_id = app_id
        self.private_key = private_key

    def fetch_diff(self, event: PullRequestEvent) -> str:
        response = self._send(
            "GET",
            f"/v1/origin/repos/{event.owner}/{event.repo}/pulls/{event.number}/files?pageSize=100",
            token=self._token(event),
        )
        if response.status_code != 200:
            raise ReviewError("the pull request diff was not readable")
        return _patches(response.json())

    def post_review(self, event: PullRequestEvent, body: str) -> None:
        response = self._send(
            "POST",
            f"/v1/origin/repos/{event.owner}/{event.repo}/pulls/{event.number}/reviews",
            token=self._token(event),
            json={"verdict": "comment", "body": body},
        )
        if response.status_code not in {200, 201}:
            raise ReviewError("the review comment was not accepted")

    def _token(self, event: PullRequestEvent) -> str:
        if not self.app_id or not self.private_key or not event.installation_id:
            raise ReviewError("origin credentials are missing")
        now = int(time.time())
        assertion = _jwt(
            {"iss": self.app_id, "aud": "origin-apps", "iat": now, "exp": now + 300},
            self.private_key,
            algorithm="EdDSA",
            headers={"kid": self.app_id, "typ": "JWT"},
        )
        response = self._send(
            "POST",
            f"/v1/origin/app/installations/{event.installation_id}/access_tokens",
            token=assertion,
            json={
                "scopes": [
                    "repository:pull_requests:read",
                    "repository:pull_requests:reviews:write",
                ]
            },
        )
        if response.status_code not in {200, 201}:
            raise ReviewError("the origin installation token was not issued")
        minted = response.json().get("token")
        if not isinstance(minted, str) or not minted.startswith("oit_"):
            raise ReviewError("the origin installation token was not issued")
        return minted

    def _send(
        self,
        method: str,
        path: str,
        *,
        token: str,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            return self.http.request(
                method,
                self.api_base + path,
                headers={"authorization": f"Bearer {token}", "accept": "application/json"},
                json=json,
            )
        except httpx.HTTPError as exc:
            raise ReviewError("the origin request failed") from exc


class RoutingForge:
    """Dispatch by the event provider. One webhook process can serve both forges."""

    def __init__(self, *, github: GitHubForge | None, origin: OriginForge | None) -> None:
        self.github = github
        self.origin = origin

    def fetch_diff(self, event: PullRequestEvent) -> str:
        return self._forge(event).fetch_diff(event)

    def post_review(self, event: PullRequestEvent, body: str) -> None:
        self._forge(event).post_review(event, body)

    def _forge(self, event: PullRequestEvent) -> GitHubForge | OriginForge:
        forge = self.github if event.provider == "github" else self.origin
        if forge is None:
            raise ReviewError("this provider is not configured")
        return forge


def _jwt(claims: dict[str, object], key: str, *, algorithm: str, headers: dict[str, str] | None = None) -> str:
    encoded = jwt.encode(claims, key, algorithm=algorithm, headers=headers)
    if isinstance(encoded, bytes):
        return encoded.decode("ascii")
    return encoded


def _patches(document: object) -> str:
    if not isinstance(document, dict):
        raise ReviewError("the pull request diff was not readable")
    files = document.get("files")
    if not isinstance(files, list):
        raise ReviewError("the pull request diff was not readable")
    chunks: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        name = item.get("filename") if isinstance(item.get("filename"), str) else "file"
        patch = item.get("patch") if isinstance(item.get("patch"), str) else ""
        chunks.append(f"diff -- {name}\n{patch}")
    return "\n".join(chunks)
