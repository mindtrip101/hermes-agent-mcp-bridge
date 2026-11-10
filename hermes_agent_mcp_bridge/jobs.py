"""One worker. Queued jobs survive a restart; a run id that was already issued is polled again."""

from __future__ import annotations

import threading
import time

from hermes_agent_mcp_bridge.errors import HermesFailed
from hermes_agent_mcp_bridge.hermes import HermesBackend
from hermes_agent_mcp_bridge.scrub import scrub_text
from hermes_agent_mcp_bridge.store import Store


class JobWorker:
    def __init__(
        self,
        store: Store,
        backend: HermesBackend,
        *,
        interval: float,
        poll_interval: float,
        run_timeout: int,
        max_output: int,
        secrets: list[str],
        retention_hours: int,
    ) -> None:
        self._store = store
        self._backend = backend
        self._interval = interval
        self._poll_interval = poll_interval
        self._run_timeout = run_timeout
        self._max_output = max_output
        self._secrets = secrets
        self._retention_hours = retention_hours
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._store.recover()
        self._store.purge_jobs(older_than=int(time.time()) - self._retention_hours * 3600)
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="hermes-bridge-jobs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._backend.request_stop()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self._store.claim_next()
            if job is None:
                self._stop.wait(self._interval)
                continue
            self._run_one(job.job_id, job.prompt, job.backend_run_id)

    def _run_one(self, job_id: str, prompt: str, backend_run_id: str | None) -> None:
        try:
            run_id = backend_run_id or self._backend.start(prompt, job_id=job_id)
            if backend_run_id is None:
                self._store.set_backend_run(job_id, run_id)
            deadline = time.monotonic() + self._run_timeout
            while not self._stop.is_set():
                view = self._backend.poll(run_id)
                if view.done:
                    if view.ok:
                        text = scrub_text(view.output or "", self._secrets)
                        truncated = len(text) > self._max_output
                        if truncated:
                            text = text[: self._max_output]
                        self._store.finish_job(
                            job_id,
                            status="succeeded",
                            result_text=text,
                            error_code=None,
                            truncated=truncated,
                        )
                    else:
                        self._store.finish_job(
                            job_id,
                            status="failed",
                            result_text=None,
                            error_code=view.error_code or "hermes_error",
                            truncated=False,
                        )
                    return
                if time.monotonic() > deadline:
                    self._store.finish_job(
                        job_id,
                        status="failed",
                        result_text=None,
                        error_code="timeout",
                        truncated=False,
                    )
                    return
                self._stop.wait(self._poll_interval)
        except HermesFailed as exc:
            self._store.finish_job(
                job_id,
                status="failed",
                result_text=None,
                error_code=exc.code,
                truncated=False,
            )
