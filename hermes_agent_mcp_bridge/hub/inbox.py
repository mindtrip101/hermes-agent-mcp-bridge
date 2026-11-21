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
class Attempt:
    agent: str
    provider: str
    model: str
    status: str
    error_code: str | None


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    owner_sub: str
    from_agent: str
    requested_agent: str
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
    attempts: tuple[Attempt, ...]
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
                CREATE TABLE IF NOT EXISTS task_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS profile_usage (
                    profile TEXT NOT NULL,
                    day TEXT NOT NULL,
                    requests INTEGER NOT NULL,
                    PRIMARY KEY (profile, day)
                );
                """
            )
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if "requested_agent" not in columns:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN requested_agent TEXT NOT NULL DEFAULT ''")

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
        requested_agent: str,
        agent: str,
        provider: str,
        model: str,
        cost_tier: int,
        strength: str,
        route_reason: str,
        task_text: str,
        status: str = "queued",
    ) -> str:
        task_id = "task_" + secrets.token_hex(16)
        now = int(time.time())
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, owner_sub, from_agent, requested_agent, agent, provider, model,
                    cost_tier, strength, route_reason, task_text, status, truncated, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    task_id,
                    owner_sub,
                    from_agent,
                    requested_agent,
                    agent,
                    provider,
                    model,
                    cost_tier,
                    strength,
                    route_reason,
                    task_text,
                    status,
                    now,
                    now,
                ),
            )
        return task_id

    def claim_queued(self) -> TaskRecord | None:
        now = int(time.time())
        copied: dict[str, object] | None = None
        with self._tx() as conn:
            row = conn.execute(
                "SELECT task_id FROM tasks WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is not None:
                task_id = str(row["task_id"])
                conn.execute(
                    """
                    UPDATE tasks SET status = 'running', updated_at = ?
                    WHERE task_id = ? AND status = 'queued'
                    """,
                    (now, task_id),
                )
                changed = conn.execute("SELECT changes()").fetchone()
                if changed is not None and int(changed[0]) == 1:
                    fetched = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
                    if fetched is not None:
                        copied = dict(fetched)
        if copied is None:
            return None
        return self._hydrate(copied)

    def add_attempt(
        self,
        task_id: str,
        *,
        agent: str,
        provider: str,
        model: str,
        status: str,
        error_code: str | None,
    ) -> None:
        now = int(time.time())
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO task_attempts (
                    task_id, agent, provider, model, status, error_code, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, agent, provider, model, status, error_code, now),
            )

    def mark_profile(
        self,
        task_id: str,
        *,
        agent: str,
        provider: str,
        model: str,
        cost_tier: int,
    ) -> None:
        now = int(time.time())
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET agent = ?, provider = ?, model = ?, cost_tier = ?, updated_at = ?
                WHERE task_id = ? AND status = 'running'
                """,
                (agent, provider, model, cost_tier, now, task_id),
            )

    def finish(
        self,
        task_id: str,
        *,
        status: str,
        result_text: str | None,
        error_code: str | None,
        truncated: bool,
    ) -> TaskRecord:
        return self._transition(
            task_id,
            owner_sub=None,
            from_status=("running",),
            status=status,
            result_text=result_text,
            error_code=error_code,
            truncated=truncated,
        )

    def complete_inbox(
        self,
        task_id: str,
        owner_sub: str,
        *,
        result_text: str | None,
        claim_only: bool,
    ) -> TaskRecord:
        if claim_only:
            return self._transition(
                task_id,
                owner_sub=owner_sub,
                from_status=("waiting", "claimed"),
                status="claimed",
                result_text=None,
                error_code=None,
                truncated=False,
            )
        return self._transition(
            task_id,
            owner_sub=owner_sub,
            from_status=("waiting", "claimed"),
            status="succeeded",
            result_text=result_text,
            error_code=None,
            truncated=False,
        )

    def usage(self, profile: str, day: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT requests FROM profile_usage WHERE profile = ? AND day = ?",
                (profile, day),
            ).fetchone()
        if row is None:
            return 0
        return int(row["requests"])

    def increment_usage(self, profile: str, day: str) -> int:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO profile_usage (profile, day, requests) VALUES (?, ?, 1)
                ON CONFLICT(profile, day) DO UPDATE SET requests = requests + 1
                """,
                (profile, day),
            )
            row = conn.execute(
                "SELECT requests FROM profile_usage WHERE profile = ? AND day = ?",
                (profile, day),
            ).fetchone()
        if row is None:
            return 0
        return int(row["requests"])

    def get(self, task_id: str, owner_sub: str) -> TaskRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ? AND owner_sub = ?",
                (task_id, owner_sub),
            ).fetchone()
            copied = dict(row) if row is not None else None
        if copied is None:
            return None
        return self._hydrate(copied)

    def _transition(
        self,
        task_id: str,
        *,
        owner_sub: str | None,
        from_status: tuple[str, ...],
        status: str,
        result_text: str | None,
        error_code: str | None,
        truncated: bool,
    ) -> TaskRecord:
        now = int(time.time())
        copied: dict[str, object] | None = None
        marks = ",".join("?" for _ in from_status)
        owner_sql = ""
        params: list[object] = [status, result_text, error_code, 1 if truncated else 0, now, task_id]
        params.extend(from_status)
        if owner_sub is not None:
            owner_sql = " AND owner_sub = ?"
            params.append(owner_sub)
        with self._tx() as conn:
            conn.execute(
                f"""
                UPDATE tasks
                SET status = ?, result_text = ?, error_code = ?, truncated = ?, updated_at = ?
                WHERE task_id = ? AND status IN ({marks}){owner_sql}
                """,
                params,
            )
            changed = conn.execute("SELECT changes()").fetchone()
            if changed is not None and int(changed[0]) == 1:
                fetched = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
                if fetched is not None:
                    copied = dict(fetched)
        if copied is None:
            raise HubError("not_found")
        return self._hydrate(copied)

    def _hydrate(self, copied: dict[str, object]) -> TaskRecord:
        task_id = str(copied["task_id"])
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT agent, provider, model, status, error_code
                FROM task_attempts WHERE task_id = ? ORDER BY id
                """,
                (task_id,),
            ).fetchall()
        attempts = tuple(
            Attempt(
                agent=str(row["agent"]),
                provider=str(row["provider"]),
                model=str(row["model"]),
                status=str(row["status"]),
                error_code=row["error_code"] if isinstance(row["error_code"], str) else None,
            )
            for row in rows
        )
        return _record(copied, attempts)


def _record(row: dict[str, object], attempts: tuple[Attempt, ...]) -> TaskRecord:
    requested = row.get("requested_agent", "")
    return TaskRecord(
        task_id=str(row["task_id"]),
        owner_sub=str(row["owner_sub"]),
        from_agent=str(row["from_agent"]),
        requested_agent=str(requested or ""),
        agent=str(row["agent"]),
        provider=str(row["provider"]),
        model=str(row["model"]),
        cost_tier=int(row["cost_tier"]),  # type: ignore[arg-type]
        strength=str(row["strength"]),
        route_reason=str(row["route_reason"]),
        task_text=str(row["task_text"]),
        status=str(row["status"]),
        result_text=row["result_text"] if isinstance(row["result_text"], str) else None,
        error_code=row["error_code"] if isinstance(row["error_code"], str) else None,
        truncated=bool(row["truncated"]),
        attempts=attempts,
        created_at=int(row["created_at"]),  # type: ignore[arg-type]
        updated_at=int(row["updated_at"]),  # type: ignore[arg-type]
    )
