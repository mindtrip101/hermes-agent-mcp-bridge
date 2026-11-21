"""Build the review prompt and the comment body. The comment never approves the pull request."""

from __future__ import annotations

from hermes_pr_reviewer.events import PullRequestEvent
from hermes_pr_reviewer.scrub import scrub

_INSTRUCTIONS = (
    "Review this pull request for risks and concrete suggestions. "
    "Reply in markdown with three sections: Summary, Risks, and Suggestions. "
    "If you see no material risk, say so under Risks. "
    "Do not approve the change and do not claim you edited files.\n\n"
)


def build_prompt(event: PullRequestEvent, diff: str, *, max_chars: int) -> str:
    header = (
        _INSTRUCTIONS
        + f"Repository: {event.owner}/{event.repo}\n"
        + f"Pull request: #{event.number} {_one_line(event.title)}\n"
        + f"Base: {event.base_ref}\n"
        + f"Head: {event.head_ref}\n\n"
        + "Diff:\n"
    )
    budget = max(0, max_chars - len(header) - 40)
    body = scrub(diff)
    truncated = len(body) > budget
    clipped = body[:budget]
    if truncated:
        clipped += "\n\n[diff truncated]\n"
    return header + clipped


def format_review(reply: str) -> str:
    text = scrub(reply).strip() or "The agent returned an empty review."
    if len(text) > 20_000:
        text = text[:20_000] + "\n\n[review truncated]"
    return (
        "## Hermes review\n\n"
        + text
        + "\n\n---\nReviewed by Hermes PR Reviewer through the MCP bridge. "
        "This comment does not approve the pull request.\n"
    )


def _one_line(value: str) -> str:
    collapsed = " ".join(value.split())
    return collapsed[:200]
