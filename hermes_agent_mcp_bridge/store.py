"""SQLite state for clients, refresh tokens, and jobs. Tokens are stored as hashes."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_agent_mcp_bridge.pkce import verifier_matches


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class ClientRecord:
    client_id: str
    client_name: str
    redirect_uris: list[str]


@dataclass
class PendingAuth:
    request_id: str
    client_id: str
    client_name: str
    redirect_uri: str
    challenge: str
    resource: str
    scope: str
    state: str


@dataclass
class JobRecord:
    job_id: str
    owner_sub: str
    status: str
    prompt: str
    result_text: str | None
    error_code: str | None
    backend_run_id: str | None
    truncated: bool
    created_at: int
    updated_at: int


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()
        os.chmod(path, 0o600)

    def close(self) -> None:
        with self._lock:
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
                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY,
                    client_name TEXT NOT NULL,
                    redirect_uris TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cimd_cache (
                    client_id TEXT PRIMARY KEY,
                    document TEXT NOT NULL,
                    fetched_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_auth (
                    request_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_codes (
                    code_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    challenge TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    state TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0,
                    family_id TEXT
                );
                CREATE TABLE IF NOT EXISTS refresh_tokens (
                    token_hash TEXT PRIMARY KEY,
                    family_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS access_family (
                    jti TEXT PRIMARY KEY,
                    family_id TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revoked_jti (
                    jti TEXT PRIMARY KEY,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    owner_sub TEXT NOT NULL,
                    status TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    result_text TEXT,
                    error_code TEXT,
                    backend_run_id TEXT,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )

    def insert_client(self, client_id: str, client_name: str, redirect_uris: list[str]) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO clients (client_id, client_name, redirect_uris, created_at) VALUES (?, ?, ?, ?)",
                (client_id, client_name, json.dumps(redirect_uris), int(time.time())),
            )

    def get_client(self, client_id: str) -> ClientRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT client_id, client_name, redirect_uris FROM clients WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        if row is None:
            return None
        uris = json.loads(row["redirect_uris"])
        return ClientRecord(client_id=row["client_id"], client_name=row["client_name"], redirect_uris=uris)

    def cache_cimd(self, client_id: str, document: dict[str, Any]) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO cimd_cache (client_id, document, fetched_at) VALUES (?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET document = excluded.document, fetched_at = excluded.fetched_at
                """,
                (client_id, json.dumps(document), int(time.time())),
            )

    def get_cimd(self, client_id: str, *, max_age: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT document, fetched_at FROM cimd_cache WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        if row is None or int(time.time()) - int(row["fetched_at"]) > max_age:
            return None
        loaded = json.loads(row["document"])
        if not isinstance(loaded, dict):
            return None
        return loaded

    def save_pending(self, pending: PendingAuth, *, ttl: int) -> None:
        payload = {
            "request_id": pending.request_id,
            "client_id": pending.client_id,
            "client_name": pending.client_name,
            "redirect_uri": pending.redirect_uri,
            "challenge": pending.challenge,
            "resource": pending.resource,
            "scope": pending.scope,
            "state": pending.state,
        }
        with self._tx() as conn:
            conn.execute("DELETE FROM pending_auth WHERE expires_at < ?", (int(time.time()),))
            conn.execute(
                "INSERT INTO pending_auth (request_id, payload, expires_at) VALUES (?, ?, ?)",
                (pending.request_id, json.dumps(payload), int(time.time()) + ttl),
            )

    def pop_pending(self, request_id: str) -> PendingAuth | None:
        now = int(time.time())
        with self._tx() as conn:
            row = conn.execute(
                "SELECT payload, expires_at FROM pending_auth WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM pending_auth WHERE request_id = ?", (request_id,))
            if int(row["expires_at"]) < now:
                return None
        data = json.loads(row["payload"])
        return PendingAuth(**data)

    def save_code(
        self,
        *,
        code: str,
        client_id: str,
        redirect_uri: str,
        challenge: str,
        resource: str,
        scope: str,
        subject: str,
        state: str,
        ttl: int,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO auth_codes (
                    code_hash, client_id, redirect_uri, challenge, resource, scope,
                    subject, state, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _hash(code),
                    client_id,
                    redirect_uri,
                    challenge,
                    resource,
                    scope,
                    subject,
                    state,
                    int(time.time()) + ttl,
                ),
            )

    def redeem_code(
        self,
        *,
        code: str,
        client_id: str,
        redirect_uri: str,
        verifier: str,
        resource: str,
        refresh_token: str,
        jti: str,
        subject_expected: str,
        access_ttl: int,
        refresh_ttl: int,
    ) -> tuple[str, str]:
        """Return (status, scope). Status is ok, invalid, reused, or mismatch."""
        now = int(time.time())
        family_id = secrets.token_urlsafe(16)
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM auth_codes WHERE code_hash = ?", (_hash(code),)).fetchone()
            if row is None or int(row["expires_at"]) < now:
                return ("invalid", "")
            if int(row["used"]):
                if row["family_id"]:
                    _revoke_family(conn, str(row["family_id"]), now + access_ttl)
                return ("reused", "")
            checks = (
                row["client_id"] == client_id
                and row["redirect_uri"] == redirect_uri
                and row["resource"] == resource
                and row["subject"] == subject_expected
                and verifier_matches(verifier, str(row["challenge"]))
            )
            conn.execute(
                "UPDATE auth_codes SET used = 1, family_id = ? WHERE code_hash = ?",
                (family_id if checks else None, _hash(code)),
            )
            if not checks:
                return ("mismatch", "")
            conn.execute(
                """
                INSERT INTO refresh_tokens (
                    token_hash, family_id, client_id, subject, resource, scope, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _hash(refresh_token),
                    family_id,
                    client_id,
                    row["subject"],
                    row["resource"],
                    row["scope"],
                    now + refresh_ttl,
                ),
            )
            conn.execute(
                "INSERT INTO access_family (jti, family_id, expires_at) VALUES (?, ?, ?)",
                (jti, family_id, now + access_ttl),
            )
            return ("ok", str(row["scope"]))

    def code_scope(self, code: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT scope FROM auth_codes WHERE code_hash = ?",
                (_hash(code),),
            ).fetchone()
        if row is None:
            return None
        return str(row["scope"])

    def redeem_refresh(
        self,
        *,
        refresh_token: str,
        client_id: str,
        resource: str | None,
        new_refresh: str,
        jti: str,
        access_ttl: int,
        refresh_ttl: int,
    ) -> tuple[str, str, str]:
        """Return (status, subject, scope). status is ok, invalid, reused, or mismatch."""
        now = int(time.time())
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM refresh_tokens WHERE token_hash = ?",
                (_hash(refresh_token),),
            ).fetchone()
            if row is None or int(row["expires_at"]) < now:
                return ("invalid", "", "")
            family_id = str(row["family_id"])
            if int(row["used"]):
                _revoke_family(conn, family_id, now + access_ttl)
                return ("reused", "", "")
            if row["client_id"] != client_id or (resource is not None and resource != row["resource"]):
                conn.execute("UPDATE refresh_tokens SET used = 1 WHERE token_hash = ?", (_hash(refresh_token),))
                _revoke_family(conn, family_id, now + access_ttl)
                return ("mismatch", "", "")
            conn.execute("UPDATE refresh_tokens SET used = 1 WHERE token_hash = ?", (_hash(refresh_token),))
            conn.execute(
                """
                INSERT INTO refresh_tokens (
                    token_hash, family_id, client_id, subject, resource, scope, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _hash(new_refresh),
                    family_id,
                    row["client_id"],
                    row["subject"],
                    row["resource"],
                    row["scope"],
                    now + refresh_ttl,
                ),
            )
            conn.execute(
                "INSERT INTO access_family (jti, family_id, expires_at) VALUES (?, ?, ?)",
                (jti, family_id, now + access_ttl),
            )
            return ("ok", str(row["subject"]), str(row["scope"]))

    def revoke_refresh(self, refresh_token: str) -> None:
        now = int(time.time())
        with self._tx() as conn:
            row = conn.execute(
                "SELECT family_id FROM refresh_tokens WHERE token_hash = ?",
                (_hash(refresh_token),),
            ).fetchone()
            if row is None:
                return
            conn.execute("UPDATE refresh_tokens SET used = 1 WHERE family_id = ?", (row["family_id"],))
            _revoke_family(conn, str(row["family_id"]), now + 86_400)

    def revoke_jti(self, jti: str, *, ttl: int) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO revoked_jti (jti, expires_at) VALUES (?, ?)",
                (jti, int(time.time()) + ttl),
            )

    def jti_revoked(self, jti: str) -> bool:
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM revoked_jti WHERE jti = ? AND expires_at >= ?",
                (jti, now),
            ).fetchone()
        return row is not None

    def create_job(self, *, owner_sub: str, prompt: str) -> str:
        job_id = "job_" + secrets.token_hex(16)
        now = int(time.time())
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, owner_sub, status, prompt, created_at, updated_at
                ) VALUES (?, ?, 'queued', ?, ?, ?)
                """,
                (job_id, owner_sub, prompt, now, now),
            )
        return job_id

    def get_job(self, job_id: str, owner_sub: str) -> JobRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ? AND owner_sub = ?",
                (job_id, owner_sub),
            ).fetchone()
        return _job(row) if row is not None else None

    def queue_depth(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued', 'submitted', 'running')"
            ).fetchone()
        return int(row["n"]) if row is not None else 0

    def recover(self) -> None:
        now = int(time.time())
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE jobs SET status = 'submitted', updated_at = ?
                WHERE status = 'running' AND backend_run_id IS NOT NULL
                """,
                (now,),
            )
            conn.execute(
                """
                UPDATE jobs SET status = 'queued', updated_at = ?
                WHERE status = 'running' AND backend_run_id IS NULL
                """,
                (now,),
            )

    def claim_next(self) -> JobRecord | None:
        now = int(time.time())
        with self._tx() as conn:
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('queued', 'submitted')
                ORDER BY created_at
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE jobs SET status = 'running', updated_at = ? WHERE job_id = ?",
                (now, row["job_id"]),
            )
        return _job(row)

    def set_backend_run(self, job_id: str, run_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE jobs SET backend_run_id = ?, status = 'running', updated_at = ? WHERE job_id = ?",
                (run_id, int(time.time()), job_id),
            )

    def finish_job(
        self,
        job_id: str,
        *,
        status: str,
        result_text: str | None,
        error_code: str | None,
        truncated: bool,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, result_text = ?, error_code = ?, truncated = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (status, result_text, error_code, 1 if truncated else 0, int(time.time()), job_id),
            )

    def purge_jobs(self, *, older_than: int) -> None:
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM jobs WHERE status IN ('succeeded', 'failed') AND updated_at < ?",
                (older_than,),
            )


def _revoke_family(conn: sqlite3.Connection, family_id: str, expires_at: int) -> None:
    rows = conn.execute("SELECT jti FROM access_family WHERE family_id = ?", (family_id,)).fetchall()
    for row in rows:
        conn.execute(
            "INSERT OR REPLACE INTO revoked_jti (jti, expires_at) VALUES (?, ?)",
            (row["jti"], expires_at),
        )
    conn.execute("UPDATE refresh_tokens SET used = 1 WHERE family_id = ?", (family_id,))


def _job(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        job_id=str(row["job_id"]),
        owner_sub=str(row["owner_sub"]),
        status=str(row["status"]),
        prompt=str(row["prompt"]),
        result_text=None if row["result_text"] is None else str(row["result_text"]),
        error_code=None if row["error_code"] is None else str(row["error_code"]),
        backend_run_id=None if row["backend_run_id"] is None else str(row["backend_run_id"]),
        truncated=bool(row["truncated"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )
