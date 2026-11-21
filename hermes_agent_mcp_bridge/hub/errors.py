"""Stable hub errors. Messages are fixed and contain no request data."""

from __future__ import annotations


class HubError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = MESSAGES.get(code, "The request failed.")


class AdapterError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


MESSAGES = {
    "invalid_input": "The task was empty or too long.",
    "unknown_agent": "No agent profile has that name.",
    "self_handoff": "An agent profile cannot delegate to itself.",
    "no_agent": "No runnable agent profile is available for this task.",
    "no_strong_agent": "No runnable agent profile meets the strong-model tier.",
    "provider_stub": "That provider is not called by the hub.",
    "missing_api_key": "The API key environment variable for that profile is empty.",
    "auth_failed": "The provider rejected the API key.",
    "rate_limited": "Too many requests. Wait and try again.",
    "timeout": "The provider did not answer in time.",
    "provider_error": "The provider request failed.",
    "empty_completion": "The provider returned no text.",
    "not_found": "No task with that id was found.",
    "interrupted": "The provider call was interrupted.",
}

# Shown on list_agents and on a stub task. Fixed text, no request data.
STUB_DETAIL = {
    "grok": (
        "Grok Bot is an MCP client of this bridge, for a personal or business account. "
        "It can call list_agents, delegate_task, and task_status. "
        "The hub does not call Grok Bot outbound."
    ),
    "cursor": (
        "Cursor and Copilot-style coding agents have no outbound adapter in this version. "
        "The profile is reserved so config and routing can name it."
    ),
}
