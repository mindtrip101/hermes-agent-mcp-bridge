"""Post the doctor result as a GitHub or Origin check run."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from hermes_bridge_doctor.report import Check, render

_SHA = re.compile(r"^[0-9a-fA-F]{40}$|^[0-9a-fA-F]{64}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class Requester(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response: ...


@dataclass(frozen=True)
class PublishTarget:
    kind: str
    http: Requester
    api_base: str
    token: str
    owner: str
    repo: str
    head_sha: str


def publish(checks: list[Check], target: PublishTarget, *, version: str) -> Check:
    if not _SHA.fullmatch(target.head_sha):
        return Check("publish", "fail", "head sha is missing or not hex", "set DOCTOR_HEAD_SHA to the commit sha")
    if not _SEGMENT.fullmatch(target.owner) or not _SEGMENT.fullmatch(target.repo):
        return Check("publish", "fail", "repository owner or name is invalid", "set the owner and repository name")
    if not target.token:
        return Check("publish", "fail", "the check token is missing", "set the forge token for --publish")
    summary = render(checks, version=version)
    if len(summary.encode("utf-8")) > 60_000:
        summary = summary[:20_000]
    failed = any(item.status == "fail" for item in checks)
    try:
        if target.kind == "github":
            response = _github(target, summary, failed)
        elif target.kind == "origin":
            response = _origin(target, summary, failed)
        else:
            return Check("publish", "fail", "unknown publish target", "use --publish github or --publish origin")
    except httpx.HTTPError:
        return Check("publish", "fail", "the check-run request failed", "confirm the API base URL and token")
    if response.status_code not in {200, 201}:
        return Check("publish", "fail", f"HTTP {response.status_code}", "confirm the token can write check runs")
    return Check("publish", "pass", f"posted to {target.kind}")


def _github(target: PublishTarget, summary: str, failed: bool) -> httpx.Response:
    return target.http.request(
        "POST",
        f"{target.api_base.rstrip('/')}/repos/{target.owner}/{target.repo}/check-runs",
        headers={
            "authorization": f"Bearer {target.token}",
            "accept": "application/vnd.github+json",
        },
        json={
            "name": "bridge-doctor",
            "head_sha": target.head_sha,
            "status": "completed",
            "conclusion": "failure" if failed else "success",
            "output": {"title": "Bridge Doctor", "summary": summary},
        },
    )


def _origin(target: PublishTarget, summary: str, failed: bool) -> httpx.Response:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    external = f"bridge-doctor-{target.head_sha[:12]}-{now}"
    return target.http.request(
        "POST",
        f"{target.api_base.rstrip('/')}/v1/origin/repos/{target.owner}/{target.repo}/check-runs",
        headers={"authorization": f"Bearer {target.token}", "accept": "application/json"},
        json={
            "headSha": target.head_sha,
            "checkSuite": {
                "key": "hermes-bridge-doctor",
                "name": "Hermes Bridge Doctor",
                "externalId": external,
            },
            "checkRun": {
                "key": "bridge-doctor",
                "name": "bridge-doctor",
                "status": "completed",
                "conclusion": "failure" if failed else "success",
                "externalUpdatedAt": now,
                "completedAt": now,
                "startedAt": now,
                "externalId": external,
                "output": {"title": "Bridge Doctor", "summary": summary},
            },
        },
    )
