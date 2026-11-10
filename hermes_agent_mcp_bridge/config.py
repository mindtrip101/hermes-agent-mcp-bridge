"""YAML configuration with environment overrides. No secret values live on the model."""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Literal, NoReturn
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from hermes_agent_mcp_bridge.errors import ConfigError

_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


class ServerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bind_host: str = "127.0.0.1"
    bind_port: int = 8765
    issuer: str
    resource: str | None = None
    allowed_hosts: list[str] = Field(default_factory=list)
    allow_non_loopback_bind: bool = False
    dev_mode: bool = False


class ConsentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exposure: Literal["public", "tailnet"] = "public"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8766
    public_url: str | None = None
    owner_password_file: str = "secrets/owner_password"


class HermesSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["http", "cli"] = "http"
    base_url: str = "http://127.0.0.1:8642"
    api_key_file: str = "secrets/hermes_api_key"
    allow_non_loopback: bool = False
    poll_interval_seconds: float = 1.0
    run_timeout_seconds: int = 300
    cli_argv: list[str] = Field(default_factory=list)
    instructions: str = ""


class TokenSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_ttl_seconds: int = 900
    refresh_ttl_seconds: int = 2_592_000
    code_ttl_seconds: int = 120
    clock_skew_seconds: int = 30


class LimitSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_prompt_chars: int = 8000
    max_output_chars: int = 32_000
    max_queue: int = 8
    asks_per_hour: int = 30
    polls_per_minute: int = 120
    sync_ask_enabled: bool = False
    sync_timeout_seconds: int = 30
    job_retention_hours: int = 168
    worker_interval_seconds: float = 0.5


class RedirectSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # `cursor` is listed so a desktop client's private-use redirect can be
    # registered beside its HTTPS redirect. Clear the list to refuse it.
    private_schemes: list[str] = Field(default_factory=lambda: ["cursor"])


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server: ServerSettings
    consent: ConsentSettings = Field(default_factory=ConsentSettings)
    hermes: HermesSettings = Field(default_factory=HermesSettings)
    tokens: TokenSettings = Field(default_factory=TokenSettings)
    limits: LimitSettings = Field(default_factory=LimitSettings)
    redirects: RedirectSettings = Field(default_factory=RedirectSettings)
    data_dir: str = "data"
    subject: str = "owner"

    def __repr__(self) -> str:
        return f"Settings(issuer={self.server.issuer!r}, resource={self.server.resource!r})"

    @model_validator(mode="after")
    def _semantics(self) -> Settings:
        dev = self.server.dev_mode
        self.server.issuer = _issuer(self.server.issuer, dev_mode=dev)
        resource = self.server.resource or (self.server.issuer + "/mcp")
        self.server.resource = _resource(resource, dev_mode=dev)
        if _origin(self.server.issuer) != _origin(self.server.resource):
            raise ConfigError("issuer and resource must share a scheme, host, and port")
        _bind(self.server.bind_host, allow_other=self.server.allow_non_loopback_bind)
        _port(self.server.bind_port)
        _bind(self.consent.bind_host, allow_other=False)
        _port(self.consent.bind_port)
        if self.consent.exposure == "tailnet":
            if not self.consent.public_url:
                raise ConfigError("consent.public_url is required when exposure is tailnet")
            self.consent.public_url = _issuer(self.consent.public_url, dev_mode=dev)
            if (self.consent.bind_port == self.server.bind_port) and (
                self.consent.bind_host == self.server.bind_host
            ):
                raise ConfigError("tailnet consent needs its own port")
        elif self.consent.public_url:
            raise ConfigError("consent.public_url is only used when exposure is tailnet")
        _hermes_url(self.hermes.base_url, allow_other=self.hermes.allow_non_loopback)
        if self.hermes.backend == "cli":
            if not self.hermes.cli_argv or "{prompt_file}" not in self.hermes.cli_argv:
                raise ConfigError("cli backend requires cli_argv containing {prompt_file}")
            if any("ignore-rules" in part or "\n" in part or "\x00" in part for part in self.hermes.cli_argv):
                raise ConfigError("cli argv is unsafe")
        if len(self.hermes.instructions) > 2000:
            raise ConfigError("hermes.instructions is too long")
        if not 60 <= self.tokens.access_ttl_seconds <= 900:
            raise ConfigError("access_ttl_seconds must be between 60 and 900")
        if not 3600 <= self.tokens.refresh_ttl_seconds <= 7_776_000:
            raise ConfigError("refresh_ttl_seconds out of range")
        if not 30 <= self.tokens.code_ttl_seconds <= 600:
            raise ConfigError("code_ttl_seconds out of range")
        if not 0 <= self.tokens.clock_skew_seconds <= 120:
            raise ConfigError("clock_skew_seconds out of range")
        if not 1 <= self.limits.max_prompt_chars <= 100_000:
            raise ConfigError("max_prompt_chars out of range")
        if not 1 <= self.limits.max_output_chars <= 200_000:
            raise ConfigError("max_output_chars out of range")
        if not 1 <= self.limits.max_queue <= 100:
            raise ConfigError("max_queue out of range")
        if not 1 <= self.limits.sync_timeout_seconds <= 120:
            raise ConfigError("sync_timeout_seconds must be between 1 and 120")
        schemes = []
        for item in self.redirects.private_schemes:
            lowered = item.lower()
            if not lowered.isascii() or not lowered.isalnum():
                raise ConfigError("private redirect scheme must be alphanumeric")
            schemes.append(lowered)
        self.redirects.private_schemes = schemes
        if not self.subject or len(self.subject) > 64 or any(ch.isspace() for ch in self.subject):
            raise ConfigError("subject is empty or unsafe")
        return self

    @property
    def authorization_endpoint(self) -> str:
        if self.consent.exposure == "tailnet":
            assert self.consent.public_url is not None
            return self.consent.public_url + "/oauth/authorize"
        return self.server.issuer + "/oauth/authorize"

    @property
    def allowed_hostnames(self) -> set[str]:
        hosts = {_hostname(self.server.issuer), "localhost", "127.0.0.1", "::1"}
        if self.consent.public_url:
            hosts.add(_hostname(self.consent.public_url))
        hosts.update(item.lower() for item in self.server.allowed_hosts)
        return {item for item in hosts if item}


def load_settings(path: Path) -> Settings:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError("config file could not be read") from exc
    except yaml.YAMLError as exc:
        raise ConfigError("invalid config") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    _apply_env(raw)
    try:
        settings = Settings.model_validate(raw)
    except ValidationError as exc:
        _reraise_config(exc)
    except ConfigError:
        raise
    return _resolve_paths(settings, path.parent.resolve())


def _reraise_config(exc: ValidationError) -> NoReturn:
    for item in exc.errors():
        ctx = item.get("ctx") or {}
        original = ctx.get("error")
        if isinstance(original, ConfigError):
            raise original from None
    raise ConfigError("invalid config") from exc


def _apply_env(raw: dict[str, object]) -> None:
    def nest(section: str) -> dict[str, object]:
        current = raw.get(section)
        if current is None:
            current = {}
            raw[section] = current
        if not isinstance(current, dict):
            raise ConfigError(f"{section} must be a mapping")
        return current

    mapping: list[tuple[str, str, str, str]] = [
        ("HERMES_BRIDGE_BIND_HOST", "server", "bind_host", "str"),
        ("HERMES_BRIDGE_BIND_PORT", "server", "bind_port", "int"),
        ("HERMES_BRIDGE_ISSUER", "server", "issuer", "str"),
        ("HERMES_BRIDGE_RESOURCE", "server", "resource", "str"),
        ("HERMES_BRIDGE_DEV_MODE", "server", "dev_mode", "bool"),
        ("HERMES_BRIDGE_ALLOW_NON_LOOPBACK_BIND", "server", "allow_non_loopback_bind", "bool"),
        ("HERMES_BRIDGE_ALLOWED_HOSTS", "server", "allowed_hosts", "list"),
        ("HERMES_BRIDGE_CONSENT_EXPOSURE", "consent", "exposure", "str"),
        ("HERMES_BRIDGE_CONSENT_BIND_PORT", "consent", "bind_port", "int"),
        ("HERMES_BRIDGE_CONSENT_PUBLIC_URL", "consent", "public_url", "str"),
        ("HERMES_BRIDGE_OWNER_PASSWORD_FILE", "consent", "owner_password_file", "str"),
        ("HERMES_BRIDGE_HERMES_BACKEND", "hermes", "backend", "str"),
        ("HERMES_BRIDGE_HERMES_BASE_URL", "hermes", "base_url", "str"),
        ("HERMES_BRIDGE_HERMES_API_KEY_FILE", "hermes", "api_key_file", "str"),
        ("HERMES_BRIDGE_HERMES_ALLOW_NON_LOOPBACK", "hermes", "allow_non_loopback", "bool"),
        ("HERMES_BRIDGE_SYNC_ASK", "limits", "sync_ask_enabled", "bool"),
        ("HERMES_BRIDGE_PRIVATE_SCHEMES", "redirects", "private_schemes", "list"),
        ("HERMES_BRIDGE_DATA_DIR", "", "data_dir", "str"),
    ]
    for env_name, section, key, kind in mapping:
        value = os.environ.get(env_name)
        if value is None or value == "":
            continue
        parsed: object
        if kind == "int":
            parsed = int(value)
        elif kind == "bool":
            parsed = _bool(value)
        elif kind == "list":
            parsed = [item.strip() for item in value.split(",") if item.strip()]
        else:
            parsed = value
        if section:
            nest(section)[key] = parsed
        else:
            raw[key] = parsed


def _resolve_paths(settings: Settings, base: Path) -> Settings:
    data_dir = _abs(base, settings.data_dir)
    consent = settings.consent.model_copy(
        update={"owner_password_file": str(_abs(base, settings.consent.owner_password_file))}
    )
    hermes = settings.hermes.model_copy(
        update={"api_key_file": str(_abs(base, settings.hermes.api_key_file))}
    )
    return settings.model_copy(
        update={"data_dir": str(data_dir), "consent": consent, "hermes": hermes}
    )


def _abs(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _bool(value: str) -> bool:
    lowered = value.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError("invalid boolean environment value")


def _issuer(value: str, *, dev_mode: bool) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"https", "http"}:
        raise ConfigError("issuer scheme must be https")
    if parts.scheme == "http" and not dev_mode:
        raise ConfigError("issuer scheme must be https")
    if parts.path not in {"", "/"} or parts.query or parts.fragment or parts.username:
        raise ConfigError("issuer must be an origin without a path, query, or userinfo")
    if value.endswith("/"):
        raise ConfigError("issuer must not have a trailing slash")
    if not parts.hostname:
        raise ConfigError("issuer host is missing")
    return value


def _resource(value: str, *, dev_mode: bool) -> str:
    parts = urlsplit(value)
    if parts.scheme == "http" and not dev_mode:
        raise ConfigError("resource must be https")
    if parts.scheme not in {"https", "http"}:
        raise ConfigError("resource must be https")
    if parts.query or parts.fragment or parts.username or value.endswith("/"):
        raise ConfigError("resource must not have a query, fragment, userinfo, or trailing slash")
    if not parts.hostname:
        raise ConfigError("resource host is missing")
    return value


def _origin(value: str) -> tuple[str, str, int | None]:
    parts = urlsplit(value)
    return parts.scheme, (parts.hostname or "").lower(), parts.port


def _hostname(value: str) -> str:
    return (urlsplit(value).hostname or "").lower()


def _port(value: int) -> None:
    if not 1 <= value <= 65535:
        raise ConfigError("port out of range")


def _bind(host: str, *, allow_other: bool) -> None:
    if host in {"0.0.0.0", "::", ""}:
        raise ConfigError("refusing an unspecified bind address")
    if host in _LOOPBACK_NAMES:
        return
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ConfigError("bind host must be an IP address") from exc
    if ip.is_loopback:
        return
    if allow_other and (ip.is_private or ip.is_link_local):
        return
    raise ConfigError("refusing non-loopback bind host")


def _hermes_url(value: str, *, allow_other: bool) -> None:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or parts.username or parts.query or parts.fragment:
        raise ConfigError("hermes base_url must be an http origin")
    if parts.path not in {"", "/"}:
        raise ConfigError("hermes base_url must not include a path")
    host = (parts.hostname or "").lower()
    if host in _LOOPBACK_NAMES:
        return
    if not allow_other:
        raise ConfigError("hermes base_url must be loopback unless allow_non_loopback is set")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return
    raise ConfigError("hermes base_url must not be a public address")
