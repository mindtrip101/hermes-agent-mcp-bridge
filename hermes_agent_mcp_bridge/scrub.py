"""Redact secrets that might otherwise land in a log line or a tool result."""

from __future__ import annotations

import logging
import re

_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_FORM = re.compile(
    r"(?i)\b(refresh_token|code_verifier|code|client_secret|password|access_token)\s*[=:]\s*[^&\s]+"
)
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")


class ScrubFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [item for item in secrets if len(item) >= 8]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        record.msg = scrub_text(rendered, self._secrets)
        record.args = ()
        return True


def install_scrubber(secrets: list[str]) -> None:
    flt = ScrubFilter(secrets)
    for name in ("hermes_agent_mcp_bridge", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.filters = [item for item in logger.filters if not isinstance(item, ScrubFilter)]
        logger.addFilter(flt)


def scrub_text(value: str, secrets: list[str]) -> str:
    cleaned = _PEM.sub("[redacted]", value)
    cleaned = _BEARER.sub("Bearer [redacted]", cleaned)
    cleaned = _FORM.sub(lambda match: match.group(1) + "=[redacted]", cleaned)
    for secret in secrets:
        if secret and secret in cleaned:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned
