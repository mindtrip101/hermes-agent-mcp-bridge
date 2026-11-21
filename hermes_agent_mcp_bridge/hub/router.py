"""Prefer the cheapest profile that can run the task, unless the task asks for a stronger model."""

from __future__ import annotations

from dataclasses import dataclass

from hermes_agent_mcp_bridge.hub.config import AgentProfile
from hermes_agent_mcp_bridge.hub.errors import HubError

RUNNABLE_PROVIDERS = frozenset({"openai_compatible", "anthropic"})


@dataclass(frozen=True)
class RouteDecision:
    profile: AgentProfile
    reason: str
    runnable: bool


def route(
    agents: list[AgentProfile],
    *,
    agent: str | None,
    from_agent: str | None,
    strength: str,
    strong_cost_tier: int,
) -> RouteDecision:
    by_name = {item.name: item for item in agents}
    if from_agent is not None and from_agent not in by_name:
        raise HubError("unknown_agent")
    if agent is not None:
        profile = by_name.get(agent)
        if profile is None:
            raise HubError("unknown_agent")
        if from_agent is not None and from_agent == profile.name:
            raise HubError("self_handoff")
        return RouteDecision(
            profile=profile,
            reason="explicit profile",
            runnable=profile.provider in RUNNABLE_PROVIDERS,
        )
    runnable = [
        item for item in agents if item.provider in RUNNABLE_PROVIDERS and item.name != from_agent
    ]
    if not runnable:
        raise HubError("no_agent")
    if strength == "strong":
        pool = [item for item in runnable if item.cost_tier >= strong_cost_tier]
        if not pool:
            raise HubError("no_strong_agent")
        reason = "lowest cost_tier among profiles at or above the strong tier"
    elif strength == "any":
        pool = runnable
        reason = "lowest cost_tier among runnable profiles"
    else:
        raise HubError("invalid_input")
    chosen = min(pool, key=lambda item: (item.cost_tier, item.name))
    return RouteDecision(profile=chosen, reason=reason, runnable=True)
