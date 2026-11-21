"""Hub settings and agent profiles. Secrets are environment variable names, never values."""

from __future__ import annotations

import ipaddress
import os
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hermes_agent_mcp_bridge.errors import ConfigError

_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_PROVIDERS = frozenset({"openai_compatible", "anthropic", "grok", "cursor"})
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_TOOL = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_TOKEN_PARAMS = frozenset({"max_tokens", "max_completion_tokens", "omit"})


class AgentProfile(BaseModel):
    """One named model the hub can route to."""

    model_config = ConfigDict(extra="forbid")

    name: str
    provider: str
    model: str
    system_prompt: str = ""
    tools_allowed: list[str] = Field(default_factory=list)
    cost_tier: int = 1
    base_url: str = ""
    api_key_env: str = ""
    allow_private_base_url: bool = False
    account: str = ""
    max_tokens: int = 1024
    # None means unlimited. 0 means the profile is never called.
    daily_request_budget: int | None = None
    # OpenAI-compatible servers disagree on the token-cap field. Local servers
    # want max_tokens. Some hosted OpenAI models want max_completion_tokens.
    token_param: str = "max_tokens"

    @model_validator(mode="after")
    def _semantics(self) -> AgentProfile:
        if not _NAME.match(self.name) or self.name.endswith("-"):
            raise ConfigError("agent name must be a short lowercase slug")
        if self.provider not in _PROVIDERS:
            raise ConfigError("agent provider is not supported")
        if not _MODEL.match(self.model):
            raise ConfigError("agent model name is empty or unsafe")
        if len(self.system_prompt) > 4000 or "\x00" in self.system_prompt:
            raise ConfigError("system_prompt is too long or unsafe")
        if not 0 <= self.cost_tier <= 5:
            raise ConfigError("cost_tier must be between 0 and 5")
        if not 16 <= self.max_tokens <= 8192:
            raise ConfigError("max_tokens must be between 16 and 8192")
        if self.token_param not in _TOKEN_PARAMS:
            raise ConfigError("token_param must be max_tokens, max_completion_tokens, or omit")
        if self.api_key_env and not _ENV.match(self.api_key_env):
            raise ConfigError("api_key_env must be an environment variable name")
        if len(self.tools_allowed) > 32 or len(set(self.tools_allowed)) != len(self.tools_allowed):
            raise ConfigError("tools_allowed is too long or has duplicates")
        for tool_name in self.tools_allowed:
            if not _TOOL.match(tool_name):
                raise ConfigError("tool name is not a short token")
        if self.account not in {"", "personal", "business"}:
            raise ConfigError("grok account must be personal or business")
        if self.account and self.provider != "grok":
            raise ConfigError("account is only valid on the grok provider")
        if self.daily_request_budget is not None and not 0 <= self.daily_request_budget <= 100_000:
            raise ConfigError("daily_request_budget is out of range")
        inbox = self.provider == "cursor" or (self.provider == "grok" and not self.api_key_env)
        if inbox:
            if self.base_url or self.allow_private_base_url or (self.provider == "cursor" and self.api_key_env):
                raise ConfigError("an inbox profile does not take a base_url or an API key")
            if self.token_param != "max_tokens":
                raise ConfigError("token_param is only used by openai-compatible calls")
            return self
        if self.provider not in {"openai_compatible", "grok"} and self.token_param != "max_tokens":
            raise ConfigError("token_param is only used by openai-compatible calls")
        if self.provider == "anthropic" and not self.base_url:
            self.base_url = "https://api.anthropic.com"
        if self.provider == "grok" and not self.base_url:
            self.base_url = "https://api.x.ai/v1"
        if not self.base_url:
            raise ConfigError("this provider requires base_url")
        self.base_url = _endpoint(self.base_url, allow_private=self.allow_private_base_url)
        path = urlsplit(self.base_url).path
        if self.provider == "anthropic":
            if path not in {"", "/v1"}:
                raise ConfigError("anthropic base_url path must be empty or /v1")
            if not self.api_key_env:
                raise ConfigError("anthropic profiles require api_key_env")
        elif path.rstrip("/").endswith("chat/completions"):
            raise ConfigError("base_url must be the API root, not the chat completions path")
        elif not _is_local_http(self.base_url) and not self.api_key_env:
            raise ConfigError("a non-local openai_compatible profile requires api_key_env")
        return self


class HubSettings(BaseModel):
    """Off by default so an existing bridge keeps its current tool list."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    policy: str = "prefer_cheap"
    strong_cost_tier: int = 3
    request_timeout_seconds: float = 60
    # Ceiling for delegate_task's optional wait_seconds. 0 disables waiting.
    max_wait_seconds: float = 30
    max_task_chars: int = 8000
    max_output_chars: int = 32_000
    agents: list[AgentProfile] = Field(default_factory=list)

    @model_validator(mode="after")
    def _semantics(self) -> HubSettings:
        if self.policy != "prefer_cheap":
            raise ConfigError("hub policy must be prefer_cheap")
        if not 0 <= self.strong_cost_tier <= 5:
            raise ConfigError("strong_cost_tier must be between 0 and 5")
        if not 1 <= self.request_timeout_seconds <= 120:
            raise ConfigError("hub request_timeout_seconds must be between 1 and 120")
        if not 0 <= self.max_wait_seconds <= 120:
            raise ConfigError("hub max_wait_seconds must be between 0 and 120")
        if not 1 <= self.max_task_chars <= 100_000:
            raise ConfigError("hub max_task_chars out of range")
        if not 1 <= self.max_output_chars <= 200_000:
            raise ConfigError("hub max_output_chars out of range")
        if len(self.agents) > 32:
            raise ConfigError("too many agent profiles")
        names = [agent.name for agent in self.agents]
        if len(names) != len(set(names)):
            raise ConfigError("agent profile names must be unique")
        return self


def endpoint_class(profile: AgentProfile) -> str:
    """`local`, `remote`, or `none`. Hostnames are not included."""

    if not profile.base_url:
        return "none"
    if _is_local_http(profile.base_url):
        return "local"
    return "remote"


def secret_values(settings: HubSettings) -> list[str]:
    """Key values present in the environment at process start, for log scrubbing."""

    found: list[str] = []
    for agent in settings.agents:
        if not agent.api_key_env:
            continue
        value = os.environ.get(agent.api_key_env, "")
        if len(value) >= 8 and value not in found:
            found.append(value)
    return found


def _endpoint(value: str, *, allow_private: bool) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("agent base_url must be an http(s) URL without userinfo or a query")
    if not parts.hostname:
        raise ConfigError("agent base_url host is missing")
    host = parts.hostname.lower()
    if parts.scheme == "http" and not _http_host_allowed(host, allow_private=allow_private):
        raise ConfigError("http agent base_url must be loopback unless allow_private_base_url is set")
    return value.rstrip("/")


def _http_host_allowed(host: str, *, allow_private: bool) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    return bool(allow_private and (ip.is_private or ip.is_link_local))


def _is_local_http(value: str) -> bool:
    parts = urlsplit(value)
    if parts.scheme != "http":
        return False
    host = (parts.hostname or "").lower()
    return _http_host_allowed(host, allow_private=True)
