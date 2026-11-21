"""Static config checks plus optional live probes."""

from __future__ import annotations

import os
from pathlib import Path

import httpx

from hermes_agent_mcp_bridge import __version__ as bridge_version
from hermes_agent_mcp_bridge.app import load_api_key, load_owner_password
from hermes_agent_mcp_bridge.config import Settings, load_settings
from hermes_agent_mcp_bridge.errors import ConfigError
from hermes_bridge_doctor.probe import (
    Requester,
    probe_auth_required,
    probe_discovery,
    probe_health,
    probe_resource,
    probe_tools,
    probe_version,
)
from hermes_bridge_doctor.report import Check

_SECRET_FIX = "set the matching environment variable or create the secret file with mode 0600"
_ONLINE_SKIP = "pass --online with the bridge issuer origin to probe a running bridge"


def run_checks(
    config: Path,
    *,
    online: str = "",
    require_secrets: bool = False,
    expect_version: str = "",
    http: Requester | None = None,
) -> list[Check]:
    try:
        settings = load_settings(config)
    except ConfigError as exc:
        return [Check("config", "fail", str(exc), "compare the file with config.example.yaml")]
    checks = [Check("config", "pass", "parsed")]
    password, password_check = _secret("owner_password", lambda: load_owner_password(settings, generate=False), require=require_secrets)
    api_check = _api_key(settings, require=require_secrets)
    checks.extend([password_check, api_check, _data_dir(settings)])
    if not online:
        for name in ("healthz", "version", "discovery", "resource", "auth", "tools"):
            checks.append(Check(name, "skip", _ONLINE_SKIP, _ONLINE_SKIP))
        return checks
    client = http if http is not None else httpx.Client(timeout=10, follow_redirects=False)
    health, found = probe_health(client, online)
    checks.append(health)
    if health.status != "pass":
        for name in ("version", "discovery", "resource", "auth", "tools"):
            checks.append(Check(name, "skip", "blocked because healthz failed", health.fix))
        return checks
    checks.append(probe_version(found, expect_version or bridge_version))
    checks.append(probe_discovery(client, online))
    resource_check, resource = probe_resource(client, online)
    checks.append(resource_check)
    checks.append(probe_auth_required(client, online))
    if online.rstrip("/") != settings.server.issuer.rstrip("/"):
        checks.append(
            Check(
                "issuer_match",
                "skip",
                "online URL differs from the config issuer",
                "use the config file for that bridge when the origins should match",
            )
        )
    if password and resource:
        checks.append(probe_tools(client, online, resource=resource, password=password))
    else:
        checks.append(Check("tools", "fail" if require_secrets else "skip", "owner password is missing", _SECRET_FIX))
    return checks


def _secret(name: str, loader: object, *, require: bool) -> tuple[str, Check]:
    try:
        loaded = loader()  # type: ignore[operator]
    except ConfigError as exc:
        missing = "missing" in str(exc)
        status = "skip" if missing and not require else "fail"
        return "", Check(name, status, str(exc), _SECRET_FIX)
    value = loaded[0] if isinstance(loaded, tuple) else ""
    if not isinstance(value, str):
        value = ""
    return value, Check(name, "pass", "present")


def _api_key(settings: Settings, *, require: bool) -> Check:
    if settings.hermes.backend == "cli":
        return Check("hermes_api_key", "pass", "cli backend does not use an API key")
    _value, check = _secret("hermes_api_key", lambda: (load_api_key(settings), False), require=require)
    return check


def _data_dir(settings: Settings) -> Check:
    path = Path(settings.data_dir)
    parent = path if path.exists() else path.parent
    if parent.exists() and not os.access(parent, os.W_OK):
        return Check("data_dir", "fail", "not writable", f"make {parent} writable by the bridge user")
    detail = "present" if path.exists() else "will be created on init"
    return Check("data_dir", "pass", detail)
