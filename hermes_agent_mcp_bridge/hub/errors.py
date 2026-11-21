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
    "budget": "That profile is over its daily request budget.",
    "not_claimable": "That task is not waiting for a client to finish it.",
}

# Shown on list_agents and on a stub task. Fixed text, no request data.
STUB_DETAIL = {
    "grok": (
        "This profile is an inbox for a connected Grok Bot. "
        "Call complete_task with the task id and the result. "
        "Set api_key_env on the profile to call the xAI API instead."
    ),
    "cursor": (
        "This profile is an inbox for a connected coding agent. "
        "Call complete_task with the task id and the result. "
        "The hub does not call Cursor outbound."
    ),
}
