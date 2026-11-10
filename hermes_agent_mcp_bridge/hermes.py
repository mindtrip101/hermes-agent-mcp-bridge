"""Pluggable Hermes backends. The HTTP runs API is the supported 0.21 path."""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from hermes_agent_mcp_bridge.errors import HermesFailed

_TERMINAL_OK = frozenset({"completed"})
_TERMINAL_BAD = frozenset({"failed", "cancelled", "canceled", "interrupted"})


@dataclass
class RunView:
    done: bool
    ok: bool
    output: str | None
    error_code: str | None


class HermesBackend(Protocol):
    def start(self, prompt: str, *, job_id: str) -> str: ...

    def poll(self, run_id: str) -> RunView: ...

    def health(self) -> bool: ...

    def request_stop(self) -> None: ...


class HttpHermes:
    """POST /v1/runs and poll GET /v1/runs/{id} on a loopback Hermes API server."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        instructions: str = "",
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._instructions = instructions
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            transport=transport,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        )
        self._key = api_key
        self._stop = threading.Event()

    def close(self) -> None:
        self._client.close()

    def request_stop(self) -> None:
        self._stop.set()

    def health(self) -> bool:
        try:
            response = self._client.get("/health", headers=self._headers())
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def start(self, prompt: str, *, job_id: str) -> str:
        body: dict[str, object] = {"input": prompt}
        if self._instructions:
            body["instructions"] = self._instructions
        try:
            response = self._client.post(
                "/v1/runs",
                json=body,
                headers={**self._headers(), "Idempotency-Key": job_id},
            )
        except httpx.HTTPError as exc:
            raise HermesFailed("hermes_error") from exc
        if response.status_code not in {200, 202}:
            raise HermesFailed("hermes_error")
        try:
            payload = response.json()
        except ValueError as exc:
            raise HermesFailed("hermes_error") from exc
        run_id = payload.get("run_id") if isinstance(payload, dict) else None
        if not isinstance(run_id, str) or not run_id or len(run_id) > 200:
            raise HermesFailed("hermes_error")
        if not _safe_run_id(run_id):
            raise HermesFailed("hermes_error")
        return run_id

    def poll(self, run_id: str) -> RunView:
        if not _safe_run_id(run_id):
            return RunView(done=True, ok=False, output=None, error_code="hermes_error")
        try:
            response = self._client.get(f"/v1/runs/{run_id}", headers=self._headers())
        except httpx.HTTPError:
            return RunView(done=False, ok=False, output=None, error_code=None)
        if response.status_code == 404:
            return RunView(done=True, ok=False, output=None, error_code="hermes_error")
        if response.status_code != 200:
            return RunView(done=False, ok=False, output=None, error_code=None)
        try:
            payload = response.json()
        except ValueError:
            return RunView(done=False, ok=False, output=None, error_code=None)
        if not isinstance(payload, dict):
            return RunView(done=True, ok=False, output=None, error_code="hermes_error")
        status = str(payload.get("status", "")).lower()
        output = _output_text(payload.get("output"))
        if status in _TERMINAL_OK:
            return RunView(done=True, ok=True, output=output or "", error_code=None)
        if status in _TERMINAL_BAD:
            return RunView(done=True, ok=False, output=None, error_code="hermes_error")
        return RunView(done=False, ok=False, output=None, error_code=None)

    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._key}", "accept": "application/json"}


class CliHermes:
    """Run a configured argv with the prompt in a mode-0600 file. shell is never used."""

    def __init__(self, argv: list[str], *, timeout: int, max_output: int, workdir: Path) -> None:
        self._argv = list(argv)
        self._timeout = timeout
        self._max_output = max_output
        self._workdir = workdir
        self._stop = threading.Event()
        self._results: dict[str, RunView] = {}
        self._guard = threading.Lock()

    def request_stop(self) -> None:
        self._stop.set()

    def health(self) -> bool:
        return True

    def start(self, prompt: str, *, job_id: str) -> str:
        self._workdir.mkdir(parents=True, exist_ok=True)
        path = self._workdir / f"{job_id}.prompt"
        path.write_text(prompt, encoding="utf-8")
        path.chmod(0o600)
        argv = [part.replace("{prompt_file}", str(path)) for part in self._argv]
        if prompt and any(prompt in part for part in argv):
            raise HermesFailed("hermes_error")
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        deadline = time.monotonic() + self._timeout
        while proc.poll() is None:
            if self._stop.is_set() or time.monotonic() > deadline:
                proc.kill()
                proc.wait(timeout=5)
                raise HermesFailed("timeout" if time.monotonic() > deadline else "interrupted")
            time.sleep(0.05)
        raw = b""
        if proc.stdout is not None:
            raw = proc.stdout.read(self._max_output + 1)
        if proc.returncode != 0:
            raise HermesFailed("hermes_error")
        text = raw.decode("utf-8", errors="replace")
        view = RunView(done=True, ok=True, output=text, error_code=None)
        with self._guard:
            self._results[job_id] = view
        return job_id

    def poll(self, run_id: str) -> RunView:
        with self._guard:
            view = self._results.get(run_id)
        if view is None:
            return RunView(done=True, ok=False, output=None, error_code="hermes_error")
        return view


class FakeHermes:
    """In-memory backend for tests. Not used by the serve command."""

    def __init__(self, *, output: str = "bridge-ok", delay_polls: int = 0, fail: bool = False) -> None:
        self.output = output
        self.delay_polls = delay_polls
        self.fail = fail
        self.prompts: list[str] = []
        self._polls: dict[str, int] = {}
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def health(self) -> bool:
        return True

    def start(self, prompt: str, *, job_id: str) -> str:
        self.prompts.append(prompt)
        return "run_" + job_id

    def poll(self, run_id: str) -> RunView:
        count = self._polls.get(run_id, 0) + 1
        self._polls[run_id] = count
        if self.fail:
            return RunView(done=True, ok=False, output=None, error_code="hermes_error")
        if count <= self.delay_polls:
            return RunView(done=False, ok=False, output=None, error_code=None)
        return RunView(done=True, ok=True, output=self.output, error_code=None)


def _safe_run_id(run_id: str) -> bool:
    if not run_id or len(run_id) > 200:
        return False
    return all(ch.isalnum() or ch in {"-", "_", "."} for ch in run_id)


def _output_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts) if parts else None
    return None


def loopback_base(url: str) -> str:
    """Used by tests that want the origin only."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"
