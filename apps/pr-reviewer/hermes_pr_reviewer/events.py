"""Pull request events from a GitHub webhook or an Origin webhook envelope."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

_SEGMENT = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SHA = re.compile(r"^[0-9a-fA-F]{40}$|^[0-9a-fA-F]{64}$")
_INSTALL = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_GITHUB_REVIEW = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})
_ORIGIN_REVIEW = frozenset(
    {
        "pull_request.created",
        "pull_request.head_ref.pushed",
        "pull_request.reopened",
        "pull_request.published",
    }
)


@dataclass(frozen=True)
class PullRequestEvent:
    provider: str
    delivery_id: str
    action: str
    owner: str
    repo: str
    number: str
    title: str
    draft: bool
    head_sha: str
    base_sha: str
    head_ref: str
    base_ref: str
    installation_id: str
    should_review: bool


def parse_event(provider: str, body: bytes, headers: Mapping[str, str]) -> PullRequestEvent | None:
    if provider == "github" and headers.get("x-github-event") == "ping":
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if provider == "github":
        return _github(payload, headers, body)
    if provider == "origin":
        return _origin(payload, headers, body)
    return None


def _github(payload: dict[str, object], headers: Mapping[str, str], body: bytes) -> PullRequestEvent | None:
    action = payload.get("action")
    pull = payload.get("pull_request")
    repository = payload.get("repository")
    if not isinstance(action, str) or not isinstance(pull, dict) or not isinstance(repository, dict):
        return None
    owner = _owner_login(repository.get("owner"))
    repo = repository.get("name")
    number = pull.get("number")
    if not isinstance(repo, str) or not isinstance(number, int):
        return None
    head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
    base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
    installation = payload.get("installation") if isinstance(payload.get("installation"), dict) else {}
    install_id = installation.get("id")
    event = _event(
        provider="github",
        delivery_id=headers.get("x-github-delivery") or _digest(body),
        action=action,
        owner=owner,
        repo=repo,
        number=str(number),
        title=pull.get("title") if isinstance(pull.get("title"), str) else "",
        draft=bool(pull.get("draft")),
        head_sha=_sha(head.get("sha")),
        base_sha=_sha(base.get("sha")),
        head_ref=_ref(head.get("ref")),
        base_ref=_ref(base.get("ref")),
        installation_id=str(install_id) if isinstance(install_id, int) else "",
        should_review=action in _GITHUB_REVIEW,
    )
    return event


def _origin(payload: dict[str, object], headers: Mapping[str, str], body: bytes) -> PullRequestEvent | None:
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    kind = event.get("type")
    inner = event.get("payload")
    if not isinstance(kind, str) or not isinstance(inner, dict):
        return None
    pull = inner.get("pullRequest")
    repository = inner.get("repository")
    if not isinstance(pull, dict) or not isinstance(repository, dict):
        return None
    owner = _owner_slug(repository.get("owner"))
    repo = repository.get("name")
    number = pull.get("number")
    if not isinstance(repo, str) or not isinstance(number, str) or not number.isdigit():
        return None
    head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
    base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
    install = headers.get("webhook-installation-id") or payload.get("installationId")
    event_id = _event(
        provider="origin",
        delivery_id=headers.get("webhook-id") or _digest(body),
        action=kind,
        owner=owner,
        repo=repo,
        number=number,
        title=pull.get("title") if isinstance(pull.get("title"), str) else "",
        draft=bool(pull.get("draft")),
        head_sha=_sha(head.get("sha")),
        base_sha=_sha(base.get("sha")),
        head_ref=_ref(head.get("ref")),
        base_ref=_ref(base.get("ref")),
        installation_id=install if isinstance(install, str) else "",
        should_review=kind in _ORIGIN_REVIEW,
    )
    return event_id


def _event(**kwargs: object) -> PullRequestEvent | None:
    owner = kwargs["owner"]
    repo = kwargs["repo"]
    number = kwargs["number"]
    install = kwargs["installation_id"]
    if not isinstance(owner, str) or not isinstance(repo, str) or not isinstance(number, str):
        return None
    if not _SEGMENT.fullmatch(owner) or not _SEGMENT.fullmatch(repo) or not number.isdigit():
        return None
    if kwargs["head_sha"] is None or kwargs["base_sha"] is None:
        return None
    if install and (not isinstance(install, str) or not _INSTALL.fullmatch(install)):
        return None
    return PullRequestEvent(
        provider=str(kwargs["provider"]),
        delivery_id=str(kwargs["delivery_id"])[:200],
        action=str(kwargs["action"])[:120],
        owner=owner,
        repo=repo,
        number=number,
        title=str(kwargs["title"])[:500],
        draft=bool(kwargs["draft"]),
        head_sha=str(kwargs["head_sha"]),
        base_sha=str(kwargs["base_sha"]),
        head_ref=str(kwargs["head_ref"]),
        base_ref=str(kwargs["base_ref"]),
        installation_id=str(install),
        should_review=bool(kwargs["should_review"]),
    )


def _owner_login(owner: object) -> str:
    if isinstance(owner, dict) and isinstance(owner.get("login"), str):
        return owner["login"]
    return ""


def _owner_slug(owner: object) -> str:
    if isinstance(owner, dict) and isinstance(owner.get("slug"), str):
        return owner["slug"]
    return ""


def _sha(value: object) -> str | None:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        return None
    return value


def _ref(value: object) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 200 or any(ord(ch) < 32 for ch in cleaned):
        return ""
    return cleaned


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
