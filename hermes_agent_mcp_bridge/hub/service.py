"""List profiles, queue a task for the worker, and read it back."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from hermes_agent_mcp_bridge.hub.adapters import ProviderAdapter, build_adapters
from hermes_agent_mcp_bridge.hub.config import AgentProfile, HubSettings, endpoint_class
from hermes_agent_mcp_bridge.hub.errors import MESSAGES, STUB_DETAIL, AdapterError, HubError
from hermes_agent_mcp_bridge.hub.inbox import Attempt, TaskRecord, TaskStore
from hermes_agent_mcp_bridge.hub.router import is_inbox, is_push, route_order
from hermes_agent_mcp_bridge.scrub import scrub_text

_TERMINAL = frozenset({"succeeded", "failed", "waiting", "claimed"})


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

    def run_once(self) -> bool:
        """Run one queued task. The Hermes job worker calls this when it is idle."""

        record = self._inbox.claim_queued()
        if record is None:
            return False
        try:
            self._execute(record)
        except Exception:
            self._fail_quiet(record.task_id, "provider_error")
        return True

    def call(self, name: str, owner: str, arguments: dict[str, Any]) -> ToolResult:
        if name == "list_agents":
            return ToolResult(self._list_payload(), False)
        if name == "task_status":
            return self._status(owner, arguments)
        if name == "delegate_task":
            return self._delegate(owner, arguments)
        if name == "complete_task":
            return self._complete(owner, arguments)
        raise HubError("invalid_input")

    def _list_payload(self) -> dict[str, Any]:
        today = _today()
        agents: list[dict[str, Any]] = []
        for profile in self._settings.agents:
            push = is_push(profile)
            agents.append(
                {
                    "name": profile.name,
                    "provider": profile.provider,
                    "model": profile.model,
                    "cost_tier": profile.cost_tier,
                    "tools_allowed": list(profile.tools_allowed),
                    "runnable": push,
                    "mode": "push" if push else "inbox",
                    "auth": "env" if profile.api_key_env else "none",
                    "account": profile.account,
                    "endpoint": endpoint_class(profile),
                    "daily_request_budget": profile.daily_request_budget,
                    "requests_today": self._inbox.usage(profile.name, today),
                    "detail": "" if push else STUB_DETAIL.get(profile.provider, ""),
                }
            )
        return {
            "policy": self._settings.policy,
            "strong_cost_tier": self._settings.strong_cost_tier,
            "max_wait_seconds": self._settings.max_wait_seconds,
            "agents": agents,
        }

    def _status(self, owner: str, arguments: dict[str, Any]) -> ToolResult:
        task_id = _task_id(arguments.get("task_id"))
        record = self._inbox.get(task_id, owner)
        if record is None:
            raise HubError("not_found")
        return _result(record)

    def _delegate(self, owner: str, arguments: dict[str, Any]) -> ToolResult:
        task, agent, from_agent, strength, wait = _arguments(
            arguments,
            self._settings.max_task_chars,
            self._settings.max_wait_seconds,
        )
        ordered = route_order(
            list(self._settings.agents),
            agent=agent,
            from_agent=from_agent,
            strength=strength,
            strong_cost_tier=self._settings.strong_cost_tier,
        )
        first = ordered[0]
        reason = "explicit profile" if agent else _auto_reason(strength)
        if is_inbox(first):
            task_id = self._inbox.create(
                owner_sub=owner,
                from_agent=from_agent or "",
                requested_agent=first.name,
                agent=first.name,
                provider=first.provider,
                model=first.model,
                cost_tier=first.cost_tier,
                strength=strength,
                route_reason=reason,
                task_text=task,
                status="waiting",
            )
            record = self._inbox.get(task_id, owner)
            if record is None:
                raise HubError("not_found")
            return _result(record)
        task_id = self._inbox.create(
            owner_sub=owner,
            from_agent=from_agent or "",
            requested_agent=agent or "",
            agent=first.name,
            provider=first.provider,
            model=first.model,
            cost_tier=first.cost_tier,
            strength=strength,
            route_reason=reason,
            task_text=task,
            status="queued",
        )
        record = self._inbox.get(task_id, owner)
        if record is None:
            raise HubError("not_found")
        if wait > 0:
            record = self._wait(owner, task_id, wait)
        return _result(record)

    def _complete(self, owner: str, arguments: dict[str, Any]) -> ToolResult:
        task_id = _task_id(arguments.get("task_id"))
        current = self._inbox.get(task_id, owner)
        if current is None:
            raise HubError("not_found")
        if current.status not in {"waiting", "claimed"}:
            raise HubError("not_claimable")
        result_text, claim_only = _completion(arguments, self._settings.max_output_chars)
        try:
            if claim_only:
                record = self._inbox.complete_inbox(task_id, owner, result_text=None, claim_only=True)
            else:
                cleaned = scrub_text(result_text or "", self._live_secrets())
                record = self._inbox.complete_inbox(task_id, owner, result_text=cleaned, claim_only=False)
        except HubError as exc:
            if exc.code == "not_found":
                raise HubError("not_claimable") from exc
            raise
        return _result(record)

    def _execute(self, record: TaskRecord) -> None:
        try:
            ordered = route_order(
                list(self._settings.agents),
                agent=record.requested_agent or None,
                from_agent=record.from_agent or None,
                strength=record.strength,
                strong_cost_tier=self._settings.strong_cost_tier,
            )
        except HubError as exc:
            self._fail_quiet(record.task_id, exc.code)
            return
        last_code = "provider_error"
        saw_call = False
        pushed = False
        for profile in ordered:
            if not is_push(profile):
                continue
            pushed = True
            if self._over_budget(profile):
                self._inbox.add_attempt(
                    record.task_id,
                    agent=profile.name,
                    provider=profile.provider,
                    model=profile.model,
                    status="failed",
                    error_code="budget",
                )
                if not saw_call:
                    last_code = "budget"
                continue
            self._inbox.mark_profile(
                record.task_id,
                agent=profile.name,
                provider=profile.provider,
                model=profile.model,
                cost_tier=profile.cost_tier,
            )
            self._inbox.increment_usage(profile.name, _today())
            adapter = self._adapters.get(profile.provider)
            if adapter is None:
                self._inbox.add_attempt(
                    record.task_id,
                    agent=profile.name,
                    provider=profile.provider,
                    model=profile.model,
                    status="failed",
                    error_code="provider_error",
                )
                last_code = "provider_error"
                continue
            try:
                text = adapter.complete(profile, task=record.task_text, system=_system(profile, record.from_agent))
            except AdapterError as exc:
                code = exc.code if exc.code in MESSAGES else "provider_error"
                self._inbox.add_attempt(
                    record.task_id,
                    agent=profile.name,
                    provider=profile.provider,
                    model=profile.model,
                    status="failed",
                    error_code=code,
                )
                if code != "missing_api_key" or not saw_call:
                    last_code = code
                if code != "missing_api_key":
                    saw_call = True
                continue
            except Exception:
                self._inbox.add_attempt(
                    record.task_id,
                    agent=profile.name,
                    provider=profile.provider,
                    model=profile.model,
                    status="failed",
                    error_code="provider_error",
                )
                last_code = "provider_error"
                saw_call = True
                continue
            cleaned = scrub_text(text, self._live_secrets())
            limited, truncated = _limit(cleaned, self._settings.max_output_chars)
            self._inbox.add_attempt(
                record.task_id,
                agent=profile.name,
                provider=profile.provider,
                model=profile.model,
                status="succeeded",
                error_code=None,
            )
            self._inbox.finish(
                record.task_id,
                status="succeeded",
                result_text=limited,
                error_code=None,
                truncated=truncated,
            )
            return
        if not pushed:
            self._fail_quiet(record.task_id, "no_agent")
            return
        self._fail_quiet(record.task_id, last_code)

    def _wait(self, owner: str, task_id: str, seconds: float) -> TaskRecord:
        deadline = time.monotonic() + seconds
        while True:
            record = self._inbox.get(task_id, owner)
            if record is None:
                raise HubError("not_found")
            if record.status in _TERMINAL:
                return record
            if time.monotonic() >= deadline:
                return record
            time.sleep(0.05)

    def _over_budget(self, profile: AgentProfile) -> bool:
        budget = profile.daily_request_budget
        if budget is None:
            return False
        return self._inbox.usage(profile.name, _today()) >= budget

    def _fail_quiet(self, task_id: str, code: str) -> None:
        try:
            self._inbox.finish(
                task_id,
                status="failed",
                result_text=None,
                error_code=code if code in MESSAGES else "provider_error",
                truncated=False,
            )
        except HubError:
            return

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
    failed = record.status in {"failed", "unsupported"}
    return ToolResult(_payload(record), failed)


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
        "requested_agent": record.requested_agent,
        "task": record.task_text,
        "truncated": record.truncated,
        "attempts": [_attempt_payload(item) for item in record.attempts],
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


def _attempt_payload(item: Attempt) -> dict[str, str]:
    return {
        "agent": item.agent,
        "provider": item.provider,
        "model": item.model,
        "status": item.status,
        "error_code": item.error_code or "",
    }


def _arguments(
    arguments: dict[str, Any],
    max_chars: int,
    max_wait: float,
) -> tuple[str, str | None, str | None, str, float]:
    task = arguments.get("task")
    if not isinstance(task, str) or not task.strip() or len(task.strip()) > max_chars:
        raise HubError("invalid_input")
    agent = _optional_name(arguments.get("agent"))
    from_agent = _optional_name(arguments.get("from_agent"))
    strength = arguments.get("strength", "any")
    if not isinstance(strength, str) or strength not in {"any", "strong"}:
        raise HubError("invalid_input")
    return task.strip(), agent, from_agent, strength, _wait_seconds(arguments.get("wait_seconds"), max_wait)


def _wait_seconds(value: object, cap: float) -> float:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise HubError("invalid_input")
    if value < 0 or value > cap:
        raise HubError("invalid_input")
    return float(value)


def _completion(arguments: dict[str, Any], max_chars: int) -> tuple[str | None, bool]:
    if "result" not in arguments or arguments.get("result") is None:
        return None, True
    result = arguments.get("result")
    if not isinstance(result, str) or not result.strip() or "\x00" in result or len(result) > max_chars:
        raise HubError("invalid_input")
    return result, False


def _task_id(value: object) -> str:
    if not isinstance(value, str) or len(value) > 80 or not value.startswith("task_"):
        raise HubError("invalid_input")
    return value


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


def _auto_reason(strength: str) -> str:
    if strength == "strong":
        return "lowest cost_tier among profiles at or above the strong tier"
    return "lowest cost_tier among runnable profiles"


def _limit(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())
