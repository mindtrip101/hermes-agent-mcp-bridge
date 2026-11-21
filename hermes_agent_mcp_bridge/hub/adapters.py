"""Provider calls. OpenAI-compatible and Anthropic are live. Grok Bot and Cursor are stubs."""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

import httpx

from hermes_agent_mcp_bridge.hub.config import AgentProfile
from hermes_agent_mcp_bridge.hub.errors import STUB_DETAIL, AdapterError

ANTHROPIC_VERSION = "2023-06-01"
_MAX_BODY_BYTES = 1_000_000


class ProviderAdapter(Protocol):
    def complete(self, profile: AgentProfile, *, task: str, system: str) -> str: ...


class OpenAICompatibleAdapter:
    """POST {base_url}/chat/completions. Covers OpenAI and local servers."""

    def __init__(self, *, timeout: float, transport: httpx.BaseTransport | None) -> None:
        self._timeout = timeout
        self._transport = transport

    def complete(self, profile: AgentProfile, *, task: str, system: str) -> str:
        key = resolve_api_key(profile)
        url = profile.base_url.rstrip("/") + "/chat/completions"
        headers = {"accept": "application/json"}
        if key:
            headers["authorization"] = "Bearer " + key
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": task})
        body: dict[str, Any] = {"model": profile.model, "messages": messages, "stream": False}
        if profile.token_param != "omit":
            body[profile.token_param] = profile.max_tokens
        with _client(self._timeout, self._transport) as client:
            payload = _post_json(client, url, headers=headers, body=body)
        return _openai_text(payload)


class AnthropicAdapter:
    """POST {base_url}/v1/messages."""

    def __init__(self, *, timeout: float, transport: httpx.BaseTransport | None) -> None:
        self._timeout = timeout
        self._transport = transport

    def complete(self, profile: AgentProfile, *, task: str, system: str) -> str:
        key = resolve_api_key(profile)
        if not key:
            raise AdapterError("missing_api_key")
        url = _anthropic_messages_url(profile.base_url)
        headers = {
            "accept": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
            "x-api-key": key,
        }
        body: dict[str, Any] = {
            "model": profile.model,
            "max_tokens": profile.max_tokens,
            "messages": [{"role": "user", "content": task}],
        }
        if system:
            body["system"] = system
        with _client(self._timeout, self._transport) as client:
            payload = _post_json(client, url, headers=headers, body=body)
        return _anthropic_text(payload)


class StubAdapter:
    """A named profile the hub can list and refuse to call."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        if provider not in STUB_DETAIL:
            raise ValueError("unknown stub provider")

    def complete(self, profile: AgentProfile, *, task: str, system: str) -> str:
        del profile, task, system
        raise AdapterError("provider_stub")


def build_adapters(*, timeout: float, transport: httpx.BaseTransport | None) -> dict[str, ProviderAdapter]:
    openai = OpenAICompatibleAdapter(timeout=timeout, transport=transport)
    anthropic = AnthropicAdapter(timeout=timeout, transport=transport)
    return {
        "openai_compatible": openai,
        "anthropic": anthropic,
        "grok": StubAdapter("grok"),
        "cursor": StubAdapter("cursor"),
    }


def resolve_api_key(profile: AgentProfile) -> str:
    if not profile.api_key_env:
        if profile.provider == "anthropic":
            raise AdapterError("missing_api_key")
        return ""
    value = os.environ.get(profile.api_key_env, "")
    if value == "" or value != value.strip() or "\n" in value or "\r" in value:
        raise AdapterError("missing_api_key")
    return value


def _anthropic_messages_url(base_url: str) -> str:
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/v1"):
        return trimmed + "/messages"
    return trimmed + "/v1/messages"


def _client(timeout: float, transport: httpx.BaseTransport | None) -> httpx.Client:
    if transport is None:
        return httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False)
    return httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False, transport=transport)


def _post_json(client: httpx.Client, url: str, *, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    try:
        response = client.post(url, headers=headers, json=body)
    except httpx.TimeoutException as exc:
        raise AdapterError("timeout") from exc
    except httpx.HTTPError as exc:
        raise AdapterError("provider_error") from exc
    if response.status_code in {401, 403}:
        raise AdapterError("auth_failed")
    if response.status_code == 429:
        raise AdapterError("rate_limited")
    if response.status_code != 200:
        raise AdapterError("provider_error")
    if len(response.content) > _MAX_BODY_BYTES:
        raise AdapterError("provider_error")
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise AdapterError("provider_error") from exc
    if not isinstance(payload, dict):
        raise AdapterError("provider_error")
    return payload


def _openai_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise AdapterError("empty_completion")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise AdapterError("empty_completion")
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        text = "".join(parts)
        if text.strip():
            return text
    raise AdapterError("empty_completion")


def _anthropic_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        raise AdapterError("empty_completion")
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    text = "".join(parts)
    if not text.strip():
        raise AdapterError("empty_completion")
    return text
