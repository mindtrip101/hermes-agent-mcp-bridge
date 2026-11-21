"""Command line. `serve` listens for webhooks. `check-config` prints which settings are present."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from hermes_pr_reviewer import __version__
from hermes_pr_reviewer.bridge import BridgeClient
from hermes_pr_reviewer.forges import GitHubForge, OriginForge, RoutingForge
from hermes_pr_reviewer.server import serve
from hermes_pr_reviewer.service import ReviewService
from hermes_pr_reviewer.signatures import keys_from_jwks, verify_github, verify_origin


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    bridge_url: str
    resource: str
    password: str
    bind_host: str
    bind_port: int
    sync: bool
    review_drafts: bool
    max_prompt_chars: int
    timeout: float
    github_webhook_secret: str
    github_token: str
    github_app_id: str
    github_private_key: str
    github_api_base: str
    origin_app_id: str
    origin_private_key: str
    origin_api_base: str
    origin_jwks_url: str


class EnvVerifier:
    def __init__(self, settings: Settings, keys: list[object]) -> None:
        self.settings = settings
        self.keys = keys

    def verify(self, provider: str, body: bytes, headers: dict[str, str], *, now: float) -> bool:
        if provider == "github":
            return verify_github(self.settings.github_webhook_secret, body, headers.get("x-hub-signature-256", ""))
        if provider == "origin":
            return verify_origin(body, headers, self.keys, now=now)  # type: ignore[arg-type]
        return False


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="hermes-pr-reviewer")
    parser.add_argument("command", choices=("serve", "check-config"))
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    if args.command == "check-config":
        _print_config(settings)
        return
    keys = _load_origin_keys(settings)
    if settings.origin_app_id and not keys:
        print("origin webhook keys are unavailable; origin deliveries will be rejected", file=sys.stderr)
    http = httpx.Client(timeout=30, follow_redirects=False)
    bridge = BridgeClient(http, base_url=settings.bridge_url, resource=settings.resource, password=settings.password)
    github = None
    if settings.github_token or (settings.github_app_id and settings.github_private_key):
        github = GitHubForge(
            http,
            api_base=settings.github_api_base,
            token=settings.github_token,
            app_id=settings.github_app_id,
            private_key=settings.github_private_key,
        )
    origin = None
    if settings.origin_app_id and settings.origin_private_key:
        origin = OriginForge(
            http,
            api_base=settings.origin_api_base,
            app_id=settings.origin_app_id,
            private_key=settings.origin_private_key,
        )
    service = ReviewService(
        verifier=EnvVerifier(settings, keys),
        bridge=bridge,
        forge=RoutingForge(github=github, origin=origin),
        sync=settings.sync,
        review_drafts=settings.review_drafts,
        max_prompt_chars=settings.max_prompt_chars,
        timeout=settings.timeout,
    )
    serve(service, settings.bind_host, settings.bind_port)


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    bridge_url = env.get("HERMES_REVIEW_BRIDGE_URL", "").rstrip("/")
    password = env.get("HERMES_BRIDGE_OWNER_PASSWORD", "")
    if not bridge_url.startswith(("http://", "https://")):
        raise ConfigError("set HERMES_REVIEW_BRIDGE_URL to the bridge origin")
    if len(password) < 14 or any(ch.isspace() for ch in password):
        raise ConfigError("set HERMES_BRIDGE_OWNER_PASSWORD to the bridge owner password")
    github_secret = env.get("GITHUB_WEBHOOK_SECRET", "")
    origin_app = env.get("ORIGIN_APP_ID", "")
    if not github_secret and not origin_app:
        raise ConfigError("set GITHUB_WEBHOOK_SECRET or ORIGIN_APP_ID")
    resource = env.get("HERMES_REVIEW_BRIDGE_RESOURCE", "") or (bridge_url + "/mcp")
    return Settings(
        bridge_url=bridge_url,
        resource=resource,
        password=password,
        bind_host=env.get("HERMES_REVIEW_BIND_HOST", "127.0.0.1"),
        bind_port=_port(env.get("HERMES_REVIEW_BIND_PORT", "8780")),
        sync=_bool(env.get("HERMES_REVIEW_SYNC", "")),
        review_drafts=_bool(env.get("HERMES_REVIEW_DRAFTS", "")),
        max_prompt_chars=_bounded(env.get("HERMES_REVIEW_MAX_PROMPT_CHARS", ""), default=7000, low=500, high=8000),
        timeout=float(_bounded(env.get("HERMES_REVIEW_TIMEOUT_SECONDS", ""), default=90, low=5, high=300)),
        github_webhook_secret=github_secret,
        github_token=env.get("GITHUB_TOKEN", ""),
        github_app_id=env.get("GITHUB_APP_ID", ""),
        github_private_key=_pem(env, "GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_PRIVATE_KEY_FILE"),
        github_api_base=env.get("GITHUB_API_BASE", "https://api.github.com") or "https://api.github.com",
        origin_app_id=origin_app,
        origin_private_key=_pem(env, "ORIGIN_APP_PRIVATE_KEY", "ORIGIN_APP_PRIVATE_KEY_FILE"),
        origin_api_base=env.get("ORIGIN_API_BASE", "https://api.cursor.com") or "https://api.cursor.com",
        origin_jwks_url=env.get("ORIGIN_JWKS_URL", "https://api.cursor.com/v1/origin/keys"),
    )


def _print_config(settings: Settings) -> None:
    rows = {
        "bridge_url": bool(settings.bridge_url),
        "owner_password": True,
        "github_webhook_secret": bool(settings.github_webhook_secret),
        "github_token": bool(settings.github_token),
        "github_app": bool(settings.github_app_id and settings.github_private_key),
        "origin_app": bool(settings.origin_app_id and settings.origin_private_key),
    }
    print(f"hermes-pr-reviewer {__version__}")
    for name, present in rows.items():
        print(f"{name}: {'set' if present else 'missing'}")


def _load_origin_keys(settings: Settings) -> list[object]:
    if not settings.origin_app_id or not settings.origin_jwks_url:
        return []
    try:
        response = httpx.get(settings.origin_jwks_url, timeout=10, follow_redirects=False)
        response.raise_for_status()
        return list(keys_from_jwks(response.content))
    except (httpx.HTTPError, ValueError):
        return []


def _pem(env: dict[str, str], value_name: str, file_name: str) -> str:
    path = env.get(file_name, "")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"{file_name} could not be read") from exc
    return env.get(value_name, "").replace("\\n", "\n")


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise ConfigError("HERMES_REVIEW_BIND_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ConfigError("HERMES_REVIEW_BIND_PORT must be an integer")
    return port


def _bounded(value: str, *, default: int, low: int, high: int) -> int:
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError("a numeric reviewer setting is invalid") from exc
    if not low <= parsed <= high:
        raise ConfigError("a numeric reviewer setting is invalid")
    return parsed
