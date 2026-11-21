"""List profiles, delegate one task, and read it back."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from hermes_agent_mcp_bridge.hub.adapters import ProviderAdapter, build_adapters
from hermes_agent_mcp_bridge.hub.config import AgentProfile, HubSettings, endpoint_class
from hermes_agent_mcp_bridge.hub.errors import MESSAGES, STUB_DETAIL, AdapterError, HubError
from hermes_agent_mcp_bridge.hub.inbox import TaskRecord, TaskStore
from hermes_agent_mcp_bridge.hub.router import RUNNABLE_PROVIDERS, route
from hermes_agent_mcp_bridge.scrub import scrub_text


@dataclass(frozen=True)
class ToolResult:
    payload: dict[str, Any]
    is_error: bool


class Hub:
    def __init__(
        self,
        settings: HubSettings,
        inbox: TaskStore,
        adapters: dict[str, ProviderAdapter],
        secrets: list[str],
    ) -> None:
        self._settings = settings
        self._inbox = inbox
        self._adapters = adapters
        self._secrets = [item for item in secrets if item]

    @classmethod
    def open(
        cls,
        settings: HubSettings,
        *,
        data_dir: Path,
        secrets: list[str],
        transport: httpx.BaseTransport | None = None,
    ) -> Hub:
        inbox = TaskStore(data_dir / "hub.sqlite3")
        adapters = build_adapters(timeout=settings.request_timeout_seconds, transport=transport)
        return cls(settings, inbox, adapters, secrets)

    def close(self) -> None:
        self._inbox.close()

    def call(self, name: str, owner: str, arguments: dict[str, Any]) -> ToolResult:
        if name == "list_agents":
            return ToolResult(self._list_payload(), False)
        if name == "task_status":
            return self._status(owner, arguments)
        if name == "delegate_task":
            return self._delegate(owner, arguments)
        raise HubError("invalid_input")

    def _list_payload(self) -> dict[str, Any]:
        agents: list[dict[str, Any]] = []
        for profile in self._settings.agents:
            runnable = profile.provider in RUNNABLE_PROVIDERS
            agents.append(
                {
                    "name": profile.name,
                    "provider": profile.provider,
                    "model": profile.model,
                    "cost_tier": profile.cost_tier,
                    "tools_allowed": list(profile.tools_allowed),
                    "runnable": runnable,
                    "auth": "env" if profile.api_key_env else "none",
                    "account": profile.account,
                    "endpoint": endpoint_class(profile),
                    "detail": "" if runnable else STUB_DETAIL.get(profile.provider, ""),
                }
            )
        return {
            "policy": self._settings.policy,
            "strong_cost_tier": self._settings.strong_cost_tier,
            "agents": agents,
        }

    def _status(self, owner: str, arguments: dict[str, Any]) -> ToolResult:
        task_id = arguments.get("task_id")
        if not isinstance(task_id, str) or len(task_id) > 80 or not task_id.startswith("task_"):
            raise HubError("invalid_input")
        record = self._inbox.get(task_id, owner)
        if record is None:
            raise HubError("not_found")
        return _result(record)

    def _delegate(self, owner: str, arguments: dict[str, Any]) -> ToolResult:
        task, agent, from_agent, strength = _arguments(arguments, self._settings.max_task_chars)
        decision = route(
            list(self._settings.agents),
            agent=agent,
            from_agent=from_agent,
            strength=strength,
            strong_cost_tier=self._settings.strong_cost_tier,
        )
        task_id = self._inbox.create(
            owner_sub=owner,
            from_agent=from_agent or "",
            agent=decision.profile.name,
            provider=decision.profile.provider,
            model=decision.profile.model,
            cost_tier=decision.profile.cost_tier,
            strength=strength,
            route_reason=decision.reason,
            task_text=task,
        )
        if not decision.runnable:
            record = self._inbox.finish(
                task_id,
                status="unsupported",
                result_text=None,
                error_code="provider_stub",
                truncated=False,
            )
            return _result(record)
        adapter = self._adapters.get(decision.profile.provider)
        if adapter is None:
            record = self._inbox.finish(
                task_id,
                status="failed",
                result_text=None,
                error_code="provider_error",
                truncated=False,
            )
            return _result(record)
        try:
            text = adapter.complete(decision.profile, task=task, system=_system(decision.profile, from_agent or ""))
        except AdapterError as exc:
            # The exception text can echo a URL or a key. Keep only the stable code.
            code = exc.code if exc.code in MESSAGES else "provider_error"
        except Exception:
            code = "provider_error"
        else:
            cleaned = scrub_text(text, self._live_secrets())
            limited, truncated = _limit(cleaned, self._settings.max_output_chars)
            record = self._inbox.finish(
                task_id,
                status="succeeded",
                result_text=limited,
                error_code=None,
                truncated=truncated,
            )
            return _result(record)
        record = self._inbox.finish(
            task_id,
            status="failed",
            result_text=None,
            error_code=code,
            truncated=False,
        )
        return _result(record)

    def _live_secrets(self) -> list[str]:
        found = list(self._secrets)
        for agent in self._settings.agents:
            if not agent.api_key_env:
                continue
            value = os.environ.get(agent.api_key_env, "")
            if len(value) >= 8 and value not in found:
                found.append(value)
        return found


def _result(record: TaskRecord) -> ToolResult:
    return ToolResult(_payload(record), record.status in {"failed", "unsupported"})


def _payload(record: TaskRecord) -> dict[str, Any]:
    body: dict[str, Any] = {
        "task_id": record.task_id,
        "status": record.status,
        "agent": record.agent,
        "provider": record.provider,
        "model": record.model,
        "cost_tier": record.cost_tier,
        "strength": record.strength,
        "route_reason": record.route_reason,
        "from_agent": record.from_agent,
        "task": record.task_text,
        "truncated": record.truncated,
    }
    if record.status == "succeeded":
        body["result"] = record.result_text or ""
    if record.error_code:
        error: dict[str, str] = {
            "code": record.error_code,
            "message": MESSAGES.get(record.error_code, "The request failed."),
        }
        if record.error_code == "provider_stub":
            detail = STUB_DETAIL.get(record.provider)
            if detail:
                error["detail"] = detail
        body["error"] = error
    return body


def _arguments(arguments: dict[str, Any], max_chars: int) -> tuple[str, str | None, str | None, str]:
    task = arguments.get("task")
    if not isinstance(task, str) or not task.strip() or len(task.strip()) > max_chars:
        raise HubError("invalid_input")
    agent = _optional_name(arguments.get("agent"))
    from_agent = _optional_name(arguments.get("from_agent"))
    strength = arguments.get("strength", "any")
    if not isinstance(strength, str) or strength not in {"any", "strong"}:
        raise HubError("invalid_input")
    return task.strip(), agent, from_agent, strength


def _optional_name(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HubError("invalid_input")
    return value.strip()


def _system(profile: AgentProfile, from_agent: str) -> str:
    parts: list[str] = []
    if profile.system_prompt:
        parts.append(profile.system_prompt)
    if from_agent:
        parts.append(
            "This task was delegated by the agent profile " + from_agent + ". Return the result to the caller."
        )
    return "\n\n".join(parts)


def _limit(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True
