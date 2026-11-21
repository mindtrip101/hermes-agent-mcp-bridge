"""Redact credential-shaped text before it is sent to the agent or posted on a pull request."""

from __future__ import annotations

import re

_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(?:ghp|github_pat|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{10,}\b"),
    re.compile(r"\boit_[A-Za-z0-9]{8,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{10,}\b"),
)


def scrub(text: str) -> str:
    cleaned = text
    for pattern in _PATTERNS:
        cleaned = pattern.sub("[redacted]", cleaned)
    return cleaned
