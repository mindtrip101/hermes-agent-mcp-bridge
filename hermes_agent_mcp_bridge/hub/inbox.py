"""SQLite task inbox. One row per delegation. The file mode is 0600."""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from hermes_agent_mcp_bridge.hub.errors import HubError


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    owner_sub: str
    from_agent: str
    agent: str
    provider: str
    model: str
    cost_tier: int
    strength: str
    route_reason: str
    task_text: str
    status: str
    result_text: str | None
    error_code: str | None
    truncated: bool
    created_at: int
    updated_at: int


class TaskStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        self.path = path
        self._lock = threading.Lock()
        self._closed = False
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()
        self.abandon_running()
        os.chmod(path, 0o600)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    owner_sub TEXT NOT NULL,
                    from_agent TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    cost_tier INTEGER NOT NULL,
                    strength TEXT NOT NULL,
                    route_reason TEXT NOT NULL,
                    task_text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_text TEXT,
                    error_code TEXT,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )

    def abandon_running(self) -> None:
        now = int(time.time())
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = 'failed', error_code = 'interrupted', updated_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )

    def create(
        self,
        *,
        owner_sub: str,
        from_agent: str,
        agent: str,
        provider: str,
        model: str,
        cost_tier: int,
        strength: str,
        route_reason: str,
        task_text: str,
    ) -> str:
        task_id = "task_" + secrets.token_hex(16)
        now = int(time.time())
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, owner_sub, from_agent, agent, provider, model, cost_tier,
                    strength, route_reason, task_text, status, truncated, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', 0, ?, ?)
                """,
                (
                    task_id,
                    owner_sub,
                    from_agent,
                    agent,
                    provider,
                    model,
                    cost_tier,
                    strength,
                    route_reason,
                    task_text,
                    now,
                    now,
                ),
            )
        return task_id

    def finish(
        self,
        task_id: str,
        *,
        status: str,
        result_text: str | None,
        error_code: str | None,
        truncated: bool,
    ) -> TaskRecord:
        now = int(time.time())
        copied: dict[str, object] | None = None
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, result_text = ?, error_code = ?, truncated = ?, updated_at = ?
                WHERE task_id = ? AND status = 'running'
                """,
                (status, result_text, error_code, 1 if truncated else 0, now, task_id),
            )
            changed = conn.execute("SELECT changes()").fetchone()
            if changed is not None and int(changed[0]) == 1:
                fetched = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
                if fetched is not None:
                    copied = dict(fetched)
        if copied is None:
            raise HubError("not_found")
        return _record(copied)

    def get(self, task_id: str, owner_sub: str) -> TaskRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ? AND owner_sub = ?",
                (task_id, owner_sub),
            ).fetchone()
            copied = dict(row) if row is not None else None
        return _record(copied) if copied is not None else None


def _record(row: dict[str, object]) -> TaskRecord:
    return TaskRecord(
        task_id=row["task_id"],
        owner_sub=row["owner_sub"],
        from_agent=row["from_agent"],
        agent=row["agent"],
        provider=row["provider"],
        model=row["model"],
        cost_tier=row["cost_tier"],
        strength=row["strength"],
        route_reason=row["route_reason"],
        task_text=row["task_text"],
        status=row["status"],
        result_text=row["result_text"],
        error_code=row["error_code"],
        truncated=bool(row["truncated"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
