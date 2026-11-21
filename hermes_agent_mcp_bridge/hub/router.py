"""Prefer the cheapest profile that can run the task, unless the task asks for a stronger model."""

from __future__ import annotations

from dataclasses import dataclass

from hermes_agent_mcp_bridge.hub.config import AgentProfile
from hermes_agent_mcp_bridge.hub.errors import HubError

PUSH_PROVIDERS = frozenset({"openai_compatible", "anthropic"})


@dataclass(frozen=True)
class RouteDecision:
    profile: AgentProfile
    reason: str
    runnable: bool


def is_push(profile: AgentProfile) -> bool:
    """True when the hub calls this profile over HTTP."""

    if profile.provider in PUSH_PROVIDERS:
        return True
    return profile.provider == "grok" and bool(profile.api_key_env)


def is_inbox(profile: AgentProfile) -> bool:
    """True when a connected client finishes the task with complete_task."""

    if profile.provider == "cursor":
        return True
    return profile.provider == "grok" and not profile.api_key_env


def route(
    agents: list[AgentProfile],
    *,
    agent: str | None,
    from_agent: str | None,
    strength: str,
    strong_cost_tier: int,
) -> RouteDecision:
    ordered = route_order(
        agents,
        agent=agent,
        from_agent=from_agent,
        strength=strength,
        strong_cost_tier=strong_cost_tier,
    )
    chosen = ordered[0]
    if agent is not None:
        reason = "explicit profile"
    elif strength == "strong":
        reason = "lowest cost_tier among profiles at or above the strong tier"
    else:
        reason = "lowest cost_tier among runnable profiles"
    return RouteDecision(profile=chosen, reason=reason, runnable=is_push(chosen))


def route_order(
    agents: list[AgentProfile],
    *,
    agent: str | None,
    from_agent: str | None,
    strength: str,
    strong_cost_tier: int,
) -> list[AgentProfile]:
    """Profiles to try, cheapest first. An explicit name stays first, then the rest of the pool."""

    by_name = {item.name: item for item in agents}
    if from_agent is not None and from_agent not in by_name:
        raise HubError("unknown_agent")
    push = [item for item in agents if is_push(item) and item.name != from_agent]
    if strength == "strong":
        ranked = [item for item in push if item.cost_tier >= strong_cost_tier]
        none_meet_tier = not ranked and bool(push)
    elif strength == "any":
        ranked = list(push)
        none_meet_tier = False
    else:
        raise HubError("invalid_input")
    ranked.sort(key=lambda item: (item.cost_tier, item.name))
    if agent is not None:
        profile = by_name.get(agent)
        if profile is None:
            raise HubError("unknown_agent")
        if from_agent is not None and from_agent == profile.name:
            raise HubError("self_handoff")
        if is_inbox(profile):
            return [profile]
        if not is_push(profile):
            raise HubError("unknown_agent")
        return [profile, *[item for item in ranked if item.name != profile.name]]
    if not ranked:
        if none_meet_tier:
            raise HubError("no_strong_agent")
        raise HubError("no_agent")
    return ranked
