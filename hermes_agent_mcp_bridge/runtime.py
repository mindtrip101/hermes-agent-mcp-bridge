"""Process-wide objects shared by the public listener and the optional consent listener."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa

from hermes_agent_mcp_bridge.config import Settings
from hermes_agent_mcp_bridge.hermes import HermesBackend
from hermes_agent_mcp_bridge.jobs import JobWorker
from hermes_agent_mcp_bridge.store import Store


class Limiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [item for item in self._hits.get(key, []) if now - item < window]
            if len(hits) >= limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True


@dataclass
class Runtime:
    settings: Settings
    store: Store
    key: rsa.RSAPrivateKey
    kid: str
    backend: HermesBackend
    password: str
    secret_values: list[str]
    limiter: Limiter = field(default_factory=Limiter)
    cimd_transport: httpx.BaseTransport | None = None
    worker: JobWorker | None = None

    def __repr__(self) -> str:
        return "Runtime(redacted)"
